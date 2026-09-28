"""Live execution (Pump.fun via PumpPortal's local-transaction API): status
and preflight, runtime execution settings, orders and reconciliation.

Nothing here can switch live trading on. That needs the three environment
locks (TRADING_ENABLED, LIVE_TRADING_ENABLED, PAPER_TRADING=false), a valid
wallet in .env, a ready order worker and the global mode LIVE — the API only
reports them. Secrets are never returned: the wallet is shown by its public
key only.
"""

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable, require_password
from yonixalpha_core import futures_live, live_smoke, live_trading
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import ExecutionOrder, LiveSmokeTest, PaperPosition, PlatformSetting, ReconciliationEvent
from yonixalpha_core.safety import store
from yonixalpha_core.solana.wallet import wallet_status

router = APIRouter(prefix="/live", tags=["live"])

VERIFICATION_PENDING = "IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION"


def _order(o: ExecutionOrder) -> dict[str, Any]:
    return {"id": o.id, "position_id": o.position_id, "assessment_id": o.assessment_id, "mode": o.mode, "side": o.side,
            "reason": o.reason, "mint": o.mint, "provider": o.provider, "route": o.route, "amount": o.amount,
            "amount_kind": o.amount_kind, "slippage_pct": o.slippage_pct, "priority_fee_sol": o.priority_fee_sol,
            "limits": o.limits, "status": o.status, "signature": o.signature, "attempts": o.attempts, "error": o.error,
            "guard": o.guard, "fill": (o.result or {}).get("fill"), "created_at": o.created_at,
            "submitted_at": o.submitted_at, "confirmed_at": o.confirmed_at}


@router.get("/status")
async def live_status(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """Preflight: every condition a LIVE order needs, each with its state."""
    now = datetime.now(timezone.utc)
    locks = {"TRADING_ENABLED": bool(settings.TRADING_ENABLED), "LIVE_TRADING_ENABLED": bool(settings.LIVE_TRADING_ENABLED),
             "PAPER_TRADING": bool(settings.PAPER_TRADING)}
    permitted = store.live_trading_permitted(settings)
    wallet = wallet_status(settings)
    worker_raw = await redis.get(live_trading.READY_KEY)
    worker = json.loads(worker_raw) if worker_raw else None
    wallet_raw = await redis.get(live_trading.WALLET_KEY)
    synced = json.loads(wallet_raw) if wallet_raw else None
    ready, reason = await live_trading.live_readiness(redis, settings, now)
    mode = await store.load_global_mode(db)
    confirmed = (await db.execute(select(func.count()).select_from(ExecutionOrder).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "CONFIRMED"))).scalar_one()
    by_status = dict((await db.execute(select(PaperPosition.status, func.count()).where(
        PaperPosition.execution_mode == "LIVE").group_by(PaperPosition.status))).all())
    checks = [
        {"check": "environment locks", "ok": permitted,
         "detail": "open" if permitted else "TRADING_ENABLED and LIVE_TRADING_ENABLED must be true and PAPER_TRADING false"},
        {"check": "wallet key", "ok": bool(wallet["valid"]), "detail": wallet["error"] or wallet["pubkey"]},
        {"check": "Solana RPC", "ok": bool(settings.SOLANA_RPC_URL),
         "detail": "configured" if settings.SOLANA_RPC_URL else "SOLANA_RPC_URL / HELIUS_API_KEY not set"},
        {"check": "order worker", "ok": bool(worker and worker.get("status") == "ready"),
         "detail": (worker or {}).get("reason") or (worker or {}).get("status") or "not running"},
        {"check": "wallet balance synced", "ok": synced is not None,
         "detail": f"{synced['sol']} SOL at {synced['at']}" if synced else "no reconciliation yet"},
        {"check": "global mode LIVE", "ok": str(mode) == "LIVE", "detail": str(mode)},
    ]
    return jsonable({
        "locks": locks, "permitted": permitted, "wallet": wallet, "worker": worker, "wallet_sync": synced,
        "ready": ready, "not_ready_reason": reason, "global_mode": str(mode), "checks": checks,
        "provider": {"name": live_trading.LIVE_PROVIDER, "api": "PumpPortal Local Transaction API (trade-local); "
                     "transactions are built remotely, checked by the transaction guard and signed locally"},
        "positions": by_status, "confirmed_live_orders": confirmed,
        "verification": VERIFICATION_PENDING if confirmed == 0 else f"{confirmed} live order(s) confirmed on chain",
    })


