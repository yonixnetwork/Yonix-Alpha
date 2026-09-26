from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SettingsOut(BaseModel):
    scope: str
    effective: dict[str, Any]
    source: dict[str, Any]
    defaults: dict[str, Any]
    hard_limits: dict[str, Any]
    # Fields with a fixed set of values (rendered as a select).
    enums: dict[str, list[str]] = {}


class SettingsUpdate(BaseModel):
    settings: dict[str, Any]
    note: str | None = Field(None, max_length=256)


class SettingsVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    version: int
    settings: dict[str, Any]
    note: str | None
    created_at: datetime


class SettingsSaved(BaseModel):
    version: SettingsVersionOut
    clamp_notes: list[str]


class ModesOut(BaseModel):
    global_mode: str
    strategies: dict[str, str]
    env: dict[str, bool]


class ModeUpdate(BaseModel):
    mode: str


class BlacklistIn(BaseModel):
    """A word filter. scope GLOBAL / FRESH / MIGRATED (or one engine);
    BLOCK rules reject, ALLOW rules waive word blocks (never the immutable
    system safety checks). Matching is case-insensitive."""
    scope: str = "GLOBAL"
    field: Literal["name", "symbol", "mint", "metadata", "any"]
    match_type: Literal["exact", "word", "substring", "pattern", "regex"] = "exact"
    action: Literal["BLOCK", "ALLOW"] = "BLOCK"
    value: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(None, max_length=256)


class BlacklistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    field: str
    match_type: str
    action: str
    value: str
    reason: str | None
    enabled: bool
    created_at: datetime


class CustomRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scope: str = "GLOBAL"
    field: str
    op: str
    threshold: str = Field(min_length=1, max_length=64)
    action: str


class CustomRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    scope: str
    field: str
    op: str
    threshold: str
    action: str
    enabled: bool
    created_at: datetime


class EnabledUpdate(BaseModel):
    enabled: bool


class AssessmentSummary(BaseModel):
    id: UUID
    candidate_id: UUID | None
    engine: str
    strategy: str
    asset_id: str
    symbol: str | None
    decision: str
    status_label: str
    executable: bool
    execution_target: str
    overall_risk: str
    approval_state: str
    reasons: list[str]
    position_size: str | None
    evaluated_at: datetime
    outcome: dict[str, Any] | None


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    detail: dict[str, Any] | None
    occurred_at: datetime
    position_id: UUID | None
    assessment_id: UUID | None


class AssessmentDetail(AssessmentSummary):
    assessment: dict[str, Any]
    approved_at: datetime | None
    timeline: list[TimelineEventOut]


class PaperAccountOut(BaseModel):
    name: str
    quote_currency: str
    starting_balance: str
    cash_balance: str
    equity: str
    open_positions: int
    open_exposure: str
    closed_positions: int
    winning_positions: int
    realized_pnl: str
    fees_paid: str
    reset_at: datetime


class PaperResetIn(BaseModel):
    starting_balance: str
    # The account name typed again: a reset erases the book's running record.
    confirm: str


class PipelineOut(BaseModel):
    stream: dict[str, Any]
    funnel: dict[str, Any]
    candidates: dict[str, int]
    decisions_24h: dict[str, int]
    last_assessment_at: datetime | None
