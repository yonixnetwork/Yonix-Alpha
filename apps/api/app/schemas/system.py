from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ServiceStatus(BaseModel):
    status: str  # running | stopped | unknown
    last_event_at: datetime | None


class KillSwitchSummary(BaseModel):
    engaged: bool
    reason: str | None


class SystemStatusOut(BaseModel):
    app_env: str
    app_name: str
    trading_enabled: bool
    live_trading_enabled: bool
    kill_switch: KillSwitchSummary
    services: dict[str, ServiceStatus]


class SystemEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service: str
    event_type: str
    severity: str
    detail: dict[str, Any] | None
    created_at: datetime
