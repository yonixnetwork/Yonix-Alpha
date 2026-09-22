from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.signals import StrategySignalOut
from yonixalpha_core.db.models import StrategySignal

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", response_model=Page[StrategySignalOut])
async def list_signals(
    candidate_id: UUID | None = None,
    decision: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[StrategySignalOut]:
    filters = []
    if candidate_id is not None:
        filters.append(StrategySignal.candidate_id == candidate_id)
    if decision is not None:
        filters.append(StrategySignal.decision == decision)

    total = (await db.execute(select(func.count()).select_from(StrategySignal).where(*filters))).scalar_one()
    result = await db.execute(select(StrategySignal).where(*filters).order_by(StrategySignal.created_at.desc()).limit(limit).offset(offset))
    signals = result.scalars().all()
    return Page(items=[StrategySignalOut.model_validate(s) for s in signals], total=total, limit=limit, offset=offset)
