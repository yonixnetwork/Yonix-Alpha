from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.paper import PaperPositionOut
from yonixalpha_core.db.models import PaperPosition

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/positions", response_model=Page[PaperPositionOut])
async def list_positions(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[PaperPositionOut]:
    filters = []
    if status_filter is not None:
        filters.append(PaperPosition.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(PaperPosition).where(*filters))).scalar_one()
    result = await db.execute(select(PaperPosition).where(*filters).order_by(PaperPosition.created_at.desc()).limit(limit).offset(offset))
    positions = result.scalars().all()
    return Page(items=[PaperPositionOut.model_validate(p) for p in positions], total=total, limit=limit, offset=offset)
