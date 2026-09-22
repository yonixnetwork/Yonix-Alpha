from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.system import KillSwitchSummary, ServiceStatus, SystemEventOut, SystemStatusOut
from yonixalpha_core import kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import SystemEvent

router = APIRouter(prefix="/system", tags=["system"])

# Every service that writes a SystemEvent row — see each
# services/*/app/main.py's own SERVICE_NAME/service= constant. A service
# absent from system_events entirely reports "unknown" rather than a
# guess: several services intentionally no-op without ever starting when
# required env config is missing (e.g. engine-solana-momentum without
# SOLANA_RPC_URL), and this environment may simply never have run others.
KNOWN_SERVICES = [
    "data-solana",
    "data-binance",
    "engine-solana-discovery",
    "engine-solana-migration",
    "engine-solana-momentum",
    "engine-binance-futures",
    "decision-engine",
    "ml",
    "paper-trading",
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
