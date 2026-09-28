"""Builds the safety gate's AssessmentInput for a pump.fun token from live
sources: the pump.fun event stream (Redis), Solana RPC (mint, bonding
curve, holders), and for migrated tokens Jupiter quotes and DexScreener.

Nothing is filled in when a source fails: the field stays None, the error
is recorded in the evidence, and the gate turns the gap into a NO_TRADE or
WAIT finding. The assembler never decides anything itself.
"""

import base64
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    TargetContext,
    GlobalMode,
    HolderInfo,
    ManualOverrides,
    MarketInfo,
    Observation,
    StrategyMode,
    TokenProgramInfo,
)
from yonixalpha_core.safety.rules import BlacklistRule, CustomRule, evaluate_custom_rules, match_blacklist
from yonixalpha_core.safety.settings import SafetySettings
from yonixalpha_core.solana import observation, pump_stream
from yonixalpha_core.solana.entry_quality import deterioration, volatility_estimate
from yonixalpha_core.solana.flow import (
    apply_demand_quality,
    early_buy_share,
    recent_high_above,
    round_trip_volume_share,
    synchronized_buy_cluster,
    trade_flow,
)
from yonixalpha_core.exit_intel import ExitConfig, solana_exit_decision
from yonixalpha_core.solana import creator_history, funding, sol_price
from yonixalpha_core.solana.market_data import DexScreenerClient, JupiterClient
from yonixalpha_core.solana.pumpfun import BondingCurveState, decode_bonding_curve
from yonixalpha_core.solana.token_safety import UnexpectedShape, parse_holders, parse_mint_account
from yonixalpha_core.strategies.solana import fresh_launch_signal, momentum_signal, post_migration_signal

FLOW_WINDOW_SECONDS = 300
DETERIORATION_WINDOW_SECONDS = 60  # current vs previous minute, against the 5 minutes before
VOLATILITY_WINDOW_SECONDS = 900

# Numeric features a custom rule may reference. Anything else is refused at
# rule creation (see apps/api), so a rule can't silently never fire.
RESISTANCE_WINDOW_SECONDS = 1800
RULE_FIELDS = {
    "price", "liquidity_quote", "age_seconds", "volatility", "top1_share", "top10_share", "creator_share",
    "unique_buyers", "trade_count", "buy_sell_volume_ratio", "top3_wallet_volume_share", "transfer_fee_bps",
    "early_buy_share", "sync_buy_cluster", "round_trip_share", "creator_launches_24h", "window_volume",
}


@dataclass
class Sources:
    redis: Redis
    rpc: Any
    jupiter: JupiterClient | None = None
    dexscreener: DexScreenerClient | None = None


@dataclass
class Controls:
    """Everything the operator controls, loaded by the caller from the DB."""

    settings: SafetySettings
    account: AccountState
    blacklist: list[BlacklistRule] = field(default_factory=list)
    custom_rules: list[CustomRule] = field(default_factory=list)
    global_mode: GlobalMode = GlobalMode.PAPER
    strategy_mode: StrategyMode = StrategyMode.PAPER
    live_trading_permitted: bool = False
    manual_approval_granted: bool = False
    overrides: ManualOverrides = field(default_factory=ManualOverrides)
    # Fixed SOL cost of a LIVE round trip (live_trading.fixed_trade_costs);
    # None for paper sizing.
    fixed_cost_quote: Decimal | None = None
    fixed_cost_detail: dict | None = None


def entry_exit_check(trades, now: datetime, creator: str | None, liquidity: Decimal | None,
                     settings) -> dict[str, Any]:
    """Runs the existing exit intelligence on the pre-entry flow as if a
    position were opened now: entry liquidity = current liquidity and no
    holder change or price high since entry yet, so only the flow rules
    (sell pressure, seller dominance, volume collapse, creator selling) can
    fire. The gate turns REDUCE / EXIT into EXIT_SIGNAL_AT_ENTRY (WAIT)."""
    d = solana_exit_decision(trades, now, creator, liquidity, liquidity, None, None,
                             cfg=ExitConfig.from_settings(settings), highest_price=None, current_price=None)
    return {"action": d.action, "reasons": list(d.reasons),
            "metrics": {k: (v if isinstance(v, (bool, int, list)) or v is None else str(v)) for k, v in d.metrics.items()}}


