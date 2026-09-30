from fastapi import APIRouter, Depends, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.risk import KillSwitchEngageRequest, KillSwitchStatus, RiskEventOut
from yonixalpha_core import kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import AuditLog, RiskEvent, User
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import send_telegram_alert

router = APIRouter(prefix="/risk", tags=["risk"])
log = get_logger("api.risk")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _write_audit(db: AsyncSession, username: str, ip: str | None, event_type: str, detail: dict) -> None:
    result = await db.execute(select(User.id).where(User.username == username))
    user_id = result.scalar_one_or_none()
    db.add(AuditLog(user_id=user_id, event_type=event_type, ip_address=ip, detail=detail))
    await db.commit()


async def _alert(settings: Settings, text: str) -> None:
    """send_telegram_alert itself already never raises (see
    yonixalpha_core.notify), but the kill switch is the single most
    safety-critical write action in this system — this extra guard means
    the engage/disengage response can never fail because of it, even if a
    future change to notify.py's contract slips.
    """
    try:
        await send_telegram_alert(settings, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("risk.kill_switch.alert_failed", error=str(exc))


@router.get("/events", response_model=Page[RiskEventOut])
async def list_risk_events(
    approved: bool | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[RiskEventOut]:
    filters = []
    if approved is not None:
        filters.append(RiskEvent.approved == approved)

    total = (await db.execute(select(func.count()).select_from(RiskEvent).where(*filters))).scalar_one()
    result = await db.execute(select(RiskEvent).where(*filters).order_by(RiskEvent.created_at.desc()).limit(limit).offset(offset))
    events = result.scalars().all()
    return Page(items=[RiskEventOut.model_validate(e) for e in events], total=total, limit=limit, offset=offset)


@router.get("/kill-switch", response_model=KillSwitchStatus)
async def get_kill_switch(
    redis: Redis = Depends(get_redis),
    _: str = Depends(get_current_username),
) -> KillSwitchStatus:
    return KillSwitchStatus(engaged=await kill_switch.is_engaged(redis), reason=await kill_switch.get_reason(redis))


@router.post("/kill-switch/engage", response_model=KillSwitchStatus)
async def engage_kill_switch(
    body: KillSwitchEngageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    username: str = Depends(get_current_username),
    settings: Settings = Depends(get_settings),
) -> KillSwitchStatus:
    """Per spec section 37, the kill switch must be reachable from
    anywhere, including here: this is the one write action a dashboard
    operator has over live trading state. Every use is audited — matching
    AuditLog's own documented purpose ("emergency stop") — with who did it,
    from where, and why. Also fires a Telegram alert (best-effort, never
    blocks the response — see yonixalpha_core.notify) since this is the
    single highest-value alert in the system: anyone else watching the
    channel should know the moment trading has been stopped or resumed,
    not just whoever is looking at the dashboard right now.
    """
    await kill_switch.engage(redis, body.reason)
    await _write_audit(db, username, _client_ip(request), "kill_switch_engaged", {"reason": body.reason})
    log.warning("risk.kill_switch.engaged", username=username, reason=body.reason)
    await _alert(settings, f"\U0001f6d1 Kill switch ENGAGED by {username}\nReason: {body.reason}")
    return KillSwitchStatus(engaged=True, reason=await kill_switch.get_reason(redis))


@router.post("/kill-switch/disengage", response_model=KillSwitchStatus)
async def disengage_kill_switch(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    username: str = Depends(get_current_username),
    settings: Settings = Depends(get_settings),
) -> KillSwitchStatus:
    await kill_switch.disengage(redis)
    await _write_audit(db, username, _client_ip(request), "kill_switch_disengaged", {})
    log.warning("risk.kill_switch.disengaged", username=username)
    await _alert(settings, f"Kill switch disengaged by {username}")
    return KillSwitchStatus(engaged=False, reason=None)
