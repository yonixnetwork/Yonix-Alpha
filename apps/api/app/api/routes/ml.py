from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.ml import MLStatsOut, ModelVersionOut
from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/models", response_model=Page[ModelVersionOut])
async def list_models(
    name: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[ModelVersionOut]:
    filters = []
    if name is not None:
        filters.append(ModelVersion.name == name)
    if status_filter is not None:
        filters.append(ModelVersion.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(ModelVersion).where(*filters))).scalar_one()
    result = await db.execute(select(ModelVersion).where(*filters).order_by(ModelVersion.trained_at.desc()).limit(limit).offset(offset))
    models = result.scalars().all()
    return Page(items=[ModelVersionOut.model_validate(m) for m in models], total=total, limit=limit, offset=offset)


@router.get("/stats", response_model=MLStatsOut)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> MLStatsOut:
    """Per docs/ML.md: labeled_features is expected to be 0 until paper
    trading (or a future live trade) closes a position with a known
    outcome — this reports the real count, never a placeholder.
    """
    total = (await db.execute(select(func.count()).select_from(MLFeatureSnapshot))).scalar_one()
    labeled = (await db.execute(select(func.count()).select_from(MLFeatureSnapshot).where(MLFeatureSnapshot.label.is_not(None)))).scalar_one()
    return MLStatsOut(total_features=total, labeled_features=labeled, unlabeled_features=total - labeled)
