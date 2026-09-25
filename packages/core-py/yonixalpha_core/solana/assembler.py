"""Builds the safety gate's AssessmentInput for a pump.fun token from live
sources: the pump.fun event stream (Redis), Solana RPC (mint, bonding
curve, holders), and for migrated tokens Jupiter quotes and DexScreener.

Nothing is filled in when a source fails: the field stays None, the error
is recorded in the evidence, and the gate turns the gap into a NO_TRADE or
WAIT finding. The assembler never decides anything itself.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    GlobalMode,
    HolderInfo,
    ManualOverrides,
    MarketInfo,
    Observation,
    StrategyMode,
    TokenProgramInfo,
    TradeFlow,
)
from yonixalpha_core.safety.rules import BlacklistRule, CustomRule, evaluate_custom_rules, match_blacklist
from yonixalpha_core.safety.settings import SafetySettings
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.flow import realized_volatility, trade_flow
from yonixalpha_core.solana.market_data import DexScreenerClient, JupiterClient
from yonixalpha_core.solana.pumpfun import BondingCurveState, decode_bonding_curve
from yonixalpha_core.solana.token_safety import UnexpectedShape, parse_holders, parse_mint_account
from yonixalpha_core.strategies.solana import fresh_launch_signal, post_migration_signal

FLOW_WINDOW_SECONDS = 300
VOLATILITY_WINDOW_SECONDS = 900

# Numeric features a custom rule may reference. Anything else is refused at
# rule creation (see apps/api), so a rule can't silently never fire.
RULE_FIELDS = {
    "price", "liquidity_quote", "age_seconds", "volatility", "top1_share", "top10_share", "creator_share",
    "unique_buyers", "trade_count", "buy_sell_volume_ratio", "top3_wallet_volume_share", "transfer_fee_bps",
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


async def fetch_holders(
    rpc, mint: str, supply_raw: int, excluded_owners: set[str], creator: str | None, now: datetime
) -> tuple[HolderInfo | None, str | None]:
    try:
        largest = ((await rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])) or {}).get("value") or []
        addresses = [a["address"] for a in largest]
        owners: dict[str, str | None] = {}
        if addresses:
            accts = ((await rpc.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])) or {}).get("value") or []
            for addr, acct in zip(addresses, accts):
                info = (((acct or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                owners[addr] = info.get("owner")
        return parse_holders(largest, owners, supply_raw, excluded_owners, creator, now, "rpc:getTokenLargestAccounts"), None
    except (UnexpectedShape, ValueError, TypeError, KeyError) as exc:
        return None, f"holders: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"holders rpc: {exc}"


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
    }


def _apply_controls(inp: AssessmentInput, c: Controls, name: str | None, symbol: str | None) -> None:
    inp.blacklisted_by = match_blacklist(c.blacklist, inp.engine, name, symbol, inp.asset_id)
    inp.rule_actions = evaluate_custom_rules(c.custom_rules, inp.engine, rule_features(inp))


def _base_input(engine: str, strategy: str, mint: str, symbol: str, now: datetime, c: Controls) -> AssessmentInput:
    return AssessmentInput(
        engine=engine, strategy_name=strategy, asset_id=mint, symbol=symbol, now=now,
        market=None, token=None, holders=None, flow=None, account=c.account,
        overrides=c.overrides, global_mode=c.global_mode, strategy_mode=c.strategy_mode,
        live_trading_permitted=c.live_trading_permitted, manual_approval_granted=c.manual_approval_granted,
    )


async def assemble_fresh(src: Sources, mint: str, now: datetime, c: Controls) -> tuple[AssessmentInput, dict[str, Any]]:
    """Bonding-curve (pre-migration) token. Venue: the pump.fun curve itself,
    simulated with its exact constant-product model and the fee rates the
    stream's most recent trade reported."""
    meta = await pump_stream.load_meta(src.redis, mint) or {}
    stream_curve = await pump_stream.load_curve(src.redis, mint)
    trades = await pump_stream.load_trades(src.redis, mint)
    hb = await pump_stream.heartbeat(src.redis)
    symbol = meta.get("symbol") or mint[:8]
    creator = meta.get("creator") or None
    curve_addr = meta.get("bonding_curve") or None
    ev: dict[str, Any] = {"errors": [], "stream_trades": len(trades), "stream_heartbeat": hb.isoformat() if hb else None}
    inp = _base_input("solana_fresh", "fresh_launch_flow", mint, symbol, now, c)

    token, err = await fetch_mint(src.rpc, mint, now)
    if err:
        ev["errors"].append(err)
    inp.token = token
    ev["token_decimals"] = token.decimals if token else None

    curve: BondingCurveState | None = None
    curve_obs: datetime | None = None
    if curve_addr:
        curve, err = await fetch_curve(src.rpc, curve_addr)
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
        inp.market = MarketInfo(
            observation=Observation("rpc:bonding_curve" if curve_obs == now else "pump_stream:curve", curve_obs),
            price=curve.price_sol(decimals),
            volatility=realized_volatility(trades, now, VOLATILITY_WINDOW_SECONDS, decimals),
            liquidity_quote=curve.real_liquidity_sol(),
            age_seconds=age,
            curve_complete=curve.complete,
            migrated=bool(stream_curve and stream_curve.pool),
        )
        if fee_bps is not None and not curve.complete:
            inp.liquidity_model = curve.model(decimals, fee_bps)
        elif fee_bps is None:
            ev["errors"].append("fee rate unknown (no trade event seen yet) — curve execution cannot be simulated")
        ev["curve"] = {
            "virtual_sol": curve.virtual_quote_reserves, "virtual_token": curve.virtual_token_reserves,
            "real_sol": curve.real_quote_reserves, "real_token": curve.real_token_reserves,
            "complete": curve.complete, "fee_bps": fee_bps,
        }

    inp.flow = trade_flow(trades, now, FLOW_WINDOW_SECONDS, creator, "pump_stream", hb)

    if token is not None and curve_addr:
        inp.holders, err = await fetch_holders(src.rpc, mint, token.supply_raw, {curve_addr}, creator, now)
        if err:
            ev["errors"].append(err)

    inp.signal = fresh_launch_signal(inp.flow, trades, now, decimals) if decimals is not None else None
    _apply_controls(inp, c, meta.get("name"), meta.get("symbol"))
    ev["features"] = {k: (str(v) if v is not None else None) for k, v in rule_features(inp).items()}
    return inp, ev


