"""Small helpers shared by the control-center routers."""

import json
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import AuditLog, User


async def user_id(db: AsyncSession, username: str) -> UUID | None:
    return (await db.execute(select(User.id).where(User.username == username))).scalar_one_or_none()


async def audit(db: AsyncSession, username: str, request: Request, event_type: str, detail: dict) -> None:
    db.add(AuditLog(user_id=await user_id(db, username), event_type=event_type,
                    ip_address=request.client.host if request.client else None, detail=jsonable(detail)))


def jsonable(v: Any) -> Any:
    """Decimals/datetimes/UUIDs -> strings, so responses and JSONB columns
    never lose precision to floats."""
    return json.loads(json.dumps(v, default=str))
