"""Strategy center: every strategy and venue with its mode (stored and
effective), validated configuration, implementation status and paper
results; grid start/stop requests.

Config never holds secrets (those stay in .env). A mode can never exceed
what the environment locks allow: LIVE needs the global mode AND the
environment flags, and live execution is not implemented for any venue.
"""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import audit, user_id
from yonixalpha_core import events, kill_switch
from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, StrategyState
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.strategies import grid
from yonixalpha_core.strategies.catalog import CATALOG, STRATEGY_VENUE_ENGINE, Entry, validate_config

router = APIRouter(prefix="/strategies", tags=["strategies"])


class ConfigIn(BaseModel):
    config: dict


class ModeIn(BaseModel):
    mode: str


def _position_filter(e: Entry):
    if e.kind in ("solana", "venue"):
        return PaperPosition.engine == e.name
    return RiskAssessment.strategy == e.name


def _venue_engine(e: Entry, config: dict) -> str | None:
    if e.kind == "futures":
        return STRATEGY_VENUE_ENGINE.get(config.get("venue") or "binance")
    return e.engine if e.kind in ("grid",) else None


async def _describe(db: AsyncSession, e: Entry) -> dict:
    saved = await store.load_strategy_config(db, e.name) if e.rules else {}
    config = {**e.defaults, **saved}
    out: dict = {"name": e.name, "label": e.label, "kind": e.kind, "status": e.status, "engine": e.engine,
                 "account": e.account, "config": config, "saved_config": saved, "editable": sorted(e.rules)}
    if e.has_mode:
        mode = await store.load_strategy_mode(db, e.name)
        venue_engine = _venue_engine(e, config)
        effective = mode
        if venue_engine:
            effective = pipeline.effective_mode(mode, await store.load_strategy_mode(db, venue_engine))
        out.update(mode=mode.value, effective_mode=effective.value, venue_mode_key=venue_engine)
    if e.kind == "analytics":
        return out
    if e.kind == "grid":
        rows = (await db.execute(select(StrategyState).where(StrategyState.strategy == e.name))).scalars().all()
        out["grid_sessions"] = [{"coin": r.key, **grid.session_summary(r.status, r.state or {})} for r in rows]
        return out
    acct = await store.get_paper_account(db, e.account) if e.account else None
    base = select(PaperPosition).outerjoin(RiskAssessment, RiskAssessment.id == PaperPosition.assessment_id).where(
        _position_filter(e))
    closed = (await db.execute(base.where(PaperPosition.status == "closed", PaperPosition.exit_at >= acct.reset_at)
                               if acct else base.where(PaperPosition.status == "closed"))).scalars().all()
    open_n = (await db.execute(select(func.count()).select_from(base.where(PaperPosition.status == "open").subquery()))).scalar_one()
    stats = performance([ClosedTrade(p.realized_pnl or Decimal(0), p.realized_pnl_pct, p.fees_paid_quote or Decimal(0),
                                     p.entry_at, p.exit_at or p.entry_at) for p in closed])
    last = (await db.execute(select(func.max(RiskAssessment.evaluated_at)).where(
        or_(RiskAssessment.strategy == e.name, RiskAssessment.engine == e.name)))).scalar_one()
    out.update(currency=acct.quote_currency if acct else None, open_positions=open_n, last_decision_at=last,
               trades=stats["trades"], win_rate=stats["win_rate"], total_pnl=stats["total_pnl"],
               profit_factor=stats["profit_factor"], max_drawdown=stats["max_drawdown"])
    return out


def _entry(name: str) -> Entry:
    e = CATALOG.get(name)
    if e is None:
        raise HTTPException(404, f"unknown strategy; one of {list(CATALOG)}")
    return e


@router.get("")
async def list_strategies(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> list[dict]:
    out = [await _describe(db, e) for e in CATALOG.values()]
    await db.commit()  # get_paper_account may create default books
    return out


@router.get("/{name}")
async def get_strategy(name: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    out = await _describe(db, _entry(name))
    await db.commit()
    return out


@router.put("/{name}/config")
async def put_config(name: str, body: ConfigIn, request: Request, db: AsyncSession = Depends(get_db),
                     redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    e = _entry(name)
    saved = await store.load_strategy_config(db, name)
    clean, errors = validate_config(name, {**saved, **body.config})
    if errors:
        raise HTTPException(422, {"errors": errors})
    await store.save_strategy_config(db, name, clean, await user_id(db, username))
    await db.commit()
    await events.publish(redis, "strategy.updated", {"strategy": name, "config": True}, "api")
    return await _describe(db, e)


@router.put("/{name}/mode")
async def put_mode(name: str, body: ModeIn, request: Request, db: AsyncSession = Depends(get_db),
                   redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    e = _entry(name)
    if not e.has_mode:
        raise HTTPException(409, f"{name} is analytics only and has no trading mode")
    try:
        mode = StrategyMode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, f"mode must be one of {[m.value for m in StrategyMode]}") from exc
    await store.set_strategy_mode(db, name, mode, await user_id(db, username))
    await db.commit()
    await events.publish(redis, "strategy.updated", {"strategy": name, "mode": mode.value}, "api")
    return await _describe(db, e)


async def _grid_command(cmd: str, request: Request, db: AsyncSession, redis: Redis, username: str) -> dict:
    e = CATALOG["hyperliquid_grid"]
    if cmd == "start":
        desc = await _describe(db, e)
        if desc["effective_mode"] == StrategyMode.OFF.value:
            raise HTTPException(409, "grid (or the hyperliquid_perps venue) is OFF; set a mode first")
        if await kill_switch.is_engaged(redis):
            raise HTTPException(409, "kill switch is engaged")
    await redis.set(grid.COMMAND_KEY, cmd, ex=600)
    await audit(db, username, request, f"grid.{cmd}_requested", {"at": datetime.now().isoformat()})
    await db.commit()
    return {"requested": cmd, "note": "the paper-trading grid loop applies this on its next tick (≈15 s) using the live mid"}


@router.post("/hyperliquid_grid/start")
async def grid_start(request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                     username: str = Depends(get_current_username)) -> dict:
    return await _grid_command("start", request, db, redis, username)


@router.post("/hyperliquid_grid/stop")
async def grid_stop(request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                    username: str = Depends(get_current_username)) -> dict:
    return await _grid_command("stop", request, db, redis, username)