async def fetch_mint(rpc, mint: str, now: datetime) -> tuple[TokenProgramInfo | None, str | None]:
    try:
        res = await rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return parse_mint_account((res or {}).get("value"), now, "rpc:getAccountInfo"), None
    except (UnexpectedShape, ValueError, TypeError, KeyError) as exc:
        return None, f"mint: {exc}"
    except Exception as exc:  # noqa: BLE001 - transport/RPC failure is data unavailability
        return None, f"mint rpc: {exc}"


async def fetch_curve(rpc, address: str) -> tuple[BondingCurveState | None, str | None]:
    try:
        res = await rpc.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        value = (res or {}).get("value")
        if not value:
            return None, "bonding curve account not found"
        raw = base64.b64decode(value["data"][0])
        state = decode_bonding_curve(raw)
        return state, None if state else "bonding curve data did not decode"
    except Exception as exc:  # noqa: BLE001
        return None, f"curve rpc: {exc}"


async def token_account_owners(rpc, largest: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """Owner wallet of each token account from getTokenLargestAccounts.

    Read at the same "confirmed" commitment as the largest-accounts list: at
    the RPC default ("finalized", ~13 s behind) an account opened by a recent
    buyer does not exist yet, which on a fresh token is most of the top 20.
    An account that no longer exists was closed after the list was read, so
    it holds nothing and is dropped. One that exists without a parsed owner
    stays in with owner None and parse_holders rejects it."""
    addresses = [a["address"] for a in largest]
    if not addresses:
        return largest, {}
    accts = ((await rpc.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}]))
             or {}).get("value") or []
    if len(accts) != len(addresses):
        raise UnexpectedShape("getMultipleAccounts returned a different number of accounts")
    kept: list[dict[str, Any]] = []
    owners: dict[str, str | None] = {}
    for entry, acct in zip(largest, accts):
        if acct is None:
            continue
        kept.append(entry)
        data = acct.get("data")
        info = ((data.get("parsed") or {}).get("info") or {}) if isinstance(data, dict) else {}
        owners[entry["address"]] = info.get("owner")
    return kept, owners


async def fetch_holders(
    rpc, mint: str, supply_raw: int, excluded_owners: set[str], creator: str | None, now: datetime
) -> tuple[HolderInfo | None, str | None]:
    try:
        largest = ((await rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])) or {}).get("value") or []
        largest, owners = await token_account_owners(rpc, largest)
        return parse_holders(largest, owners, supply_raw, excluded_owners, creator, now, "rpc:getTokenLargestAccounts"), None
    except (UnexpectedShape, ValueError, TypeError, KeyError) as exc:
        return None, f"holders: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"holders rpc: {exc}"


GATE_TREND_MIN_WINDOW_SECONDS = 30


def _curve_progress(real_token_reserves: int | None, meta: dict[str, str]) -> Decimal | None:
    if real_token_reserves is None:
        return None
    initial = int(meta.get("initial_real_token_reserves") or 0) or observation.DEFAULT_INITIAL_REAL_TOKEN_RESERVES
    return max(Decimal(0), 1 - Decimal(real_token_reserves) / Decimal(initial))


def rule_features(inp: AssessmentInput) -> dict[str, Any]:
    m, h, fl, t = inp.market, inp.holders, inp.flow, inp.token
    ratio = None
    if fl and fl.sell_volume_quote > 0:
        ratio = fl.buy_volume_quote / fl.sell_volume_quote
    return {
        "price": m.price if m else None,
        "liquidity_quote": m.liquidity_quote if m else None,
        "age_seconds": m.age_seconds if m else None,
        "volatility": m.volatility if m else None,
        "top1_share": h.top1_share if h else None,
        "top10_share": h.top10_share if h else None,
        "creator_share": h.creator_share if h else None,
        "unique_buyers": fl.unique_buyers if fl else None,
        "trade_count": fl.trade_count if fl else None,
        "buy_sell_volume_ratio": ratio,
        "top3_wallet_volume_share": fl.top3_wallet_volume_share if fl else None,
        "transfer_fee_bps": t.transfer_fee_bps if t else None,
        "early_buy_share": fl.early_buy_share if fl else None,
        "sync_buy_cluster": fl.sync_buy_cluster if fl else None,
        "round_trip_share": fl.round_trip_share if fl else None,
        "creator_launches_24h": fl.creator_launches_24h if fl else None,
        "window_volume": (fl.buy_volume_quote + fl.sell_volume_quote) if fl else None,
    }


