from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RiskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID | None
    symbol: str | None
    approved: bool
    reasons: list[Any]
    context: dict[str, Any] | None
    created_at: datetime


class KillSwitchStatus(BaseModel):
    engaged: bool
    reason: str | None


class KillSwitchEngageRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=512)
