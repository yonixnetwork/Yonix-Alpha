"""Crypto Trading: exchange connections (API keys). Separate from the
Memecoin engine; everything is in the `crypto` schema (yonixalpha_crypto).

    GET  /crypto/connections               environments, saved connections (never a key), live gates
    POST /crypto/connections               save a key (password + typed environment confirmation)
    PUT  /crypto/connections/{id}          {enabled, password?}  (password required for LIVE)
    POST /crypto/connections/{id}/test     read-only connection and permission test
    POST /crypto/connections/{id}/delete   {password}

Keys are encrypted at rest and never returned. DEMO / TESTNET keys can only
reach practice hosts and LIVE keys only live hosts. Saving or enabling a
LIVE connection does not authorize an order: real orders are locked in code
(yonixalpha_crypto.private.livelock)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import require_password
from yonixalpha_crypto.db import ApiConnection, schema_ready
from yonixalpha_crypto.private import connections, environments, livelock
from yonixalpha_crypto.private.environments import LIVE
from yonixalpha_crypto.private.secretstore import NoEncryptionKey

router = APIRouter(prefix="/crypto/connections", tags=["crypto"])

TEST_COOLDOWN_S = 10


class ConnectionIn(BaseModel):
    exchange: str = Field(max_length=16)
    environment: str = Field(max_length=8)
    confirm_environment: str = Field(max_length=8)
    label: str = Field(min_length=1, max_length=48)
    api_key: str = Field(min_length=1, max_length=256)
    api_secret: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)


class ConnectionUpdate(BaseModel):
    enabled: bool
    password: str | None = Field(None, max_length=256)


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


async def _ready(db: AsyncSession) -> None:
    if not await schema_ready(db):
        raise HTTPException(503, "The crypto schema is not installed yet.")


async def _get(db: AsyncSession, conn_id: str) -> ApiConnection:
    try:
        cid = uuid.UUID(conn_id)
    except ValueError:
        raise HTTPException(404, "no such connection") from None
    c = await db.get(ApiConnection, cid)
    if c is None:
        raise HTTPException(404, "no such connection")
    return c


def _out(c: ApiConnection) -> dict[str, Any]:
    v = connections.view(c)
    v["live_gates"] = livelock.gates_json(v) if c.environment == LIVE else None
    return v


@router.get("")
async def list_connections(db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    rows = (await db.execute(select(ApiConnection).order_by(ApiConnection.exchange, ApiConnection.environment,
                                                             ApiConnection.label))).scalars().all()
    return {
        "live_orders_unlocked": livelock.LIVE_ORDERS_UNLOCKED,
        "environments": [{"exchange": e.exchange, "environment": e.environment, "host": e.base_url,
                          "label": e.label, "note": e.note} for e in environments.ACCOUNT_ENVIRONMENTS.values()],
        "connections": [_out(c) for c in rows],
    }


@router.post("")
async def create_connection(body: ConnectionIn, request: Request, db: AsyncSession = Depends(get_db),
                            redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)):
    await _ready(db)
    if body.confirm_environment != body.environment:
        raise HTTPException(422, f"type the environment ({body.environment}) again to confirm where this key belongs")
    await require_password(db, redis, username, body.password, request, "crypto_keys",
                           {"action": "create", "exchange": body.exchange, "environment": body.environment})
    try:
        c = await connections.create(db, exchange=body.exchange, environment=body.environment, label=body.label,
                                     api_key=body.api_key, api_secret=body.api_secret, actor=username)
    except connections.ConnectionInputError as e:
        raise HTTPException(422, str(e)) from None
    except NoEncryptionKey as e:
        raise HTTPException(503, str(e)) from None
    await db.commit()
    return _out(c)


@router.put("/{conn_id}")
async def update_connection(conn_id: str, body: ConnectionUpdate, request: Request, db: AsyncSession = Depends(get_db),
                            redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)):
    await _ready(db)
    c = await _get(db, conn_id)
    if c.environment == LIVE:
        if not body.password:
            raise HTTPException(422, "changing a LIVE connection needs your password")
        await require_password(db, redis, username, body.password, request, "crypto_keys",
                               {"action": "enable" if body.enabled else "disable", "id": conn_id})
    await connections.set_enabled(db, c, body.enabled, username)
    await db.commit()
    return _out(c)


@router.post("/{conn_id}/test")
async def test_connection(conn_id: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                          username: str = Depends(get_current_username)):
    await _ready(db)
    c = await _get(db, conn_id)
    if not await redis.set(f"yx:crypto:conn_test:{conn_id}", "1", nx=True, ex=TEST_COOLDOWN_S):
        raise HTTPException(429, f"wait {TEST_COOLDOWN_S} s between tests of one connection")
    try:
        result = await connections.test(db, c)
    except NoEncryptionKey as e:
        raise HTTPException(503, str(e)) from None
    await db.commit()
    return {**result, "connection": _out(c)}


@router.post("/{conn_id}/delete")
async def delete_connection(conn_id: str, body: PasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                            redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)):
    await _ready(db)
    c = await _get(db, conn_id)
    await require_password(db, redis, username, body.password, request, "crypto_keys", {"action": "delete", "id": conn_id})
    try:
        await connections.delete(db, c, username)
    except connections.ConnectionInputError as e:
        raise HTTPException(409, str(e)) from None
    await db.commit()
    return {"deleted": conn_id}
