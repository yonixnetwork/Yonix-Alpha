"""Management of gate-opened paper positions (PaperPosition.engine set) and
outcome tracking for opportunities the gate did not take.

Pricing, by venue recorded at entry:
- pump_curve, curve still trading: the stream's latest curve reserves.
  The curve price only moves when someone trades, so with a live stream
  the last reserves ARE the current price, and exits are simulated
  exactly against them.
- migrated (curve complete) or a quoted venue: a real Jupiter sell quote
  for the remaining quantity; its effective price already includes impact
  and fees, so no further exit cost is charged.
If neither is available now, the position is left untouched and the gap
is reported — never marked at a guessed price.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import paper_engine
from yonixalpha_core.db.models import PaperAccount, PaperPosition, RiskAssessment
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.market_data import JupiterClient
from yonixalpha_core.solana.pumpfun import WSOL_MINT

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


async def price_position(redis: Redis, jupiter: JupiterClient | None, p: PaperPosition, now: datetime):
    """Returns (price, model, exit_cost_bps, source) or (None, None, None, reason)."""
    venue = (p.plan or {}).get("venue") or {}
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
    if jupiter is None:
        return None, None, None, "migrated and no Jupiter client configured"
    qty = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    raw = int(qty * Decimal(10) ** int(decimals))
    q = await jupiter.quote(p.asset_id, WSOL_MINT, raw, 300)
    if q.status != "ok" or not q.out_amount:
        return None, None, None, f"jupiter sell quote: {q.status} {q.error or ''}".strip()
    price = (Decimal(q.out_amount) / LAMPORTS) / qty
    return price, None, Decimal(0), "jupiter:sell_quote"


async def manage_gate_positions(session_factory, redis: Redis, jupiter: JupiterClient | None, now: datetime) -> dict[str, int]:
    counts = {"managed": 0, "closed": 0, "unpriced": 0}
    async with session_factory() as session:
        ids = (await session.execute(
            select(PaperPosition.id).where(PaperPosition.status == "open", PaperPosition.engine.is_not(None))
        )).scalars().all()
    for pid in ids:
        # Per-position isolation: one failing row must never stop the other
        # positions' stops from being evaluated.
        try:
            async with session_factory() as session:
                p = await session.get(PaperPosition, pid)
                if p is None or p.status != "open":
                    continue
                price, model, exit_cost, source = await price_position(redis, jupiter, p, now)
                if price is None:
                    counts["unpriced"] += 1
                    log.warning("gate_manage.unpriced", position_id=str(pid), asset=p.asset_id, reason=source)
                    continue
                account = await session.get(PaperAccount, p.account_id)
                tfee = ((p.plan or {}).get("venue") or {}).get("transfer_fee_bps")
                result = await paper_engine.apply_step(
                    session, p, account, price, model, None if source.startswith("jupiter") else tfee, now,
                    exit_cost_bps=exit_cost,
                )
                await session.commit()
                counts["managed"] += 1
                if result.closed:
                    counts["closed"] += 1
                    log.info("gate_manage.closed", position_id=str(pid), reason=p.exit_reason, pnl=str(p.realized_pnl),
                             source=source)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] = counts.get("failed", 0) + 1
            log.error("gate_manage.position_failed", position_id=str(pid), error=str(exc))
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
