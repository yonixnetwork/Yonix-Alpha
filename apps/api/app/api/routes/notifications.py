"""In-app notification feed and per-kind Telegram preferences."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.api.util import audit, jsonable
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from yonixalpha_core.db.models import Notification, PlatformSetting
from yonixalpha_core.events import DEFAULT_TELEGRAM_KINDS, NOTIFICATION_KINDS, PREFS_KEY

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _out(n: Notification) -> dict:
    return jsonable({"id": n.id, "kind": n.kind, "severity": n.severity, "title": n.title, "body": n.body, "data": n.data,
                     "read_at": n.read_at, "created_at": n.created_at})


@router.get("")
async def list_notifications(
    unread: bool = False,
    kind: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> dict:
    filters = []
    if unread:
        filters.append(Notification.read_at.is_(None))
    if kind:
        filters.append(Notification.kind == kind)
    total = (await db.execute(select(func.count()).select_from(Notification).where(*filters))).scalar_one()
    unread_n = (await db.execute(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))).scalar_one()
    rows = (await db.execute(select(Notification).where(*filters).order_by(Notification.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return {"items": [_out(n) for n in rows], "total": total, "unread": unread_n, "limit": limit, "offset": offset}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    n = await db.get(Notification, notification_id)
    if n is None:
        raise HTTPException(404, "notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
    return _out(n)


@router.post("/read-all")
async def mark_all_read(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    r = await db.execute(update(Notification).where(Notification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"marked": r.rowcount}


async def _prefs(db: AsyncSession) -> dict:
    row = await db.get(PlatformSetting, PREFS_KEY)
    stored = (row.value or {}) if row else {}
    return {k: {"in_app": True, "telegram": bool((stored.get(k) or {}).get("telegram", k in DEFAULT_TELEGRAM_KINDS))}
            for k in NOTIFICATION_KINDS}


@router.get("/prefs")
async def get_prefs(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    return await _prefs(db)


@router.put("/prefs")
async def put_prefs(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                    username: str = Depends(get_current_username)) -> dict:
    """Body: {kind: {"telegram": bool}}. In-app delivery is always on — every
    notification is stored — so only Telegram is configurable."""
    unknown = [k for k in body if k not in NOTIFICATION_KINDS]
    bad = [k for k, v in body.items() if not isinstance(v, dict) or not isinstance(v.get("telegram"), bool)]
    if unknown or bad:
        raise HTTPException(422, {"errors": [f"unknown kind: {k}" for k in unknown] +
                                  [f"{k}: expected {{\"telegram\": true|false}}" for k in bad if k not in unknown]})
    current = {k: {"telegram": v["telegram"]} for k, v in (await _prefs(db)).items()}
    current.update({k: {"telegram": v["telegram"]} for k, v in body.items()})
    row = await db.get(PlatformSetting, PREFS_KEY)
    if row is None:
        db.add(PlatformSetting(key=PREFS_KEY, value=current))
    else:
        row.value = current
    await audit(db, username, request, "notification_prefs.updated", {"changed": body})
    await db.commit()
    return await _prefs(db)
