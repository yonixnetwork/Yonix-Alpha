"""Crypto Trading engine (Bybit / Binance spot, DEMO only). Separate from
the Memecoin engine: everything here reads and writes the `crypto` schema
(yonixalpha_crypto), and the platform kill switch is only read.

    GET  /crypto/status               engine, worker, exchanges, strategies, demo accounts
    PUT  /crypto/engine               {enabled?, emergency_stop?, note?}
    PUT  /crypto/strategies/{id}      {expected_version, ...settings}  (one strategy only)
    POST /crypto/health-check         ask the worker for a public exchange check
    GET  /crypto/signals | orders | positions | events | metrics

There is no live mode: settings accept DISABLED / DEMO only, and the engine
package holds no exchange credential."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from yonixalpha_crypto import config_store, metrics, registry
from yonixalpha_crypto.db import (DemoAccount, DemoOrder, DemoPosition, EngineState, Event, Signal, StrategyConfig,
                                  schema_ready)
from yonixalpha_crypto.engine import HEALTH_KEY, HEALTH_REQUEST_KEY, HEARTBEAT_KEY, KILL_SWITCH_KEY
from yonixalpha_crypto.exchanges import PROFILES

router = APIRouter(prefix="/crypto", tags=["crypto"])

WORKER_HINT = ("The worker runs only while the engine is ON or a demo position is open, and exits otherwise to "
               "save memory. After turning the engine on, start it on the server with: cd /opt/yonixalpha && "
               "docker compose --env-file .env -f infra/docker/docker-compose.yml "
               "-f infra/docker/docker-compose.prod.yml up -d crypto-trading")


class EngineIn(BaseModel):
    enabled: bool | None = None
    emergency_stop: bool | None = None
    note: str | None = Field(default=None, max_length=300)


class StrategyIn(BaseModel):
    model_config = {"extra": "allow"}
    expected_version: int = Field(..., ge=1)


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(obj, cols: tuple[str, ...]) -> dict[str, Any]:
    return {c: _j(getattr(obj, c)) for c in cols}


async def _ready(db: AsyncSession) -> None:
    if not await schema_ready(db):
        raise HTTPException(status_code=503, detail="The crypto schema is not installed yet (the API runs "
                                                    "`python -m yonixalpha_crypto.migrate upgrade` at start).")


async def _redis_json(redis: Redis, key: str) -> Any:
    raw = await redis.get(key)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _spec_dict(spec: registry.StrategySpec) -> dict[str, Any]:
    return {"id": spec.id, "name": spec.name, "version": spec.version, "category": spec.category,
            "status": spec.status, "status_reason": spec.status_reason, "provenance": spec.provenance,
            "exchanges": list(spec.exchanges), "intervals": list(spec.intervals), "notes": list(spec.notes),
            "params": {k: {"default": str(v[0]) if isinstance(v[0], Decimal) else v[0], "kind": v[1],
                           "min": _j(v[2]), "max": _j(v[3])} for k, v in spec.params.items()}}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                 _user: str = Depends(get_current_username)) -> dict[str, Any]:
    ready = await schema_ready(db)
    heartbeat = await _redis_json(redis, HEARTBEAT_KEY)
    health = await _redis_json(redis, HEALTH_KEY) or {}
    out: dict[str, Any] = {
        "schema_ready": ready, "mode": "DEMO only (simulated execution on public market data)",
        "kill_switch": bool(await redis.get(KILL_SWITCH_KEY)),
        "worker": heartbeat, "worker_running": heartbeat is not None, "worker_hint": None if heartbeat else WORKER_HINT,
        "exchanges": [{"id": p.id, "label": p.label, "market_type": p.market_type, "base_url": p.base_url,
                       "maker_fee_bps": str(p.maker_fee_bps), "taker_fee_bps": str(p.taker_fee_bps),
                       "fee_source": p.fee_source, "note": p.note, "health": health.get(p.id)}
                      for p in PROFILES.values()],
    }
    if not ready:
        out.update(engine=None, strategies=[_spec_dict(s) for s in registry.all_specs()], accounts=[])
        return out
    await config_store.ensure_defaults(db)
    await db.commit()
    eng = await db.get(EngineState, 1)
    configs = {c.strategy_id: c for c in (await db.execute(select(StrategyConfig))).scalars()}
    open_by = dict((await db.execute(select(DemoPosition.strategy_id, func.count()).where(
        DemoPosition.status == "OPEN").group_by(DemoPosition.strategy_id))).all())
    out["engine"] = config_store.engine_dict(eng)
    out["strategies"] = [{**_spec_dict(s), "config": config_store.config_dict(configs[s.id]) if s.id in configs else None,
                          "open_positions": open_by.get(s.id, 0)} for s in registry.all_specs()]
    out["accounts"] = [_row(a, ("exchange", "quote", "starting_balance", "cash", "created_at"))
                       for a in (await db.execute(select(DemoAccount))).scalars()]
    return out


@router.put("/engine")
async def put_engine(body: EngineIn, db: AsyncSession = Depends(get_db),
                     user: str = Depends(get_current_username)) -> dict[str, Any]:
    await _ready(db)
    out = await config_store.set_engine(db, enabled=body.enabled, emergency_stop=body.emergency_stop, note=body.note,
                                        actor=user)
    await db.commit()
    return out


@router.put("/strategies/{strategy_id}")
async def put_strategy(strategy_id: str, body: StrategyIn, db: AsyncSession = Depends(get_db),
                       user: str = Depends(get_current_username)) -> dict[str, Any]:
    await _ready(db)
    await config_store.ensure_defaults(db)
    change = {k: v for k, v in body.model_dump().items() if k != "expected_version"}
    try:
        out = await config_store.save_strategy(db, strategy_id, change, body.expected_version, user)
    except config_store.ConfigError as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=e.errors)
    except config_store.ConfigConflict as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    await db.commit()
    return out


@router.post("/health-check")
async def request_health(redis: Redis = Depends(get_redis), _user: str = Depends(get_current_username)) -> dict:
    await redis.set(HEALTH_REQUEST_KEY, datetime.now(timezone.utc).isoformat(), ex=3600)
    return {"requested": True, "note": "the worker runs it on its next loop (if the worker is running)"}


SIGNAL_COLS = ("id", "strategy_id", "strategy_version", "exchange", "symbol", "interval", "candle_open_time", "side",
               "action", "reasons", "detail", "created_at")
ORDER_COLS = ("id", "client_id", "strategy_id", "exchange", "symbol", "side", "status", "reason", "qty", "price",
              "quote_amount", "fee_quote", "slippage_bps", "spread_bps", "created_at")
POSITION_COLS = ("id", "strategy_id", "exchange", "symbol", "side", "status", "qty", "entry_price", "entry_cost_quote",
                 "stop_price", "target_price", "fees_quote", "last_price", "exit_price", "proceeds_quote",
                 "realized_pnl_quote", "exit_reason", "opened_at", "closed_at")


@router.get("/signals")
async def signals(strategy_id: str | None = None, limit: int = Query(50, ge=1, le=500),
                  db: AsyncSession = Depends(get_db), _user: str = Depends(get_current_username)) -> list[dict]:
    await _ready(db)
    q = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
    if strategy_id:
        q = q.where(Signal.strategy_id == strategy_id)
    return [_row(s, SIGNAL_COLS) for s in (await db.execute(q)).scalars()]


@router.get("/orders")
async def orders(strategy_id: str | None = None, limit: int = Query(50, ge=1, le=500),
                 db: AsyncSession = Depends(get_db), _user: str = Depends(get_current_username)) -> list[dict]:
    await _ready(db)
    q = select(DemoOrder).order_by(DemoOrder.created_at.desc()).limit(limit)
    if strategy_id:
        q = q.where(DemoOrder.strategy_id == strategy_id)
    return [_row(o, ORDER_COLS) for o in (await db.execute(q)).scalars()]


@router.get("/positions")
async def positions(status: str | None = Query(None, pattern="^(OPEN|CLOSED)$"), strategy_id: str | None = None,
                    limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                    _user: str = Depends(get_current_username)) -> list[dict]:
    await _ready(db)
    q = select(DemoPosition).order_by(DemoPosition.opened_at.desc()).limit(limit)
    if status:
        q = q.where(DemoPosition.status == status)
    if strategy_id:
        q = q.where(DemoPosition.strategy_id == strategy_id)
    rows = []
    for p in (await db.execute(q)).scalars():
        r = _row(p, POSITION_COLS)
        if p.status == "OPEN" and p.last_price is not None:
            r["unrealized_pnl_quote"] = str(Decimal(p.qty) * Decimal(p.last_price) - Decimal(p.entry_cost_quote))
        rows.append(r)
    return rows


@router.get("/events")
async def events(limit: int = Query(50, ge=1, le=500), db: AsyncSession = Depends(get_db),
                 _user: str = Depends(get_current_username)) -> list[dict]:
    await _ready(db)
    q = select(Event).order_by(Event.at.desc()).limit(limit)
    return [_row(e, ("at", "level", "source", "message", "detail")) for e in (await db.execute(q)).scalars()]


@router.get("/metrics")
async def strategy_metrics(strategy_id: str, db: AsyncSession = Depends(get_db),
                           _user: str = Depends(get_current_username)) -> dict[str, Any]:
    await _ready(db)
    P = DemoPosition
    rows = (await db.execute(select(P.realized_pnl_quote, P.fees_quote, P.exchange).where(
        P.strategy_id == strategy_id, P.status == "CLOSED").order_by(P.closed_at).limit(5000))).all()
    start = None
    if rows:
        acc = (await db.execute(select(DemoAccount).where(DemoAccount.exchange == rows[-1][2]))).scalars().first()
        start = Decimal(acc.starting_balance) if acc else None
    return {"strategy_id": strategy_id, **metrics.summarize([Decimal(r[0]) for r in rows],
                                                            [Decimal(r[1]) for r in rows], start)}