def _apply_controls(inp: AssessmentInput, c: Controls, name: str | None, symbol: str | None,
                    metadata: str | None = None, lifecycle: str | None = None) -> None:
    inp.blacklisted_by = match_blacklist(c.blacklist, inp.engine, name, symbol, inp.asset_id, metadata)
    inp.rule_actions = evaluate_custom_rules(c.custom_rules, inp.engine, rule_features(inp))


async def _wallet_analysis(src: Sources, inp: AssessmentInput, trades, creator: str | None, now: datetime,
                           decimals: int | None, c: Controls, ev: dict) -> None:
    apply_demand_quality(inp.flow, trades, now, FLOW_WINDOW_SECONDS, decimals)
    n = c.settings.funding_check_wallets
    if n <= 0 or not trades:
        return
    try:
        res = await funding.funding_links(src.rpc, src.redis, funding.early_buyers(trades, n), creator, n)
    except Exception as exc:  # noqa: BLE001
        ev["errors"].append(f"funding analysis: {type(exc).__name__}")
        inp.flow.funding_checked_wallets = 0
        return
    inp.flow.funding_checked_wallets = res["checked"]
    inp.flow.creator_linked_buyers = res["creator_linked"] if res["checked"] else None
    inp.flow.related_wallet_groups = res["largest_group"] if res["checked"] else None
    ev["funding"] = {k: v for k, v in res.items() if k != "groups"} | {"groups": {f[:8] + "…": len(w) for f, w in res["groups"].items()}}


async def _creator_and_name(src: Sources, inp: AssessmentInput, c: Controls, meta: dict[str, str], mint: str,
                            curve_addr: str | None, now: datetime, ev: dict) -> None:
    """Creator history (on-chain launch count, stream behaviour) and the
    name filters' inputs. A failure leaves the history UNKNOWN, with why."""
    if c.settings.creator_history_check:
        try:
            h = await creator_history.creator_history(src.redis, src.rpc, inp.creator, mint, curve_addr, now)
        except Exception as exc:  # noqa: BLE001 - unavailable data, stated as UNKNOWN
            h = creator_history.CreatorHistory(inp.creator, creator_history.UNKNOWN, None, None, None, None, now.isoformat(),
                                               error=f"creator history: {type(exc).__name__}")
        inp.creator_history = h.to_dict()
        ev["creator_history"] = inp.creator_history
    if meta.get("name") is not None:
        inp.token_name = meta.get("name")
        if c.settings.skip_duplicate_names:
            inp.duplicate_of = await pump_stream.duplicate_of(src.redis, mint, inp.token_name)


async def _timed(ev: dict[str, Any], name: str, awaitable):
    """Awaits and records how long it took in ev["timings_ms"] (the
    decision's data-assembly latency, per source)."""
    t0 = time.monotonic()
    try:
        return await awaitable
    finally:
        timings = ev.setdefault("timings_ms", {})
        timings[name] = timings.get(name, 0) + int((time.monotonic() - t0) * 1000)


def _base_input(engine: str, strategy: str, mint: str, symbol: str, now: datetime, c: Controls) -> AssessmentInput:
    return AssessmentInput(
        engine=engine, strategy_name=strategy, asset_id=mint, symbol=symbol, now=now,
        market=None, token=None, holders=None, flow=None, account=c.account,
        overrides=c.overrides, global_mode=c.global_mode, strategy_mode=c.strategy_mode,
        live_trading_permitted=c.live_trading_permitted, manual_approval_granted=c.manual_approval_granted,
        fixed_cost_quote=c.fixed_cost_quote, fixed_cost_detail=c.fixed_cost_detail,
    )


