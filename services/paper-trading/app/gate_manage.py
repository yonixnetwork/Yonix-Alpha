"""Management of gate-opened paper positions (PaperPosition.engine set) and
outcome tracking for opportunities the gate did not take.

Pricing, by venue recorded at entry:
- pump_curve, curve still trading: the stream's latest curve reserves.
  The curve price only moves when someone trades, so with a live stream
  the last reserves ARE the current price, and exits are simulated
  exactly against them.
- pumpswap_pool, or a pump_curve position whose curve has completed (the
  token migrated): the canonical PumpSwap pool, read from chain — vault
  reserves + virtual quote reserves, and the fee charged by the pool's
  latest trade event — so exits are simulated against the same AMM the
  live path sells into.
- otherwise (no RPC configured, or a quoted venue): a real Jupiter sell
  quote for the remaining quantity; its effective price already includes
  impact and fees, so no further exit cost is charged.

LIVE positions (execution_mode == "LIVE") use the same pricing and the
same exit decisions, but an exit becomes a SELL order for the live worker
(live_trading.manage_live_position); the position only changes when the
on-chain fill is confirmed.
If neither is available now, the position is left untouched and the gap
is reported — never marked at a guessed price.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, live_trading, paper_engine, paper_execution
from yonixalpha_core.db.models import PaperAccount, PaperPosition, RiskAssessment
from yonixalpha_core.execution.registry import FUTURES_PROVIDERS
from yonixalpha_core.exit_intel import ExitConfig, solana_exit_decision
from yonixalpha_core.safety.store import add_timeline_event, load_settings
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.solana import pump_stream, pumpswap
from yonixalpha_core.solana.assembler import fetch_holders
from yonixalpha_core.solana.market_data import JupiterClient
from yonixalpha_core.solana.pumpfun import WSOL_MINT
from yonixalpha_core.venues.common import VenueError
from yonixalpha_core.notify import alert_error

log = get_logger("paper-trading.gate_manage")

LAMPORTS = Decimal(1_000_000_000)
MAX_STREAM_AGE_SECONDS = 60
OUTCOME_DELAY = timedelta(minutes=15)
OUTCOME_HORIZON = timedelta(hours=6)
NOT_TAKEN = ["REJECT", "NO_TRADE", "WAIT", "REQUIRE_MANUAL_APPROVAL"]


def curve_price_and_model(curve: pump_stream.StreamCurve, decimals: int) -> tuple[Decimal, ConstantProductModel | None]:
    scale = Decimal(10) ** decimals
    q, t = Decimal(curve.vsol) / LAMPORTS, Decimal(curve.vtok) / scale
    price = q / t
    if curve.fee_bps is None:
        return price, None
    real = Decimal(curve.rsol) / LAMPORTS if curve.rsol is not None else None
    return price, ConstantProductModel(q, t, Decimal(curve.fee_bps), real)


async def pool_price(rpc, redis: Redis, mint: str, decimals: int, now: datetime):
    """(price, model, flow trades) from the canonical PumpSwap pool. The
    model is None when the pool's fee is not yet known (no trade seen)."""
    trades = await pumpswap.recent_pool_trades(rpc, redis, pumpswap.canonical_pool(mint), limit=15)
    fee = trades[-1].fee_bps if trades else None
    state = await pumpswap.fetch_pool(rpc, mint, now, fee, decimals)
    model = state.model() if fee is not None else None
    return state.price, model, [pumpswap.as_flow_trade(t) for t in trades]


async def price_position(redis: Redis, jupiter: JupiterClient | None, p: PaperPosition, now: datetime, venues: dict | None = None,
                         rpc=None, ctx: dict | None = None):
    """Returns (price, model, exit_cost_bps, source) or (None, None, None, reason).
    `ctx` receives the pool's recent trades when priced from PumpSwap."""
    venue = (p.plan or {}).get("venue") or {}
    if venue.get("kind") == "futures":
        adapter = (venues or {}).get(venue.get("venue"))
        if adapter is None:
            return None, None, None, f"venue {venue.get('venue')} not configured"
        try:
            book = await adapter.book(venue.get("symbol") or p.asset_id)
        except VenueError as exc:
            return None, None, None, f"order book unavailable: {exc}"
        return book.mid, book, None, f"{venue.get('venue')}:book"
    decimals = venue.get("decimals")
    if decimals is None:
        return None, None, None, "token decimals unknown"
    if venue.get("type") == "pump_curve":
        hb = await pump_stream.heartbeat(redis)
        if hb is None or (now - hb).total_seconds() > MAX_STREAM_AGE_SECONDS:
            return None, None, None, "pump.fun stream stale"
        curve = await pump_stream.load_curve(redis, p.asset_id)
        if curve is None:
            return None, None, None, "no curve state in stream store"
        if not curve.complete:
            price, model = curve_price_and_model(curve, int(decimals))
            return price, model, None, "pump_stream:curve"
    if venue.get("type") in ("pump_curve", "pumpswap_pool") and rpc is not None:
        try:
            price, model, trades = await pool_price(rpc, redis, p.asset_id, int(decimals), now)
        except pumpswap.PoolUnavailable as exc:
            return None, None, None, f"pumpswap pool: {exc}"
        if model is None:
            return None, None, None, "pumpswap pool fee unknown (no trade event yet)"
        if ctx is not None:
            ctx["pool_trades"] = trades
        return price, model, None, "rpc:pumpswap_pool"
    if venue.get("type") == "pumpswap_pool":
        return None, None, None, "pumpswap position and no Solana RPC configured"
    if jupiter is None:
        return None, None, None, "migrated and no Jupiter client configured"
    qty = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    raw = int(qty * Decimal(10) ** int(decimals))
    q = await jupiter.quote(p.asset_id, WSOL_MINT, raw, 300)
    if q.status != "ok" or not q.out_amount:
        return None, None, None, f"jupiter sell quote: {q.status} {q.error or ''}".strip()
    price = (Decimal(q.out_amount) / LAMPORTS) / qty
    return price, None, Decimal(0), "jupiter:sell_quote"