@router.get("/settings")
async def get_live_settings(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    s = await live_trading.load_live_settings(db)
    return {"settings": s.to_dict(), "limits": {k: [str(lo), str(hi)] for k, (lo, hi) in live_trading.LIMITS.items()}}


@router.put("/settings")
async def put_live_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                            username: str = Depends(get_current_username)) -> dict:
    current = (await live_trading.load_live_settings(db)).to_dict()
    merged = {**current, **{k: str(v) for k, v in body.items()}}
    s, errors = live_trading.parse_live_settings(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = await db.get(PlatformSetting, live_trading.SETTINGS_KEY)
    if row is None:
        db.add(PlatformSetting(key=live_trading.SETTINGS_KEY, value=s.to_dict()))
    else:
        row.value = s.to_dict()
    await audit(db, username, request, "live_settings.updated", {"before": current, "after": s.to_dict()})
    await db.commit()
    return {"settings": s.to_dict(), "limits": {k: [str(lo), str(hi)] for k, (lo, hi) in live_trading.LIMITS.items()}}


@router.get("/futures")
async def futures_status(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                         settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """Live futures/FX execution: settings and each venue's readiness as
    reported by services/execution-futures (never inferred here)."""
    venues = {}
    for venue in ("binance", "bybit", "hyperliquid", "mt5"):
        ok, why = await futures_live.readiness(redis, settings, venue)
        raw = await redis.get(futures_live.READY_KEY.format(venue=venue))
        venues[venue] = {"ready": ok, "reason": why, "report": json.loads(raw) if raw else None}
    s = await futures_live.load_settings(db)
    return {"settings": s.to_dict(), "limits": {k: [str(lo), str(hi)] for k, (lo, hi) in futures_live.LIMITS.items()},
            "venues": venues}


@router.put("/futures/settings")
async def put_futures_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                               username: str = Depends(get_current_username)) -> dict:
    current = (await futures_live.load_settings(db)).to_dict()
    s, errors = futures_live.parse_settings({**current, **body})
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = await db.get(PlatformSetting, futures_live.SETTINGS_KEY)
    if row is None:
        db.add(PlatformSetting(key=futures_live.SETTINGS_KEY, value=s.to_dict()))
    else:
        row.value = s.to_dict()
    await audit(db, username, request, "futures_live_settings.updated", {"before": current, "after": s.to_dict()})
    await db.commit()
    return {"settings": s.to_dict(), "limits": {k: [str(lo), str(hi)] for k, (lo, hi) in futures_live.LIMITS.items()}}


@router.get("/orders")
async def list_orders(status: str | None = None, side: str | None = None, mint: str | None = None,
                      limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                      _: str = Depends(get_current_username)) -> list[dict]:
    q = select(ExecutionOrder).order_by(ExecutionOrder.created_at.desc()).limit(limit)
    if status:
        q = q.where(ExecutionOrder.status.in_(status.split(",")))
    if side:
        q = q.where(ExecutionOrder.side == side)
    if mint:
        q = q.where(ExecutionOrder.mint == mint)
    return jsonable([_order(o) for o in (await db.execute(q)).scalars().all()])


@router.get("/orders/{order_id}")
async def get_order(order_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    o = await db.get(ExecutionOrder, order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    return jsonable({**_order(o), "result": o.result})


@router.get("/reconciliation")
async def list_reconciliation(kind: str | None = None, limit: int = Query(100, ge=1, le=500),
                              db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> list[dict]:
    q = select(ReconciliationEvent).order_by(ReconciliationEvent.created_at.desc()).limit(limit)
    if kind:
        q = q.where(ReconciliationEvent.kind == kind)
    rows = (await db.execute(q)).scalars().all()
    return jsonable([{"id": r.id, "kind": r.kind, "severity": r.severity, "mint": r.mint, "position_id": r.position_id,
                      "order_id": r.order_id, "detail": r.detail, "created_at": r.created_at} for r in rows])


@router.get("/positions")
async def live_positions(status: str | None = None, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                         _: str = Depends(get_current_username)) -> list[dict]:
    """LIVE positions with PnL from their recorded fills and latest mark;
    price_status is LIVE / STALE / UNAVAILABLE, never a silent old number."""
    now = datetime.now(timezone.utc)
    q = select(PaperPosition).where(PaperPosition.execution_mode == "LIVE").order_by(PaperPosition.created_at.desc()).limit(limit)
    if status:
        q = q.where(PaperPosition.status.in_(status.split(",")))
    rows = (await db.execute(q)).scalars().all()
    sigs: dict[Any, str | None] = {}
    if rows:
        for o in (await db.execute(select(ExecutionOrder).where(
                ExecutionOrder.position_id.in_([p.id for p in rows]), ExecutionOrder.side == "BUY")
                .order_by(ExecutionOrder.created_at))).scalars():
            sigs[o.position_id] = o.signature
    return jsonable([{**live_smoke.position_view(p, now),
                      "route": p.execution_route, "pool": p.pool, "remaining": p.remaining_quantity,
                      "entry_cost_sol": p.entry_cost_quote, "proceeds_sol": p.proceeds_quote, "last_price": p.last_price,
                      "pending_order_id": p.pending_order_id, "exit_failures": p.exit_failures,
                      "entry_signature": sigs.get(p.id)} for p in rows])


def _short(addr: str | None) -> str | None:
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 12 else addr


@router.get("/wallets")
async def wallets(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                  _: str = Depends(get_current_username)) -> dict:
    """LIVE and PAPER side by side, never mixed. LIVE comes from the chain
    (the order worker's wallet sync); only the public address is shown."""
    now = datetime.now(timezone.utc)
    raw = await redis.get(live_trading.WALLET_KEY)
    w = json.loads(raw) if raw else None
    settings_live = await live_trading.load_live_settings(db)
    pending = (await db.execute(select(func.coalesce(func.sum(cast(ExecutionOrder.amount, Numeric)), 0)).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "BUY",
        ExecutionOrder.status.in_(("PENDING", "SIGNED", "SUBMITTED"))))).scalar_one()
    live: dict[str, Any] = {"label": "LIVE", "synced": w is not None}
    if w:
        sol = Decimal(w["sol"])
        reserved = settings_live.min_sol_reserve + Decimal(pending)
        val = w.get("valuation") or {}
        age = (now - datetime.fromisoformat(w["at"])).total_seconds()
        live.update({
            "address": _short(w.get("pubkey")), "sol": str(sol), "balance_at": w["at"], "balance_age_seconds": round(age, 1),
            "balance_status": "LIVE" if age <= 180 else "STALE", "reserved_sol": str(reserved),
            "reserve_breakdown": {"min_sol_reserve": str(settings_live.min_sol_reserve), "pending_buys": str(pending)},
            "available_sol": str(max(Decimal(0), sol - reserved)), "holdings": val.get("holdings") or [],
            "holdings_value_sol": val.get("total_sol"), "holdings_unvalued": val.get("unvalued_count"),
            "valuation_note": val.get("note") or val.get("error"), "sol_usd": val.get("sol_usd"),
            "sol_usd_source": val.get("sol_usd_source"),
            "total_estimated_sol": str(sol + Decimal(val["total_sol"])) if val.get("total_sol") is not None else None,
            "total_is_complete": val.get("unvalued_count", 1) == 0 and not val.get("more_not_shown"),
            "empty_token_accounts": w.get("empty_token_accounts"),
        })
    rent_orders = (await db.execute(select(ExecutionOrder).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == live_trading.RENT_SIDE)
        .order_by(ExecutionOrder.created_at.desc()).limit(5))).scalars().all()
    live["rent_reclaims"] = [{"id": o.id, "status": o.status, "reason": o.reason, "scope": o.mint, "signature": o.signature,
                              "error": o.error, "created_at": o.created_at,
                              "closed": len(((o.result or {}).get("reclaim") or {}).get("closing") or [])
                              if o.status == "CONFIRMED" else 0,
                              "refund_sol": str(Decimal(((o.result or {}).get("fill") or {}).get("sol_change_lamports") or 0)
                                                / live_trading.LAMPORTS) if o.status == "CONFIRMED" else None}
                             for o in rent_orders]
    acct = await store.get_paper_account(db, "solana")
    open_rows = (await db.execute(select(PaperPosition).where(PaperPosition.account_id == acct.id,
                                                              PaperPosition.status == "open"))).scalars().all()
    open_value = sum((store.marked_value(p) for p in open_rows), Decimal(0))
    open_cost = sum(((p.entry_cost_quote or Decimal(0)) * ((p.remaining_quantity if p.remaining_quantity is not None else p.quantity)
                     / (p.initial_quantity or p.quantity)) if (p.initial_quantity or p.quantity) else Decimal(0)
                     for p in open_rows), Decimal(0))
    realized = (await db.execute(select(func.coalesce(func.sum(PaperPosition.realized_pnl), 0)).where(
        PaperPosition.account_id == acct.id, PaperPosition.status == "closed",
        PaperPosition.exit_at >= acct.reset_at))).scalar_one()
    paper = {"label": "PAPER", "currency": acct.quote_currency, "starting_balance": str(acct.starting_balance),
             "available_balance": str(acct.cash_balance), "open_positions": len(open_rows),
             "open_position_value": str(open_value), "unrealized_pnl": str(open_value - open_cost),
             "realized_pnl": str(Decimal(realized)), "equity": str(acct.cash_balance + open_value), "since": acct.reset_at}
    return jsonable({"live": live, "paper": paper})


class RentReclaim(BaseModel):
    confirm: bool


@router.post("/reclaim-rent")
async def reclaim_rent(body: RentReclaim, request: Request, db: AsyncSession = Depends(get_db),
                       redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                       username: str = Depends(get_current_username)) -> dict:
    """Queues closing the wallet's EMPTY token accounts so their rent deposit
    returns to the wallet. The order worker reads the accounts from chain,
    closes only those holding zero tokens with no open or pending position,
    and its guard refuses any transaction that does anything else."""
    if body.confirm is not True:
        raise HTTPException(422, "confirm must be true")
    now = datetime.now(timezone.utc)
    ready, reason = await live_trading.live_readiness(redis, settings, now)
    if not ready:
        raise HTTPException(409, f"live order worker not ready: {reason}")
    order = await live_trading.request_rent_reclaim(db, now, None, "rent_reclaim_manual")
    if order is None:
        raise HTTPException(409, "a rent reclaim is already queued")
    await audit(db, username, request, "live.rent_reclaim_requested", {"order_id": str(order.id)})
    await db.commit()
    return jsonable({"order_id": order.id, "status": order.status})


# --- LIVE_EXECUTION_SMOKE_TEST (yonixalpha_core.live_smoke) ------------------

class SmokeArm(BaseModel):
    category: str = Field(pattern="^(FRESH|MIGRATED|MOMENTUM)$")
    max_sol: Decimal | None = Field(default=None, gt=0, le=Decimal("10"))
    minutes: int = Field(default=live_smoke.DEFAULT_MINUTES, ge=live_smoke.MIN_MINUTES, le=live_smoke.MAX_MINUTES)
    password: str = Field(min_length=1, max_length=256)
    confirm: str = Field(max_length=64)


@router.get("/smoke-test")
async def smoke_status(db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                       _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    armed = await live_smoke.armed_run(db, now)
    await db.commit()  # records expiry (NO_TEST_EXECUTION_CANDIDATE)
    runs = (await db.execute(select(LiveSmokeTest).order_by(LiveSmokeTest.created_at.desc()).limit(10))).scalars().all()
    return jsonable({"config": live_smoke.config_status(settings), "trades_used": await live_smoke.trades_used(db),
                     "armed": str(armed.id) if armed else None,
                     "runs": [await live_smoke.run_view(db, r, now) for r in runs]})


@router.post("/smoke-test/arm")
async def smoke_arm(body: SmokeArm, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                    settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    """Arms ONE smoke-test run. Needs the server switch (.env), the admin
    password again and the typed confirmation phrase; it never changes the
    global mode and every buy still passes the full gate."""
    await require_password(db, redis, username, body.password, request, "live_smoke", {"category": body.category})
    if body.confirm != live_smoke.CONFIRM_PHRASE:
        raise HTTPException(422, f"type the confirmation phrase exactly: {live_smoke.CONFIRM_PHRASE}")
    now = datetime.now(timezone.utc)
    try:
        run = await live_smoke.arm(db, redis, settings, body.category, body.max_sol, body.minutes, username, now)
    except live_smoke.SmokeRefused as exc:
        await audit(db, username, request, "live_smoke.arm_refused", {"category": body.category, "stage": exc.stage,
                                                                      "reason": exc.reason})
        await db.commit()
        raise HTTPException(409, {"stage": exc.stage, "reason": exc.reason}) from exc
    await audit(db, username, request, "live_smoke.armed", {"run": str(run.id), "category": run.category,
                                                            "max_sol": str(run.max_sol), "expires_at": run.expires_at.isoformat()})
    await db.commit()
    return jsonable(await live_smoke.run_view(db, run, now))


@router.post("/smoke-test/{run_id}/cancel")
async def smoke_cancel(run_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                       username: str = Depends(get_current_username)) -> dict:
    run = await db.get(LiveSmokeTest, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run.status != "ARMED":
        raise HTTPException(409, f"run is {run.status}; an open smoke-test position is closed with /close")
    now = datetime.now(timezone.utc)
    run.status, run.stage, run.stage_reason, run.finished_at = "CANCELLED", "CANCELLED", f"disarmed by {username}", now
    await audit(db, username, request, "live_smoke.cancelled", {"run": str(run.id)})
    await db.commit()
    return jsonable(await live_smoke.run_view(db, run, now))


@router.post("/smoke-test/{run_id}/close")
async def smoke_close(run_id: UUID, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      username: str = Depends(get_current_username)) -> dict:
    """Test-close: the same operator exit every position has. The position
    manager sells through the existing exit path (expected output, slippage
    limit, transaction guard); nothing is filled until the SELL confirms."""
    run = await db.get(LiveSmokeTest, run_id)
    if run is None or run.position_id is None:
        raise HTTPException(404, "no smoke-test position for this run")
    p = await db.get(PaperPosition, run.position_id)
    if p is None or p.status != "open":
        raise HTTPException(409, f"position is {p.status if p else 'missing'}")
    if p.exit_requested:
        raise HTTPException(409, "exit already requested")
    now = datetime.now(timezone.utc)
    p.exit_requested = True
    await store.add_timeline_event(db, "operator_exit", now, {"by": username, "note": "smoke-test close"},
                                   candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    await audit(db, username, request, "live_smoke.close_requested", {"run": str(run.id), "position_id": str(p.id)})
    await db.commit()
    return jsonable(await live_smoke.run_view(db, run, now))