async def assemble_fresh(src: Sources, mint: str, now: datetime, c: Controls,
                         engine: str = "solana_fresh") -> tuple[AssessmentInput, dict[str, Any]]:
    """Bonding-curve (pre-migration) token. Venue: the pump.fun curve itself,
    simulated with its exact constant-product model and the fee rates the
    stream's most recent trade reported. `engine` selects the strategy:
    solana_fresh (launch flow) or solana_momentum (acceleration)."""
    meta = await pump_stream.load_meta(src.redis, mint) or {}
    stream_curve = await pump_stream.load_curve(src.redis, mint)
    trades = await pump_stream.load_trades(src.redis, mint)
    hb = await pump_stream.heartbeat(src.redis)
    symbol = meta.get("symbol") or mint[:8]
    creator = meta.get("creator") or None
    curve_addr = meta.get("bonding_curve") or None
    ev: dict[str, Any] = {"errors": [], "stream_trades": len(trades), "stream_heartbeat": hb.isoformat() if hb else None,
                          "creator": creator}
    strategy = "fresh_launch_flow" if engine == "solana_fresh" else "solana_momentum"
    inp = _base_input(engine, strategy, mint, symbol, now, c)

    token, err = await _timed(ev, "mint_rpc", fetch_mint(src.rpc, mint, now))
    if err:
        ev["errors"].append(err)
    inp.token = token
    ev["token_decimals"] = token.decimals if token else None

    curve: BondingCurveState | None = None
    curve_obs: datetime | None = None
    if curve_addr:
        curve, err = await _timed(ev, "curve_rpc", fetch_curve(src.rpc, curve_addr))
        if err:
            ev["errors"].append(err)
        else:
            curve_obs = now
    else:
        ev["errors"].append("bonding curve address unknown (create event not seen by this stream)")
    if curve is None and stream_curve is not None:
        # Fall back to the stream's last trade reserves; the gate judges
        # their age like any other observation.
        curve = stream_curve.as_state()
        curve_obs = stream_curve.updated_at if curve else None
    if curve is not None and not curve.sol_quoted:
        ev["errors"].append("curve is not SOL-quoted; unsupported")
        curve = None

    fee_bps = stream_curve.fee_bps if stream_curve else None
    decimals = token.decimals if token else None
    created_at = int(meta["created_at"]) if meta.get("created_at") else None
    age = (now - datetime.fromtimestamp(created_at, tz=timezone.utc)).total_seconds() if created_at else None

    if curve is not None and decimals is not None:
        vest = volatility_estimate(trades, now, VOLATILITY_WINDOW_SECONDS, decimals)
        ev["volatility_source"], ev["volatility_confidence"] = vest["source"], vest["confidence"]
        inp.market = MarketInfo(
            observation=Observation("rpc:bonding_curve" if curve_obs == now else "pump_stream:curve", curve_obs),
            price=curve.price_sol(decimals),
            volatility=vest["value"],
            volatility_confidence=vest["confidence"], volatility_note=vest["source"],
            liquidity_quote=curve.real_liquidity_sol(),
            age_seconds=age,
            curve_complete=curve.complete,
            migrated=bool(stream_curve and stream_curve.pool),
            curve_progress=_curve_progress(curve.real_token_reserves, meta),
        )
        if not curve.has_reserves:
            # Migrated on chain (`migrate` empties the curve) before the stream
            # recorded the pool: there is no curve price (0/0) and nothing to
            # simulate. PRICE_UNAVAILABLE blocks; once the stream sees the
            # pool the migration engine evaluates the token with pool rules.
            ev["errors"].append("bonding curve has no reserves (complete and migrated on chain): curve price undefined; "
                                "waiting for the PumpSwap pool")
            ev["curve_state"] = "MIGRATED_ON_CHAIN"
        elif fee_bps is not None and not curve.complete:
            inp.liquidity_model = curve.model(decimals, fee_bps)
        elif fee_bps is None:
            ev["errors"].append("fee rate unknown (no trade event seen yet) — curve execution cannot be simulated")
        ev["curve"] = {
            "virtual_sol": curve.virtual_quote_reserves, "virtual_token": curve.virtual_token_reserves,
            "real_sol": curve.real_quote_reserves, "real_token": curve.real_token_reserves,
            "complete": curve.complete, "fee_bps": fee_bps,
        }

    inp.creator = creator
    inp.flow = trade_flow(trades, now, FLOW_WINDOW_SECONDS, creator, "pump_stream", hb)
    if engine == "solana_fresh":
        # Activity trend over the latest window (T0 / T+half / T+window), the
        # same comparison the funnel's observation window made before
        # promotion; the gate turns DETERIORATING into a WAIT.
        window = max(c.settings.fresh_observation_seconds, GATE_TREND_MIN_WINDOW_SECONDS)
        obs = observation.evaluate(mint, trades, created_at, now, replace(c.settings, fresh_observation_seconds=window),
                                   creator=creator, curve=stream_curve,
                                   initial_real_token_reserves=int(meta.get("initial_real_token_reserves") or 0) or None,
                                   monitoring_since=now)
        inp.observation = {"trend": obs.trend, "window_seconds": window, "positive": obs.positive, "negative": obs.negative,
                           "checkpoints": obs.checkpoints, "halves": obs.halves, "metrics": obs.metrics}
        ev["observation"] = inp.observation
    created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc) if created_at else None
    inp.flow.early_buy_share = early_buy_share(trades, created_dt, token.supply_raw if token else None) if engine == "solana_fresh" else None
    inp.flow.sync_buy_cluster = synchronized_buy_cluster(trades, now, FLOW_WINDOW_SECONDS)
    inp.flow.round_trip_share = round_trip_volume_share(trades, now, FLOW_WINDOW_SECONDS)
    inp.flow.creator_launches_24h = await pump_stream.creator_launches(src.redis, creator, now)
    await _timed(ev, "wallet_analysis", _wallet_analysis(src, inp, trades, creator, now, decimals, c, ev))
    await _timed(ev, "creator_and_name", _creator_and_name(src, inp, c, meta, mint, curve_addr, now, ev))
    inp.entry_exit_check = entry_exit_check(trades, now, creator, inp.market.liquidity_quote if inp.market else None,
                                            c.settings)
    ev["entry_exit_check"] = inp.entry_exit_check
    if decimals is not None:
        inp.entry_quality = deterioration(trades, now, DETERIORATION_WINDOW_SECONDS, decimals)
        ev["entry_quality"] = inp.entry_quality

    if token is not None and curve_addr:
        inp.holders, err = await _timed(ev, "holders_rpc", fetch_holders(src.rpc, mint, token.supply_raw, {curve_addr}, creator, now))
        if err:
            ev["errors"].append(err)

    if decimals is None:
        inp.signal = None
    elif engine == "solana_momentum":
        inp.signal = momentum_signal(trades, now, FLOW_WINDOW_SECONDS, decimals)
    else:
        inp.signal = fresh_launch_signal(inp.flow, trades, now, decimals)
    if inp.market is not None and inp.market.price and decimals is not None:
        inp.targets = TargetContext(resistance=recent_high_above(trades, now, RESISTANCE_WINDOW_SECONDS, decimals, inp.market.price),
                                    resistance_source="bonding-curve trades, last 30 min")
    _apply_controls(inp, c, meta.get("name"), meta.get("symbol"), meta.get("uri"), "FRESH")
    ev["source"], ev["lifecycle"] = "PUMPFUN", "FRESH"
    ev["features"] = {k: (str(v) if v is not None else None) for k, v in rule_features(inp).items()}
    return inp, ev


