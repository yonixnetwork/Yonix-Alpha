"""Manual trading from the dashboard: BUY on a Fresh / Observation /
Migrated / Momentum token and SELL on an open position.

BUY never executes here: the request is queued for the decision engine,
which runs the full safety gate and the normal execution path
(yonixalpha_core.manual_trade). SELL uses the existing operator exit
(exit_requested), which the position manager executes on the position's
current route (switched to PumpSwap automatically after a migration)."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import events, live_smoke, live_trading, manual_trade
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, RiskAssessment
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode
from yonixalpha_core.solana import pump_stream

router = APIRouter(prefix="/trade", tags=["trade"])
SOURCES = {"fresh", "observation", "migrated", "momentum", "candidates", "token"}
PUMP_DECIMALS = 6


class BuyIn(BaseModel):
    mint: str = Field(min_length=32, max_length=44)
    engine: str | None = None  # solana_fresh / solana_momentum; migrated tokens always use solana_migration
    source: str = "token"
    confirm: bool = False


def _curve_price(curve) -> Decimal | None:
    if curve is None or curve.vsol <= 0 or curve.vtok <= 0:
        return None  # a migrated curve has no reserves: no price, never 0
    return (Decimal(curve.vsol) / Decimal(10**9)) / (Decimal(curve.vtok) / Decimal(10**PUMP_DECIMALS))


@router.get("/preview")
async def preview(mint: str, engine: str | None = None, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                  settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """Everything the confirmation dialog shows. Values come from the latest
    stream data and the latest gate assessment; the gate runs again on
    confirm and its result is what executes."""
    if not manual_trade.BASE58.match(mint):
        raise HTTPException(422, "not a valid Solana mint address")
    now = datetime.now(timezone.utc)
    curve = await pump_stream.load_curve(redis, mint)
    meta = await pump_stream.load_meta(redis, mint) or {}
    if curve is None and not meta:
        raise HTTPException(404, "this token is not in the Pump.fun stream data")
    resolved = manual_trade.resolve_engine(curve.pool if curve else None, engine)
    global_mode = await store.load_global_mode(db)
    live_target = global_mode == GlobalMode.LIVE and store.live_trading_permitted(settings)
    safety, _meta = await store.load_settings(db, resolved)
    if live_target:
        ready, reason = await live_trading.live_readiness(redis, settings, now)
        wallet = await redis.get(live_trading.WALLET_KEY)
        w = json.loads(wallet) if wallet else None
        reserve = (await live_trading.load_live_settings(db)).min_sol_reserve
        balance = {"kind": "LIVE wallet", "sol": w.get("sol") if w else None, "at": w.get("at") if w else None,
                   "available_sol": str(max(Decimal(0), Decimal(w["sol"]) - reserve)) if w else None,
                   "live_ready": ready, "live_not_ready_reason": reason}
    else:
        acct = await store.get_paper_account(db, "solana")
        balance = {"kind": "PAPER account", "sol": str(acct.cash_balance), "available_sol": str(acct.cash_balance), "at": None}
    a = (await db.execute(select(RiskAssessment).where(RiskAssessment.asset_id == mint)
                          .order_by(RiskAssessment.evaluated_at.desc()).limit(1))).scalar_one_or_none()
    latest = None
    if a is not None:
        plan = (a.assessment or {}).get("plan") or {}
        blockers = [f"{f.get('code')}: {f.get('message')}" for f in (a.assessment or {}).get("findings", [])
                    if f.get("action") in ("REJECT", "NO_TRADE") and f.get("category") not in ("STRATEGY", "ML")]
        latest = {"evaluated_at": a.evaluated_at, "age_seconds": round((now - a.evaluated_at).total_seconds(), 1),
                  "decision": a.decision, "status": a.status_label, "overall_risk": a.overall_risk, "engine": a.engine,
                  "planned_size_sol": (plan.get("position_size") or {}).get("value"),
                  "max_loss_sol": (plan.get("max_loss") or {}).get("value"),
                  "stop_loss": (plan.get("stop_loss") or {}).get("value"), "safety_blockers": blockers[:10]}
    price = _curve_price(curve) if resolved != "solana_migration" else None
    est_qty = (Decimal(latest["planned_size_sol"]) / price) if latest and latest["planned_size_sol"] and price else None
    return jsonable({
        "mint": mint, "symbol": meta.get("symbol"), "name": meta.get("name"),
        "engine": resolved, "route": manual_trade.ROUTE_LABEL[resolved],
        "migration_state": "MIGRATED (PumpSwap pool)" if curve and curve.pool else
                           ("CURVE COMPLETE — migrating" if curve and curve.complete else "BONDING CURVE (not migrated)"),
        "current_price_sol": str(price) if price is not None else None,
        "price_source": "pump.fun stream (bonding curve)" if price is not None else
                        "pool price is read by the gate at execution" if resolved == "solana_migration" else "unavailable",
        "price_at": curve.updated_at if curve else None,
        "execution_mode": "LIVE" if live_target else "PAPER", "global_mode": global_mode.value,
        "balance": balance, "slippage_limit_pct": str(Decimal(safety.max_slippage_bps) / 100),
        "latest_assessment": latest, "estimated_quantity": str(est_qty) if est_qty is not None else None,
        "note": "Pressing CONFIRM BUY runs the full safety gate again. The size is the gate's plan (never more), the "
                "strategy signal is not required, and every safety check (sellability, token risk, liquidity, route, "
                "stale data, wallet, risk limits) still applies. If one blocks, nothing is bought and the reason is shown.",
    })


@router.post("/buy")
async def buy(body: BuyIn, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
              username: str = Depends(get_current_username)) -> dict:
    if not body.confirm:
        raise HTTPException(422, "confirmation required (confirm: true)")
    if body.source not in SOURCES:
        raise HTTPException(422, f"source must be one of {sorted(SOURCES)}")
    try:
        req = await manual_trade.create_request(db, redis, body.mint, body.engine, body.source, username)
    except manual_trade.ManualTradeError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit(db, username, request, "manual_trade.buy_requested",
                {"mint": body.mint, "engine": req["engine"], "route": req["route"], "request": req["id"], "source": body.source})
    await db.commit()
    return req


async def _live_progress(db: AsyncSession, req: dict) -> dict:
    """For a LIVE buy, status from the real order and position rows."""
    pid = req.get("position_id")
    if not pid or req.get("target") != "LIVE":
        return req
    p = await db.get(PaperPosition, UUID(pid))
    orders = (await db.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == UUID(pid))
                               .order_by(ExecutionOrder.created_at))).scalars().all()
    buy = next((o for o in orders if o.side == "BUY"), None)
    decimals = ((p.plan or {}).get("venue") or {}).get("decimals") if p else None
    view = live_smoke.order_view(buy, p.entry_price if p else None, decimals) if buy else None
    status = req["status"]
    if buy is not None:
        if buy.status == "PENDING":
            status = "SUBMITTING"
        elif buy.status in ("SIGNED", "SUBMITTED"):
            status = "CONFIRMING"
        elif buy.status == "CONFIRMED":
            status = "POSITION_OPEN" if p is not None and p.status == "open" else "CONFIRMED"
        else:
            status = "FAILED"
    return {**req, "status": status, "order": view, "failure_stage": (view or {}).get("failure_stage"),
            "position": live_smoke.position_view(p, datetime.now(timezone.utc)) if p else None}


@router.get("/requests/{request_id}")
async def request_status(request_id: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                         _: str = Depends(get_current_username)) -> dict:
    req = await manual_trade.get(redis, request_id)
    if req is None:
        raise HTTPException(404, "request not found (requests are kept for 24 h)")
    return jsonable(await _live_progress(db, req))


@router.get("/requests")
async def recent_requests(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                          _: str = Depends(get_current_username)) -> list[dict]:
    out = []
    for rid in await redis.lrange(manual_trade.RECENT, 0, 19):
        req = await manual_trade.get(redis, rid)
        if req:
            out.append(await _live_progress(db, req))
    return jsonable(out)


@router.post("/sell/{position_id}")
async def sell(position_id: UUID, request: Request, confirm: bool = False, db: AsyncSession = Depends(get_db),
               redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Manual SELL of the whole open position through the normal exit path
    (current route, slippage limit, transaction guard). Works whether or
    not any automatic exit is triggering."""
    if not confirm:
        raise HTTPException(422, "confirmation required (?confirm=true)")
    p = await db.get(PaperPosition, position_id)
    if p is None:
        raise HTTPException(404, "position not found")
    if p.status != "open":
        raise HTTPException(409, f"position is {p.status}")
    if p.exit_requested:
        raise HTTPException(409, "a sell is already requested for this position")
    now = datetime.now(timezone.utc)
    p.exit_requested = True
    await store.add_timeline_event(db, "operator_exit", now, {"by": username, "note": "manual SELL from the dashboard"},
                                   candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    await audit(db, username, request, "manual_trade.sell_requested", {"position_id": str(p.id), "symbol": p.symbol,
                                                                      "mode": p.execution_mode, "route": p.execution_route})
    await db.commit()
    await events.publish(redis, "position.updated", {"position_id": str(p.id), "action": "exit", "exit_requested": True}, "api")
    return {"position_id": str(p.id), "status": "SELL_REQUESTED", "mode": p.execution_mode, "route": p.execution_route,
            "note": "the position manager sells on its next cycle through the current route; status follows below"}


@router.get("/positions/{position_id}")
async def position_status(position_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Position with PnL and every order (BUY and SELL) with its stage."""
    p = await db.get(PaperPosition, position_id)
    if p is None:
        raise HTTPException(404, "position not found")
    orders = (await db.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == p.id)
                               .order_by(ExecutionOrder.created_at))).scalars().all()
    decimals = ((p.plan or {}).get("venue") or {}).get("decimals")
    return jsonable({"position": live_smoke.position_view(p, datetime.now(timezone.utc)),
                     "orders": [live_smoke.order_view(o, p.entry_price, decimals) for o in orders]})
