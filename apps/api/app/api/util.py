"""Small helpers shared by the control-center routers."""

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import AuditLog, User
from yonixalpha_core.security import verify_password

PASSWORD_MAX_FAILURES, PASSWORD_LOCKOUT_SECONDS = 5, 900


async def user_id(db: AsyncSession, username: str) -> UUID | None:
    return (await db.execute(select(User.id).where(User.username == username))).scalar_one_or_none()


async def audit(db: AsyncSession, username: str, request: Request, event_type: str, detail: dict) -> None:
    db.add(AuditLog(user_id=await user_id(db, username), event_type=event_type,
                    ip_address=request.client.host if request.client else None, detail=jsonable(detail)))


def jsonable(v: Any) -> Any:
    """Decimals/datetimes/UUIDs -> strings, so responses and JSONB columns
    never lose precision to floats."""
    return json.loads(json.dumps(v, default=str))


async def require_password(db: AsyncSession, redis, username: str, password: str, request: Request, scope: str,
                           detail: dict) -> None:
    """Re-authentication for dangerous actions: the logged-in user's own
    password again, 5 wrong attempts lock the action for 15 minutes. Raises
    429 / 403; a rejection is audited (never the password itself)."""
    fails_key = f"yx:{scope}:pwfail:{username}"
    if int(await redis.get(fails_key) or 0) >= PASSWORD_MAX_FAILURES:
        raise HTTPException(429, "too many wrong passwords — try again in 15 minutes")
    user = (await db.execute(select(User).where(User.username == username, User.is_active.is_(True)))).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        await redis.incr(fails_key)
        await redis.expire(fails_key, PASSWORD_LOCKOUT_SECONDS)
        await audit(db, username, request, f"{scope}.password_rejected", detail)
        await db.commit()
        raise HTTPException(403, "password incorrect")
    await redis.delete(fails_key)
