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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import live_trading
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, PlatformSetting, ReconciliationEvent
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
    q = select(PaperPosition).where(PaperPosition.execution_mode == "LIVE").order_by(PaperPosition.created_at.desc()).limit(limit)
    if status:
        q = q.where(PaperPosition.status.in_(status.split(",")))
    rows = (await db.execute(q)).scalars().all()
    return jsonable([{"id": p.id, "symbol": p.symbol, "mint": p.asset_id, "status": p.status, "lifecycle": p.lifecycle,
                      "route": p.execution_route, "pool": p.pool, "quantity": p.quantity, "remaining": p.remaining_quantity,
                      "entry_price": p.entry_price, "entry_cost_sol": p.entry_cost_quote, "proceeds_sol": p.proceeds_quote,
                      "last_price": p.last_price, "stop_loss": p.stop_loss, "trailing_stop": p.trailing_stop,
                      "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason, "pending_order_id": p.pending_order_id,
                      "exit_failures": p.exit_failures, "entry_at": p.entry_at, "exit_at": p.exit_at} for p in rows])
