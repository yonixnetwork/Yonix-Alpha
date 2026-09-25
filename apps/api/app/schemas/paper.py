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
    # Gate-driven positions (null on Phase 7 rows)
    engine: str | None = None
    asset_id: str | None = None
    assessment_id: UUID | None = None
    initial_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    entry_cost_quote: Decimal | None = None
    proceeds_quote: Decimal | None = None
    fees_paid_quote: Decimal | None = None
    max_loss_quote: Decimal | None = None
    plan: dict[str, Any] | None = None
    tp_hits: list[Any] | None = None
    trailing_stop: Decimal | None = None
    highest_price: Decimal | None = None
    lowest_price: Decimal | None = None
    last_price: Decimal | None = None
    last_marked_at: datetime | None = None
    management_paused: bool = False
    exit_requested: bool = False
    account_id: UUID | None = None
    # Provenance (migration 0011)
    execution_mode: str = "PAPER"
    source: str | None = None
    lifecycle: str | None = None
    execution_provider: str | None = None
    execution_route: str | None = None
    pool: str | None = None
    strategy: str | None = None
    model_version: str | None = None
    feature_version: str | None = None
    pending_order_id: UUID | None = None
    exit_failures: int = 0
