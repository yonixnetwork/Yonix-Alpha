import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health_state
from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.system import KillSwitchSummary, ServiceStatus, SystemEventOut, SystemStatusOut
from yonixalpha_core import config_validation, events, kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import DataQualityEvent, Notification, RiskAssessment, SystemEvent

router = APIRouter(prefix="/system", tags=["system"])

# Every service that writes a SystemEvent row — see each
# services/*/app/main.py's own SERVICE_NAME/service= constant. A service
# absent from system_events entirely reports "unknown" rather than a
# guess: several services intentionally no-op without ever starting when
# required env config is missing (e.g. engine-solana-momentum without
# SOLANA_RPC_URL), and this environment may simply never have run others.
KNOWN_SERVICES = [
    "data-solana",
    "engine-solana-discovery",
    "engine-solana-migration",
    "engine-solana-momentum",
    "decision-engine",
    "ml",
    "paper-trading",
    "data-evm",
    "copy-engine",
]


async def _service_statuses(db: AsyncSession) -> dict[str, ServiceStatus]:
    """running/stopped is decided by whichever of that service's own
    service_started/service_stopped events is most recent — not a live
    health check (this API process has no channel to ping another
    container), just this database's own record of what last happened.
    """
    statuses: dict[str, ServiceStatus] = {}
    for service in KNOWN_SERVICES:
        result = await db.execute(
            select(SystemEvent)
            .where(SystemEvent.service == service, SystemEvent.event_type.in_(["service_started", "service_stopped"]))
            .order_by(SystemEvent.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            statuses[service] = ServiceStatus(status="unknown", last_event_at=None)
        elif row.event_type == "service_started":
            statuses[service] = ServiceStatus(status="running", last_event_at=row.created_at)
        else:
            statuses[service] = ServiceStatus(status="stopped", last_event_at=row.created_at)
    return statuses


@router.get("/status", response_model=SystemStatusOut)
async def status(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _: str = Depends(get_current_username),
    settings: Settings = Depends(get_settings),
) -> SystemStatusOut:
    return SystemStatusOut(
        app_env=settings.APP_ENV,
        app_name=settings.APP_NAME,
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        kill_switch=KillSwitchSummary(engaged=await kill_switch.is_engaged(redis), reason=await kill_switch.get_reason(redis)),
        services=await _service_statuses(db),
    )


@router.get("/events", response_model=Page[SystemEventOut])
async def list_events(
    service: str | None = None,
    severity: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[SystemEventOut]:
    filters = []
    if service is not None:
        filters.append(SystemEvent.service == service)
    if severity is not None:
        filters.append(SystemEvent.severity == severity)

    total = (await db.execute(select(func.count()).select_from(SystemEvent).where(*filters))).scalar_one()
    result = await db.execute(select(SystemEvent).where(*filters).order_by(SystemEvent.created_at.desc()).limit(limit).offset(offset))
    events = result.scalars().all()
    return Page(items=[SystemEventOut.model_validate(e) for e in events], total=total, limit=limit, offset=offset)


@router.get("/health")
async def health(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_username),
) -> dict:
    items = await health_state.connections(db, redis, settings)
    return {"overall": health_state.worst([c["state"] for c in items]), "states": health_state.STATES, "connections": items}


@router.get("/config-validation")
async def config_check(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_username),
) -> dict:
    """Per-module configuration status (validated now, against the current
    modes). Lists missing/invalid variable NAMES only — never values."""
    result = await config_validation.load_and_validate(db, settings)
    await config_validation.store_result(redis, result)
    return {"modules": result, "statuses": [config_validation.DISABLED, config_validation.READY, config_validation.CONFIG_ERROR],
            "note": ".env holds secrets and infrastructure; modes, risk limits and strategy parameters live in the "
                    "database and are edited from the dashboard"}


@router.get("/observability")
async def observability(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _: str = Depends(get_current_username),
) -> dict:
    """Counters for the operator: realtime events published (since Redis
    started), decisions / notifications / errors / quarantined ML samples in
    the last 24 h, and Redis memory."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    decisions = (await db.execute(select(RiskAssessment.decision, func.count()).where(
        RiskAssessment.evaluated_at >= since).group_by(RiskAssessment.decision))).all()
    notes = (await db.execute(select(Notification.kind, func.count()).where(
        Notification.created_at >= since).group_by(Notification.kind))).all()
    errors = (await db.execute(select(SystemEvent.service, func.count()).where(
        SystemEvent.created_at >= since, SystemEvent.severity.in_(["error", "critical"])).group_by(SystemEvent.service))).all()
    quality = (await db.execute(select(DataQualityEvent.issue, func.count()).where(
        DataQualityEvent.created_at >= since).group_by(DataQualityEvent.issue))).all()
    memory = await redis.info("memory")
    # Last pass of the open-position loop (paper-trading _position_loop):
    # when it ran, how long it took, and how many positions it managed.
    position_loop = None
    raw = await redis.get("yx:pm:last_pass")
    if raw:
        try:
            position_loop = json.loads(raw)
            position_loop["age_s"] = round((datetime.now(timezone.utc) - datetime.fromisoformat(position_loop["at"])).total_seconds(), 1)
        except (ValueError, KeyError, TypeError):
            position_loop = None
    return {
        "position_loop": position_loop,
        "events_published": {k: int(v) for k, v in (await redis.hgetall(events.COUNTS)).items()},
        "decisions_24h": dict(decisions),
        "notifications_24h": dict(notes),
        "errors_24h": dict(errors),
        "data_quality_24h": dict(quality),
        "websocket_clients": int(await redis.get("yx:ws:clients") or 0),
        "redis_memory": {"used": memory.get("used_memory_human"), "max": memory.get("maxmemory_human"),
                         "policy": memory.get("maxmemory_policy")},
    }
