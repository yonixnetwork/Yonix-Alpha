"""Strategy center: the Solana strategies with their mode, validated
configuration, implementation status and paper results. (The futures, FX,
grid and Gold vs BTC strategies were removed; see branch
archive/legacy-futures-forex-grid-2026-09-29.)

Config never holds secrets (those stay in .env). A mode can never exceed
what the environment locks allow: LIVE needs the global mode AND the
environment flags; live execution exists only for the Pump.fun strategies
(yonixalpha_core.live_trading).
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, user_id
from yonixalpha_core import events, strategy_modes
from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import PaperPosition, RiskAssessment
from yonixalpha_core.momentum_insight import insight as momentum_insight_report
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.strategies import momentum_v2
from yonixalpha_core.strategies.catalog import CATALOG, Entry, validate_config
from yonixalpha_core.strategy_comparison import compare as strategy_comparison_report

router = APIRouter(prefix="/strategies", tags=["strategies"])


class ConfigIn(BaseModel):
    config: dict


class ModeIn(BaseModel):
    mode: str
    # Required (true) to switch to AUTO: see yonixalpha_core.strategy_modes.
    confirm_auto: bool = False


class EmergencyIn(BaseModel):
    confirm: str
    close_positions: bool = False
    reason: str | None = None


def _position_filter(e: Entry):
    if e.kind in ("solana", "venue"):
        return PaperPosition.engine == e.name
    return RiskAssessment.strategy == e.name


async def _describe(db: AsyncSession, e: Entry, settings: Settings | None = None) -> dict:
    saved = await store.load_strategy_config(db, e.name) if e.rules else {}
    config = {**e.defaults, **saved}
    out: dict = {"name": e.name, "label": e.label, "kind": e.kind, "status": e.status, "engine": e.engine,
                 "account": e.account, "config": config, "saved_config": saved, "editable": sorted(e.rules)}
    if e.has_mode:
        mode = await store.load_strategy_mode(db, e.name)
        out.update(mode=mode.value, effective_mode=mode.value, venue_mode_key=None)
        global_mode = await store.load_global_mode(db)
        permitted = store.live_trading_permitted(settings) if settings is not None else False
        out["execution"] = {**strategy_modes.execution_view(mode, global_mode, permitted),
                            "global_mode": global_mode.value, "environment_live_permitted": permitted}
        if e.name == "solana_momentum":
            mcfg = momentum_v2.config_of(saved)
            out["momentum"] = {"version": momentum_v2.version_of(mcfg), "enabled_checks": momentum_v2.enabled_checks(mcfg),
                               "monitor_when_off": mcfg.get("monitor_when_off") is True}
    acct = await store.get_paper_account(db, e.account) if e.account else None
    base = select(PaperPosition).outerjoin(RiskAssessment, RiskAssessment.id == PaperPosition.assessment_id).where(
        _position_filter(e))
    closed = (await db.execute(base.where(PaperPosition.status == "closed", PaperPosition.exit_at >= acct.reset_at)
                               if acct else base.where(PaperPosition.status == "closed"))).scalars().all()
    open_n = (await db.execute(select(func.count()).select_from(base.where(PaperPosition.status == "open").subquery()))).scalar_one()
    stats = performance([ClosedTrade(p.realized_pnl or Decimal(0), p.realized_pnl_pct, p.fees_paid_quote or Decimal(0),
                                     p.entry_at, p.exit_at or p.entry_at) for p in closed])
    # Newest decision of this engine, read from (engine, evaluated_at) (migration 0045). It was
    # max() over `strategy = X OR engine = X`: strategy has no index, so the OR read the
    # 3.6 GB table and the Fresh / Migrated / Momentum pages stopped at the 25 s limit (2026-10-09).
    async def newest(col) -> datetime | None:
        return (await db.execute(select(RiskAssessment.evaluated_at).where(col == e.name)
                                 .order_by(RiskAssessment.evaluated_at.desc()).limit(1))).scalar_one_or_none()

    last = await newest(RiskAssessment.engine)
    if last is None and e.engine != e.name:  # a strategy run by a shared engine
        last = await newest(RiskAssessment.strategy)
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
async def list_strategies(db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                          _: str = Depends(get_current_username)) -> list[dict]:
    out = [await _describe(db, e, settings) for e in CATALOG.values()]
    await db.commit()  # get_paper_account may create default books
    return out


@router.get("/comparison")
async def strategy_comparison(since: datetime | None = None, db: AsyncSession = Depends(get_db),
                              _: str = Depends(get_current_username)) -> dict:
    """Closed Solana positions by strategy, token category and both (PAPER and
    LIVE apart), with the net without the single best trade. Read-only;
    declared before /{name} so "comparison" is not taken for a strategy."""
    return await strategy_comparison_report(db, since)


@router.get("/solana_momentum/insight")
async def momentum_insight(since: datetime | None = None, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                           _: str = Depends(get_current_username)) -> dict:
    """Momentum panel: decisions by state, results by token category and
    entry type, recent decision explanations. Read-only; declared before
    /{name}."""
    return await momentum_insight_report(db, since)


@router.get("/{name}")
async def get_strategy(name: str, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                       _: str = Depends(get_current_username)) -> dict:
    out = await _describe(db, _entry(name), settings)
    await db.commit()
    return out


@router.put("/{name}/config")
async def put_config(name: str, body: ConfigIn, request: Request, db: AsyncSession = Depends(get_db),
                     redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                     username: str = Depends(get_current_username)) -> dict:
    e = _entry(name)
    saved = await store.load_strategy_config(db, name)
    clean, errors = validate_config(name, {**saved, **body.config})
    if errors:
        raise HTTPException(422, {"errors": errors})
    await store.save_strategy_config(db, name, clean, await user_id(db, username))
    await db.commit()
    await events.publish(redis, "strategy.updated", {"strategy": name, "config": True}, "api")
    return await _describe(db, e, settings)


@router.put("/{name}/mode")
async def put_mode(name: str, body: ModeIn, request: Request, db: AsyncSession = Depends(get_db),
                   redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                   username: str = Depends(get_current_username)) -> dict:
    """Stored in strategy_configs (the gate, the funnel and the position
    loop read it on every pass, so a change applies without a restart);
    audited with the previous and new mode. AUTO needs confirm_auto."""
    e = _entry(name)
    if not e.has_mode:
        raise HTTPException(409, f"{name} is analytics only and has no trading mode")
    try:
        mode = StrategyMode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, f"mode must be one of {[m.value for m in StrategyMode]}") from exc
    try:
        await strategy_modes.change_mode(db, name, mode, await user_id(db, username), confirm_auto=body.confirm_auto)
    except strategy_modes.ModeChangeRefused as exc:
        raise HTTPException(409, exc.reason) from exc
    await db.commit()
    await events.publish(redis, "strategy.updated", {"strategy": name, "mode": mode.value}, "api")
    return await _describe(db, e, settings)


@router.post("/{name}/emergency-stop")
async def emergency_stop(name: str, body: EmergencyIn, request: Request, db: AsyncSession = Depends(get_db),
                         redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                         username: str = Depends(get_current_username)) -> dict:
    """Separate from the mode switch: sets the strategy OFF (no new entries)
    and, only with close_positions, requests an exit for each of its open
    positions (the position loop sells them through the normal exit path;
    a failed fill is retried). confirm must be 'STOP <name>'."""
    e = _entry(name)
    if not e.has_mode:
        raise HTTPException(409, f"{name} has no trading mode")
    if body.confirm != f"STOP {name}":
        raise HTTPException(422, f"confirm must be 'STOP {name}'")
    uid = await user_id(db, username)
    before = await strategy_modes.change_mode(db, name, StrategyMode.OFF, uid)
    requested = 0
    if body.close_positions:
        now = datetime.now(timezone.utc)
        rows = (await db.execute(select(PaperPosition).where(
            PaperPosition.engine == name, PaperPosition.status == "open", PaperPosition.exit_requested.is_(False)))).scalars().all()
        for p in rows:
            p.exit_requested = True
            await store.add_timeline_event(db, "operator_exit", now, {"by": username, "note": f"emergency stop of {name}"},
                                           candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
        requested = len(rows)
    await audit(db, username, request, "strategy.emergency_stop",
                {"strategy": name, "from": before.value, "close_positions": body.close_positions,
                 "exit_requested": requested, "reason": body.reason})
    await db.commit()
    await events.publish(redis, "strategy.updated", {"strategy": name, "mode": "OFF"}, "api")
    if requested:
        await events.publish(redis, "position.updated", {"action": "close_strategy", "strategy": name, "positions": requested}, "api")
    return {"strategy": name, "mode": "OFF", "previous_mode": before.value, "exit_requested": requested,
            "note": ("open positions keep their normal exit management" if not body.close_positions
                     else "the position loop sells each requested position on its next pass")}
