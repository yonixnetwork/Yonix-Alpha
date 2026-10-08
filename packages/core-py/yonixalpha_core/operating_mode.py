"""System resource mode and copy-trading status (low-resource operation,
2026-10-08).

    SYSTEM_RESOURCE_MODE  NORMAL | LOW_RESOURCE | EMERGENCY
    COPY_TRADING_STATUS   ACTIVE | THROTTLED | SUSPENDED

Defaults come from Settings (env); the dashboard can change both at runtime
(platform_settings key "operating_mode"). Copy trading is never deleted:
SUSPENDED stops every copy step that costs anything (watching targets,
outcome evaluation, wallet profiles, enrichment, wallet ML) while the copy
engine keeps protecting the copy positions already open (stop loss,
trailing, exits). EMERGENCY forces copy trading SUSPENDED.

Priority (what yields first under pressure):
  1. never paused: execution, open positions, stop loss / trailing / exits,
     risk engine, wallet balance, reconciliation, Solana discovery,
     fresh-token monitoring, migration detection
  2. kept: signals, momentum, token safety, market data, ML inference, dashboard
  3. reduced / paused: copy trading and its wallet ML / analytics, missed-
     winner calculations, historical reviews; memecoin ML training waits only
     while the host is CRITICAL
Nothing in this module is read by priority-1 code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import PlatformSetting

KEY = "operating_mode"
RESOURCE_MODES = ("NORMAL", "LOW_RESOURCE", "EMERGENCY")
COPY_STATUSES = ("ACTIVE", "THROTTLED", "SUSPENDED")

DISABLED_REASON = ("Copy trading is switched off for production (COPY_TRADING_ENABLED=false): copy-engine is not "
                   "started. Copy history, targets and settings are kept; set COPY_TRADING_ENABLED=true and redeploy "
                   "to turn it back on.")
SUSPENDED_REASON = ("Server currently running under constrained CPU/RAM capacity. Copy trading has been paused to "
                    "protect token discovery, sniping, live positions, the risk engine and execution.")

PRIORITIES = {
    "never_paused": ["execution", "open-position monitoring", "stop loss", "trailing stop", "emergency exits",
                     "risk engine", "wallet balance", "transaction reconciliation", "Solana token discovery",
                     "fresh-token monitoring", "migration detection"],
    "kept": ["signal generation", "momentum detection", "token safety", "market data", "ML inference", "dashboard",
             "paper trading"],
    "reduced_or_paused": ["copy trading", "copy-trading wallet ML and analytics", "missed-winner calculations",
                          "historical reviews (ML Review) while CRITICAL",
                          "memecoin ML training only while CRITICAL (retried within the hour)"],
}


def _norm(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip().upper()
    return v if v in allowed else default


async def load(session: AsyncSession, settings: Any) -> dict[str, Any]:
    """The mode and copy status in force, and where each came from."""
    row = await session.get(PlatformSetting, KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    mode_default = _norm(settings.SYSTEM_RESOURCE_MODE, RESOURCE_MODES, "LOW_RESOURCE")
    copy_default = _norm(settings.COPY_TRADING_STATUS, COPY_STATUSES, "SUSPENDED")
    mode = _norm(stored.get("resource_mode"), RESOURCE_MODES, mode_default)
    copy = _norm(stored.get("copy_trading"), COPY_STATUSES, copy_default)
    enabled = bool(getattr(settings, "COPY_TRADING_ENABLED", False))
    return {"resource_mode": mode, "resource_mode_source": "dashboard" if "resource_mode" in stored else "default",
            "copy_trading": copy, "copy_trading_source": "dashboard" if "copy_trading" in stored else "default",
            "copy_trading_enabled": enabled,
            "copy_trading_effective": effective_copy_status(mode, copy, enabled),
            "changed_at": stored.get("changed_at"), "changed_by": stored.get("changed_by"),
            "note": stored.get("note")}


async def save(session: AsyncSession, *, username: str, user_id=None, resource_mode: str | None = None,
               copy_trading: str | None = None, note: str | None = None) -> None:
    """Records a change (caller commits). Values are validated by the caller."""
    row = await session.get(PlatformSetting, KEY)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    if resource_mode is not None:
        value["resource_mode"] = resource_mode
    if copy_trading is not None:
        value["copy_trading"] = copy_trading
    value.update(changed_at=datetime.now(timezone.utc).isoformat(), changed_by=username, note=(note or None))
    if row is None:
        session.add(PlatformSetting(key=KEY, value=value, updated_by=user_id))
    else:
        row.value, row.updated_by = value, user_id


def effective_copy_status(mode: str, copy: str, enabled: bool = True) -> str:
    """COPY_TRADING_ENABLED=false (system profile) and EMERGENCY suspend copy
    trading whatever its own setting."""
    return "SUSPENDED" if mode == "EMERGENCY" or not enabled else copy


def training_decision(mode: str, level: str, last_run_at: datetime | None, now: datetime, settings: Any,
                      normal_interval: timedelta) -> tuple[bool, str]:
    """Whether a memecoin ML training step may run now, and why not.
    NORMAL: its usual interval. LOW_RESOURCE: its usual interval too
    (ML_TRAINING_INTERVAL_LOW_RESOURCE_H > 0 lengthens it), but not while the
    host is CRITICAL (checked again on the next wake). EMERGENCY: never.
    Inference is not affected (it runs in the decision engine). Copy-trading
    wallet ML is decided by the copy status, not here."""
    if mode == "EMERGENCY":
        return False, "SKIPPED - EMERGENCY resource mode: ML training paused"
    if mode == "LOW_RESOURCE" and level == "CRITICAL":
        return False, "SKIPPED - RESOURCE PRESSURE: host resource level CRITICAL"
    extra = getattr(settings, "ML_TRAINING_INTERVAL_LOW_RESOURCE_H", 0) or 0
    interval = timedelta(hours=extra) if mode == "LOW_RESOURCE" and extra > 0 else normal_interval
    if last_run_at is not None and timedelta(0) <= now - last_run_at < interval:
        return False, (f"SKIPPED - {mode}: runs every {interval}; next after "
                       f"{(last_run_at + interval).isoformat(timespec='seconds')}")
    return True, ""
