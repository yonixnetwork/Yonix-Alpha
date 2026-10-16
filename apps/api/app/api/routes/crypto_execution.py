"""Crypto Trading: trades on exchange accounts (practice now; live only after
the code unlock). Read-only views plus one control, close, which goes
through the same executor as the worker (cancel protection, confirm, sell).

    GET  /crypto/exec/trades               recent trades with their connection and last decision
    GET  /crypto/exec/trades/{id}          one trade: orders and every recorded action
    GET  /crypto/exec/guards               connections whose new entries are blocked, and why
    POST /crypto/exec/trades/{id}/close    {password}"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import require_password
from yonixalpha_crypto.db import ApiConnection, ExecGuard, ExecOrder, ExecTrade, TradeAction, schema_ready
from yonixalpha_crypto.private.executor import Executor
from yonixalpha_crypto.private.livelock import LiveLocked
from yonixalpha_crypto.private.service import open_account
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError

router = APIRouter(prefix="/crypto/exec", tags=["crypto"])

TRADE_COLS = ("exchange", "environment", "strategy_id", "symbol", "status", "protection", "qty_planned", "qty",
              "entry_price", "initial_stop", "stop", "stop_limit", "take_profit", "highest_price", "risk_budget",
              "planned_loss", "plan", "thesis", "realized_quote", "fees_quote", "close_reason", "created_at",
              "opened_at", "closed_at", "updated_at")


class CloseIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


async def _ready(db: AsyncSession) -> None:
    if not await schema_ready(db):
        raise HTTPException(503, "The crypto schema is not installed yet.")


async def _trade(db: AsyncSession, trade_id: str) -> ExecTrade:
    try:
        t = await db.get(ExecTrade, uuid.UUID(trade_id))
    except ValueError:
        t = None
    if t is None:
        raise HTTPException(404, "no such trade")
    return t


@router.get("/trades")
async def list_trades(limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db),
                      username: str = Depends(get_current_username)):
    await _ready(db)
    rows = (await db.execute(select(ExecTrade, ApiConnection.label).join(ApiConnection, ApiConnection.id == ExecTrade.connection_id)
                             .order_by(ExecTrade.created_at.desc()).limit(limit))).all()
    out = []
    for t, label in rows:
        last = (await db.execute(select(TradeAction).where(TradeAction.trade_id == t.id)
                                 .order_by(TradeAction.id.desc()).limit(1))).scalar_one_or_none()
        out.append({"id": str(t.id), "connection_id": str(t.connection_id), "connection": label,
                    **{c: _j(getattr(t, c)) for c in TRADE_COLS},
                    "last_action": {"action": last.action, "reason": last.reason, "at": _j(last.at)} if last else None})
    return out


@router.get("/trades/{trade_id}")
async def trade_detail(trade_id: str, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    t = await _trade(db, trade_id)
    orders = (await db.execute(select(ExecOrder).where(ExecOrder.trade_id == t.id).order_by(ExecOrder.created_at))).scalars()
    acts = (await db.execute(select(TradeAction).where(TradeAction.trade_id == t.id).order_by(TradeAction.id))).scalars()
    return {"id": str(t.id), **{c: _j(getattr(t, c)) for c in TRADE_COLS},
            "orders": [{c: _j(getattr(o, c)) for c in ("client_id", "role", "side", "kind", "qty", "price", "trigger_price",
                                                       "status", "active", "filled_qty", "avg_price", "fee", "fee_asset",
                                                       "error", "created_at", "checked_at")} for o in orders],
            "actions": [{"at": _j(a.at), "action": a.action, "reason": a.reason, "detail": a.detail} for a in acts]}


@router.get("/guards")
async def guards(db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    rows = (await db.execute(select(ExecGuard, ApiConnection).join(ApiConnection, ApiConnection.id == ExecGuard.connection_id))).all()
    return [{"connection_id": str(g.connection_id), "connection": c.label, "exchange": c.exchange,
             "environment": c.environment, "entries_blocked": g.entries_blocked, "reason": g.reason,
             "since": _j(g.since)} for g, c in rows]


@router.post("/trades/{trade_id}/close")
async def close_trade(trade_id: str, body: CloseIn, request: Request, db: AsyncSession = Depends(get_db),
                      redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)):
    await _ready(db)
    t = await _trade(db, trade_id)
    if t.status not in ("OPEN", "UNCERTAIN"):
        raise HTTPException(409, f"the trade is {t.status}")
    await require_password(db, redis, username, body.password, request, "crypto_exec", {"action": "close", "id": trade_id})
    c = await db.get(ApiConnection, t.connection_id)
    acct = open_account(c)
    if acct is None:
        raise HTTPException(503, "the connection's keys cannot be decrypted; enter them again")
    try:
        await Executor(acct, c).close(db, t, f"closed from the dashboard by {username}")
    except LiveLocked as e:
        raise HTTPException(423, str(e)) from None
    except ExchangeRequestError as e:
        raise HTTPException(502, f"exchange error: {e}") from None
    finally:
        await acct.aclose()
    await db.refresh(t)
    return {"id": trade_id, "status": t.status, "close_reason": t.close_reason}
