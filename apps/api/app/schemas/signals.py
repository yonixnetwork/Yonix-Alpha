from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StrategySignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID | None
    symbol: str
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
