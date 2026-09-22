from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PaperPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID | None
    symbol: str
    provider: str
    side: str
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal | None
    take_profit: list[Any]
    status: str
    exit_price: Decimal | None
    exit_reason: str | None
    realized_pnl: Decimal | None
    realized_pnl_pct: Decimal | None
    entry_at: datetime
    exit_at: datetime | None
    created_at: datetime