HOLDER_CHECK_SECONDS = 60
MAX_SIMULATED_EXIT_FAILURES = 5  # a misconfigured rate can delay an exit, never block it


async def holders_now(rpc, redis: Redis, p: PaperPosition, now: datetime) -> dict | None:
    """Current holder shares for an open position, re-read from chain at
    most once per HOLDER_CHECK_SECONDS (cached in Redis between reads).
    None when there is no RPC, no entry snapshot, or the read failed."""
    venue = (p.plan or {}).get("venue") or {}
    if rpc is None or not venue.get("holders_at_entry") or not venue.get("supply_raw"):
        return None
    key = f"yx:holders:{p.id}"
    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached)
    h, err = await fetch_holders(rpc, p.asset_id, int(venue["supply_raw"]), set(venue.get("holder_excluded") or []),
                                 venue.get("creator"), now)
    if h is None:
        log.warning("gate_manage.holders_unavailable", position_id=str(p.id), error=err)
        return None
    snap = {"top1_share": str(h.top1_share), "top10_share": str(h.top10_share),
            "creator_share": str(h.creator_share) if h.creator_share is not None else None, "at": now.isoformat()}
    await redis.set(key, json.dumps(snap), ex=HOLDER_CHECK_SECONDS)
    return snap


def _would_exit(p: PaperPosition, price: Decimal, extra) -> tuple[bool, paper_engine.PositionState]:
    s = paper_engine.state_of(p)
    r = paper_engine.manage_step(s, price, exit_now=bool(p.exit_requested))
    return bool(r.exits) or bool(extra and extra[0] > 0), s


NOTIFY_KIND = {"take_profit_1": "tp1", "take_profit_2": "tp2", "take_profit_3": "tp3", "stop_loss": "stop_loss",
               "trailing_stop": "trailing_stop"}
REDUCE_COOLDOWN_SECONDS = 300