async def assemble_migrated(src: Sources, mint: str, now: datetime, c: Controls) -> tuple[AssessmentInput, dict[str, Any]]:
    """Migrated token trading on PumpSwap/other AMMs. Execution is measured
    with real Jupiter quotes in both directions; pool state comes from
    DexScreener, whose transaction counts carry no wallet identities, so the
    gate requires operator approval for these (NO_WALLET_DATA)."""
    meta = await pump_stream.load_meta(src.redis, mint) or {}
    stream_curve = await pump_stream.load_curve(src.redis, mint)
    trades = await pump_stream.load_trades(src.redis, mint)
    symbol = meta.get("symbol") or mint[:8]
    creator = meta.get("creator") or None
    ev: dict[str, Any] = {"errors": []}
    inp = _base_input("solana_migration", "post_migration_flow", mint, symbol, now, c)

    token, err = await fetch_mint(src.rpc, mint, now)
    if err:
        ev["errors"].append(err)
    inp.token = token
    ev["token_decimals"] = token.decimals if token else None

    pool = None
    if src.dexscreener is not None:
        pool, err = await src.dexscreener.pool(mint)
        if err:
            ev["errors"].append(f"dexscreener: {err}")
    else:
        ev["errors"].append("dexscreener client not configured")

    pool_addr = (stream_curve.pool if stream_curve else None) or (pool.pair_address if pool else None)
    if pool is not None and token is not None:
        age = (now - pool.pair_created_at).total_seconds() if pool.pair_created_at else None
        # Volatility from the pre-migration curve trades: the only per-trade
        # prices this system observes for the token. Recorded as such.
        vol = realized_volatility(trades, now, VOLATILITY_WINDOW_SECONDS, token.decimals)
        ev["volatility_source"] = "pre-migration bonding-curve trades" if vol is not None else None
        inp.market = MarketInfo(
            observation=Observation("dexscreener", pool.observed_at),
            price=pool.price_sol,
            volatility=vol,
            liquidity_quote=pool.liquidity_sol,
            age_seconds=age,
            curve_complete=True,
            migrated=True,
        )
        buys, sells = pool.buys_m5, pool.sells_m5
        inp.flow = TradeFlow(
            observation=Observation("dexscreener", pool.observed_at),
            window_seconds=300,
            trade_count=(buys or 0) + (sells or 0),
            buy_count=buys or 0,
            sell_count=sells or 0,
            unique_buyers=None,
            unique_sellers=None,
            buy_volume_quote=Decimal(0),
            sell_volume_quote=Decimal(0),
            top3_wallet_volume_share=None,
            creator_sold=None,
            wallet_level=False,
        )
        ev["pool"] = {"pair": pool.pair_address, "dex": pool.dex_id, "liquidity_sol": str(pool.liquidity_sol),
                      "price_sol": str(pool.price_sol), "buys_h1": pool.buys_h1, "sells_h1": pool.sells_h1}
        inp.signal = post_migration_signal(pool.buys_h1, pool.sells_h1, age)
    elif stream_curve is not None:
        # Curve done, no pool data yet: the gate reports MIGRATION_PENDING.
        inp.market = MarketInfo(
            observation=Observation("pump_stream:curve", stream_curve.updated_at),
            price=None, volatility=None, liquidity_quote=None, age_seconds=None,
            curve_complete=True, migrated=False,
        )

    if src.jupiter is not None and token is not None and pool is not None:
        quote, qev = await src.jupiter.execution_quote(
            mint, c.settings.max_position_size_quote, Decimal("0.01"), c.settings.max_slippage_bps
        )
        inp.quote = quote
        ev["jupiter"] = qev
    elif src.jupiter is None:
        ev["errors"].append("jupiter client not configured")

    if token is not None and pool_addr:
        inp.holders, err = await fetch_holders(src.rpc, mint, token.supply_raw, {pool_addr}, creator, now)
        if err:
            ev["errors"].append(err)

    _apply_controls(inp, c, meta.get("name"), meta.get("symbol"))
    ev["features"] = {k: (str(v) if v is not None else None) for k, v in rule_features(inp).items()}
    return inp, ev
