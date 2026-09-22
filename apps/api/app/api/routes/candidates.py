from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_username, get_db
from app.schemas.candidates import (
    CandidateDetail,
    CandidatePaperPositionSummary,
    CandidateRiskEventSummary,
    CandidateSignalSummary,
    CandidateSummary,
)
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from yonixalpha_core.db.models import PaperPosition, RiskEvent, StrategySignal, TradingCandidate

router = APIRouter(prefix="/candidates", tags=["candidates"])


def _to_summary(candidate: TradingCandidate) -> CandidateSummary:
    return CandidateSummary(
        id=candidate.id,
        token_id=candidate.token_id,
        mint_address=candidate.token.mint_address,
        engine=candidate.engine,
        state=candidate.state,
        state_updated_at=candidate.state_updated_at,
        created_at=candidate.created_at,
    )


@router.get("", response_model=Page[CandidateSummary])
async def list_candidates(
    state: str | None = None,
    engine: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[CandidateSummary]:
    filters = []
    if state is not None:
        filters.append(TradingCandidate.state == state)
    if engine is not None:
        filters.append(TradingCandidate.engine == engine)

    total = (await db.execute(select(func.count()).select_from(TradingCandidate).where(*filters))).scalar_one()
    result = await db.execute(
        select(TradingCandidate)
        .options(selectinload(TradingCandidate.token))
        .where(*filters)
        .order_by(TradingCandidate.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    candidates = result.scalars().all()
    return Page(items=[_to_summary(c) for c in candidates], total=total, limit=limit, offset=offset)


@router.get("/{candidate_id}", response_model=CandidateDetail)
async def get_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> CandidateDetail:
    result = await db.execute(
        select(TradingCandidate).options(selectinload(TradingCandidate.token)).where(TradingCandidate.id == candidate_id)
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    signal_result = await db.execute(
        select(StrategySignal).where(StrategySignal.candidate_id == candidate_id).order_by(StrategySignal.created_at.desc()).limit(1)
    )
    latest_signal = signal_result.scalar_one_or_none()

    risk_result = await db.execute(
        select(RiskEvent).where(RiskEvent.candidate_id == candidate_id).order_by(RiskEvent.created_at.desc()).limit(1)
    )
    latest_risk_event = risk_result.scalar_one_or_none()

    position_result = await db.execute(
        select(PaperPosition).where(PaperPosition.candidate_id == candidate_id).order_by(PaperPosition.created_at.desc()).limit(1)
    )
    paper_position = position_result.scalar_one_or_none()

    summary = _to_summary(candidate)
    return CandidateDetail(
        **summary.model_dump(),
        detail=candidate.detail,
        state_history=candidate.state_history,
        latest_signal=CandidateSignalSummary.model_validate(latest_signal) if latest_signal else None,
        latest_risk_event=CandidateRiskEventSummary.model_validate(latest_risk_event) if latest_risk_event else None,
        paper_position=CandidatePaperPositionSummary.model_validate(paper_position) if paper_position else None,
    )