async def _exit_intelligence(redis: Redis, p: PaperPosition, model, now: datetime, session,
                             pool_trades: list | None = None, rpc=None, price: Decimal | None = None) -> tuple | None:
    """For curve and PumpSwap positions: HOLD / REDUCE / EXIT from flow,
    liquidity and creator behaviour. Returns an `extra_exit`, or None."""
    venue = (p.plan or {}).get("venue") or {}
    if venue.get("type") not in ("pump_curve", "pumpswap_pool") or model is None or p.side != "LONG":
        return None
    trades = pool_trades if pool_trades is not None else await pump_stream.load_trades(redis, p.asset_id)
    entry_liq = Decimal(venue["real_liquidity_at_entry"]) if venue.get("real_liquidity_at_entry") else None
    holders = await holders_now(rpc, redis, p, now)
    settings, _ = await load_settings(session, p.engine) if p.engine else (None, None)
    d = solana_exit_decision(trades, now, venue.get("creator"), entry_liq, model.liquidity_quote,
                             venue.get("holders_at_entry"), holders,
                             cfg=ExitConfig.from_settings(settings) if settings is not None else None,
                             highest_price=max(p.highest_price or Decimal(0), price or Decimal(0)) or None, current_price=price)
    if d.action == "HOLD":
        return None
    remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    if d.action == "REDUCE":
        if not await redis.set(f"yx:exit_reduce:{p.id}", "1", nx=True, ex=REDUCE_COOLDOWN_SECONDS):
            return None
        qty = remaining * d.fraction
    else:
        qty = remaining
    await add_timeline_event(session, f"exit_intelligence.{d.action.lower()}", now, {"reasons": d.reasons, **d.metrics},
                             candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    return qty, f"exit_intel_{d.action.lower()}"


async def _manage_live(session, redis: Redis, app_settings, p: PaperPosition, price, model, extra, now: datetime,
                       source: str) -> None:
    out = await live_trading.manage_live_position(session, p, price, model, now, extra)
    if out["requested"]:
        await events.notify(session, redis, app_settings, NOTIFY_KIND.get(out["requested"], "close"),
                            f"LIVE exit requested: {p.symbol}", f"{out['requested'].replace('_', ' ')} at {price}", "warning",
                            {"position_id": str(p.id)})
    await session.commit()
    await events.publish(redis, "position.updated", {"position_id": str(p.id), "price": str(price), "source": source,
                                                     "exit_requested": out["requested"]}, "live")


async def _note_migration(session, p: PaperPosition, source: str, now: datetime) -> None:
    """A position bought on the bonding curve whose token has migrated: the
    same position, now in its POST_MIGRATION market state. Its price
    already comes from the PumpSwap pool (price_position); a live sell must
    go there too — a bonding-curve sell of a completed curve cannot fill."""
    venue = (p.plan or {}).get("venue") or {}
    if venue.get("type") != "pump_curve" or source != "rpc:pumpswap_pool" or p.lifecycle == "MIGRATED":
        return
    before = p.execution_route
    p.lifecycle = "MIGRATED"
    p.pool = pumpswap.canonical_pool(p.asset_id)
    if p.execution_mode == "LIVE":
        p.execution_route = "pump-amm"
    await add_timeline_event(session, "position_migrated", now,
                             {"market_state": "POST_MIGRATION", "previous_market_state": "PRE_MIGRATION",
                              "pool": p.pool, "route_before": before, "route_after": p.execution_route,
                              "price_source": source},
                             candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    log.info("gate_manage.position_migrated", position_id=str(p.id), mint=p.asset_id, route=p.execution_route)


async def manage_gate_positions(session_factory, redis: Redis, jupiter: JupiterClient | None, now: datetime,
                                venues: dict | None = None, app_settings=None, rpc=None) -> dict[str, int]:
    counts = {"managed": 0, "closed": 0, "unpriced": 0}
    rates = None  # paper execution failure rates, loaded once per pass when needed
    async with session_factory() as session:
        # pending_entry (LIVE buy not yet confirmed) and needs_review
        # positions are not "open" and are therefore never managed here.
        ids = (await session.execute(
            select(PaperPosition.id).where(
                PaperPosition.status == "open", PaperPosition.engine.is_not(None),
                # LIVE futures/FX positions are managed by services/execution-futures.
                or_(PaperPosition.execution_provider.is_(None),
                    PaperPosition.execution_provider.not_in(FUTURES_PROVIDERS)))
        )).scalars().all()
    for pid in ids:
        # Per-position isolation: one failing row must never stop the other
        # positions' stops from being evaluated.
        try:
            async with session_factory() as session:
                p = await session.get(PaperPosition, pid)
                if p is None or p.status != "open":
                    continue
                ctx: dict = {}
                price, model, exit_cost, source = await price_position(redis, jupiter, p, now, venues, rpc, ctx)
                if price is None:
                    counts["unpriced"] += 1
                    log.warning("gate_manage.unpriced", position_id=str(pid), asset=p.asset_id, reason=source)
                    continue
                await _note_migration(session, p, source, now)
                extra = None if p.exit_requested or p.management_paused else await _exit_intelligence(
                    redis, p, model, now, session, ctx.get("pool_trades"), rpc, price)
                if p.execution_mode == "LIVE":  # a paused position still honours its stop (manage_step)
                    await _manage_live(session, redis, app_settings, p, price, model, extra, now, source)
                    counts["managed"] += 1
                    continue
                if rates is None:
                    rates = await paper_execution.effective_rates(session)
                if rates["exit_pct"] > 0 and p.exit_failures < MAX_SIMULATED_EXIT_FAILURES:
                    exiting, s = _would_exit(p, price, extra)
                    key = f"exit:{p.id}:{len(p.tp_hits or [])}:{p.exit_failures}"
                    if exiting and paper_execution.simulated_failure(key, rates["exit_pct"]):
                        # As with a live sell that never lands: the position stays
                        # open, the decision state moves on, and the exit is
                        # attempted again next tick at that tick's price.
                        p.highest_price, p.lowest_price = s.highest_price, s.lowest_price
                        p.trailing_stop, p.stop_loss = s.trailing_stop, s.stop_loss
                        p.last_price, p.last_marked_at = price, now
                        p.exit_failures += 1
                        await add_timeline_event(session, "paper_exit_failed", now,
                                                 {"simulated": True, "price": str(price), "attempt": p.exit_failures,
                                                  "failure_pct": str(rates["exit_pct"]), "source": rates["exit_source"]},
                                                 candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
                        await session.commit()
                        counts["exit_failed"] = counts.get("exit_failed", 0) + 1
                        await events.publish(redis, "position.updated", {"position_id": str(p.id), "price": str(price),
                                                                         "exit_failed": True}, "paper")
                        continue
                account = await session.get(PaperAccount, p.account_id)
                tfee = ((p.plan or {}).get("venue") or {}).get("transfer_fee_bps")
                result = await paper_engine.apply_step(
                    session, p, account, price, model, None if source.startswith("jupiter") else tfee, now,
                    exit_cost_bps=exit_cost, extra_exit=extra,
                )
                if result.exits:
                    p.exit_failures = 0
                for _, reason in result.exits:
                    kind = NOTIFY_KIND.get(reason)
                    if kind:
                        await events.notify(session, redis, app_settings, kind, f"{p.symbol}: {reason.replace('_', ' ')}",
                                            f"price {price}", "info", {"position_id": str(p.id)})
                if result.closed:
                    await events.notify(session, redis, app_settings, "close", f"Paper position closed: {p.symbol}",
                                        f"{p.exit_reason}, realized {p.realized_pnl:.6f}", "info", {"position_id": str(p.id)})
                await session.commit()
                counts["managed"] += 1
                if result.exits:
                    await events.publish(redis, "trade.closed" if result.closed else "trade.updated",
                                         {"position_id": str(p.id), "symbol": p.symbol, "exits": [r for _, r in result.exits],
                                          "realized_pnl": str(p.realized_pnl) if p.realized_pnl is not None else None}, "paper")
                    await events.publish(redis, "balance.updated", {"account_id": str(p.account_id)}, "paper")
                else:
                    await events.publish(redis, "position.updated", {"position_id": str(p.id), "price": str(price)}, "paper")
                if result.closed:
                    counts["closed"] += 1
                    log.info("gate_manage.closed", position_id=str(pid), reason=p.exit_reason, pnl=str(p.realized_pnl),
                             source=source)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] = counts.get("failed", 0) + 1
            log.error("gate_manage.position_failed", position_id=str(pid), error=str(exc))
            await alert_error("paper-trading", "gate_manage.position_failed",
                              {"position_id": str(pid), "error": f"{type(exc).__name__}: {exc}"})
    return counts


async def _price_now(redis: Redis, mint: str, decimals: int) -> tuple[Decimal | None, dict]:
    curve = await pump_stream.load_curve(redis, mint)
    if curve is None:
        return None, {"reason": "no curve state"}
    price, _ = curve_price_and_model(curve, decimals)
    return price, {"graduated": curve.complete, "curve_updated_at": curve.updated_at.isoformat()}


async def track_outcomes(session: AsyncSession, redis: Redis, now: datetime, limit: int = 50) -> int:
    """For opportunities the gate did not take, records what the price did
    afterwards (latest assessment per token, 15 min to 6 h later). This is
    for reviewing rejections, not for training: it ignores costs and says
    nothing about whether a trade would have been exitable."""
    rows = (await session.execute(
        select(RiskAssessment)
        .where(RiskAssessment.outcome.is_(None), RiskAssessment.decision.in_(NOT_TAKEN),
               RiskAssessment.engine == "solana_fresh",
               RiskAssessment.evaluated_at <= now - OUTCOME_DELAY, RiskAssessment.evaluated_at >= now - OUTCOME_HORIZON)
        .order_by(RiskAssessment.asset_id, RiskAssessment.evaluated_at.desc())
        .distinct(RiskAssessment.asset_id)
        .limit(limit)
    )).scalars().all()
    done = 0
    for row in rows:
        snapshot = row.assessment.get("inputs_snapshot") or {}
        before = (snapshot.get("features") or {}).get("price")
        decimals = snapshot.get("token_decimals")
        if decimals is None:
            after, detail = None, {"reason": "token decimals were not observed at decision time"}
        else:
            after, detail = await _price_now(redis, row.asset_id, int(decimals))
        outcome = {"measured_at": now.isoformat(), "after_seconds": int((now - row.evaluated_at).total_seconds()),
                   "price_at_decision": before, "price_after": str(after) if after is not None else None,
                   "source": "pump_stream curve", **detail}
        if before and after is not None and Decimal(before) > 0:
            outcome["change_pct"] = str((after / Decimal(before) - 1).quantize(Decimal("0.0001")))
        row.outcome = outcome
        await session.execute(
            update(RiskAssessment)
            .where(RiskAssessment.asset_id == row.asset_id, RiskAssessment.outcome.is_(None), RiskAssessment.id != row.id,
                   RiskAssessment.evaluated_at <= row.evaluated_at)
            .values(outcome={"superseded_by": str(row.id)})
        )
        done += 1
    await session.commit()
    return done