async def assemble_migrated(src: Sources, mint: str, now: datetime, c: Controls) -> tuple[AssessmentInput, dict[str, Any]]:
    """Migrated Pump.fun token trading on its canonical PumpSwap pool.

    Pump.fun-only by construction: the canonical pool is a PDA of the Pump
    program's pool authority for this mint, which only a Pump.fun migration
    creates. The pool is verified on chain (owner, mints, vault reserves),
    execution is simulated exactly with its constant-product math and the
    fee rate its own latest trade event charged, and trader wallets from
    its recent Buy/Sell events give wallet-level flow. A Jupiter quote, when
    configured, is recorded as an independent cross-check of both routes.
    """
    from yonixalpha_core.solana import pumpswap

    meta = await pump_stream.load_meta(src.redis, mint) or {}
    curve_trades = await pump_stream.load_trades(src.redis, mint)
    symbol = meta.get("symbol") or mint[:8]
    creator = meta.get("creator") or None
    ev: dict[str, Any] = {"errors": [], "source": "PUMPFUN", "lifecycle": "MIGRATED", "creator": creator}
    inp = _base_input("solana_migration", "post_migration_flow", mint, symbol, now, c)

    token, err = await _timed(ev, "mint_rpc", fetch_mint(src.rpc, mint, now))
    if err:
        ev["errors"].append(err)
    inp.token = token
    decimals = token.decimals if token else None
    ev["token_decimals"] = decimals

    pool_addr = pumpswap.canonical_pool(mint)
    ev["pool"] = {"address": pool_addr, "program": pumpswap.PUMP_AMM_PROGRAM}
    trades = []
    pool_state = None
    if decimals is not None:
        # The pool's trade history (getSignaturesForAddress + getTransaction)
        # and its state (the pool account and vaults) are separate lookups: a
        # failed history fetch leaves the flow unavailable, it does not erase
        # the pool's price and liquidity.
        pool_trades = []
        try:
            pool_trades = await _timed(ev, "pool_trades_rpc", pumpswap.recent_pool_trades(src.rpc, src.redis, pool_addr))
        except Exception as exc:  # noqa: BLE001 - RPC failure is data unavailability
            ev["errors"].append(f"pumpswap trade history unavailable: {type(exc).__name__}: {str(exc)[:200]}")
            ev["pool"]["trades_unavailable"] = True
        fee_bps = pool_trades[-1].fee_bps if pool_trades else None
        try:
            pool_state = await _timed(ev, "pool_state_rpc", pumpswap.fetch_pool(src.rpc, mint, now, fee_bps, decimals))
            trades = [pumpswap.as_flow_trade(t) for t in pool_trades]
            ev["pool"].update(verified=True, base_reserve_raw=pool_state.base_reserve_raw,
                              quote_reserve_lamports=pool_state.quote_reserve_lamports, fee_bps=fee_bps,
                              recent_trades=len(pool_trades), coin_creator=pool_state.account.coin_creator)
        except pumpswap.PoolUnavailable as exc:
            ev["errors"].append(f"pumpswap: {exc}")
            ev["pool"]["verified"] = False
        except Exception as exc:  # noqa: BLE001 - RPC failure is data unavailability
            ev["errors"].append(f"pumpswap rpc: {type(exc).__name__}: {exc}")
            ev["pool"]["verified"] = False

    inp.creator = creator
    if pool_state is not None:
        # Pool age is time since the migration event. The oldest of the last
        # 25 pool trades is NOT the pool's age: on a busy pool 25 trades span
        # seconds, which kept post_migration_signal waiting forever.
        stream_curve = await pump_stream.load_curve(src.redis, mint)
        pool_trades_at = [t.at for t in trades]
        if stream_curve is not None and stream_curve.migrated_at is not None:
            age = (now - stream_curve.migrated_at).total_seconds()
            ev["pool_age_source"] = "migration event timestamp"
        else:
            age = (now - min(pool_trades_at)).total_seconds() if pool_trades_at else None
            ev["pool_age_source"] = "oldest recent pool trade (lower bound: migration time unknown)"
        vest = volatility_estimate(trades or curve_trades, now, VOLATILITY_WINDOW_SECONDS, decimals)
        ev["volatility_source"] = ("pumpswap pool trades" if trades else "pre-migration bonding-curve trades") + f", {vest['source']}"
        ev["volatility_confidence"] = vest["confidence"]
        inp.market = MarketInfo(
            observation=Observation("rpc:pumpswap_pool", now),
            price=pool_state.price,
            volatility=vest["value"],
            volatility_confidence=vest["confidence"], volatility_note=ev["volatility_source"],
            liquidity_quote=pool_state.liquidity_sol,
            age_seconds=age,
            curve_complete=True,
            migrated=True,
        )
        if pool_state.fee_bps is not None:
            inp.liquidity_model = pool_state.model()
        else:
            ev["errors"].append("pool fee unknown (no trade event yet) — execution cannot be simulated")
        hb = now if trades else None
        inp.flow = trade_flow(trades, now, FLOW_WINDOW_SECONDS, creator, "rpc:pumpswap_events", hb)
        inp.flow.sync_buy_cluster = synchronized_buy_cluster(trades, now, FLOW_WINDOW_SECONDS)
        inp.flow.round_trip_share = round_trip_volume_share(trades, now, FLOW_WINDOW_SECONDS)
        inp.flow.creator_launches_24h = await pump_stream.creator_launches(src.redis, creator, now)
        await _timed(ev, "wallet_analysis", _wallet_analysis(src, inp, trades, creator, now, decimals, c, ev))
        # Same pool flow exit intelligence reads for a PumpSwap position.
        inp.entry_exit_check = entry_exit_check(trades, now, creator, pool_state.liquidity_sol, c.settings)
        ev["entry_exit_check"] = inp.entry_exit_check
        inp.entry_quality = deterioration(trades, now, DETERIORATION_WINDOW_SECONDS, decimals)
        ev["entry_quality"] = inp.entry_quality
        hour = [t for t in trades if (now - t.at).total_seconds() <= 3600]
        inp.signal = post_migration_signal(sum(1 for t in hour if t.is_buy), sum(1 for t in hour if not t.is_buy), age)
    else:
        # Curve completed but no verified pool: the gate reports MIGRATION_PENDING.
        inp.market = MarketInfo(observation=Observation("pump_stream", now), price=None, volatility=None,
                                liquidity_quote=None, age_seconds=None, curve_complete=True, migrated=False)

    await _timed(ev, "creator_and_name", _creator_and_name(src, inp, c, meta, mint, meta.get("bonding_curve") or None, now, ev))
    if pool_state is not None and c.settings.migrated_liquidity_check:
        inp.sol_usd, inp.sol_usd_source, errs = await _timed(ev, "sol_usd", sol_price.sol_usd(src.redis, src.jupiter, src.dexscreener, mint, now))
        ev["sol_usd"] = {"price": str(inp.sol_usd) if inp.sol_usd is not None else None, "source": inp.sol_usd_source}
        ev["errors"].extend(errs)

    if src.jupiter is not None and token is not None and pool_state is not None:
        quote, qev = await _timed(ev, "jupiter_quote", src.jupiter.execution_quote(
            mint, c.settings.max_position_size_quote, Decimal("0.01"), c.settings.max_slippage_bps
        ))
        ev["jupiter_cross_check"] = qev
        if quote is not None and (quote.buy_route_available is False or quote.sell_route_available is False):
            # An aggregator that can't route the pool is a warning sign; use its (failing) quote so the gate blocks.
            inp.quote = quote

    if token is not None and pool_state is not None:
        inp.holders, err = await _timed(ev, "holders_rpc", fetch_holders(src.rpc, mint, token.supply_raw, {pool_addr}, creator, now))
        if err:
            ev["errors"].append(err)

    if pool_state is not None and decimals is not None:
        inp.targets = TargetContext(resistance=recent_high_above(trades, now, RESISTANCE_WINDOW_SECONDS, decimals, pool_state.price),
                                    resistance_source="PumpSwap pool trades, last 30 min")
    _apply_controls(inp, c, meta.get("name"), meta.get("symbol"), meta.get("uri"), "MIGRATED")
    ev["features"] = {k: (str(v) if v is not None else None) for k, v in rule_features(inp).items()}
    return inp, ev
