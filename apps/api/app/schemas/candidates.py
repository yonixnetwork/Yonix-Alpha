from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CandidateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    token_id: UUID
    mint_address: str
    engine: str
    state: str
    state_updated_at: datetime
    created_at: datetime


class CandidateSignalSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    decision: str
    confidence: Decimal
    entry_type: str | None
    entry: Decimal | None
    stop_loss: Decimal | None
    take_profit: list[Any]
    risk_score: Decimal
    reason: list[Any]
    data_quality: str
    created_at: datetime


class CandidateRiskEventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    approved: bool
    reasons: list[Any]
    created_at: datetime


class CandidatePaperPositionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    side: str
    entry_price: Decimal
    exit_price: Decimal | None
    realized_pnl: Decimal | None
    realized_pnl_pct: Decimal | None
    exit_reason: str | None
    entry_at: datetime
    exit_at: datetime | None


class CandidateDetail(CandidateSummary):
    detail: dict[str, Any] | None
    state_history: list[Any]
    latest_signal: CandidateSignalSummary | None
    latest_risk_event: CandidateRiskEventSummary | None
    paper_position: CandidatePaperPositionSummary | None
