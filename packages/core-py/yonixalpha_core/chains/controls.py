"""Operator trading controls below the global kill switch.

Keys (trading_controls): new_entries, sniper, copy, chain:<chain>,
launchpad:<key>. A missing row is the default: enabled. Launchpads also
carry a mode (OFF / PAPER / LIVE); the default is DEFAULT_MODE per chain.

What a switch blocks (new entries only; exits and position management are
never blocked by these switches — use CLOSE POSITIONS / EMERGENCY EXIT):
  new_entries OFF   every new entry (sniper, copy, manual)
  chain:X OFF       every new entry on chain X
  sniper OFF        autonomous discovery entries (not manual, not copy)
  copy OFF          copy-trading entries
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains.base import Chain
from yonixalpha_core.db.models import TradingControl

SWITCHES = ("new_entries", "sniper", "copy") + tuple(f"chain:{c.value}" for c in Chain)
MODES = ("OFF", "PAPER", "LIVE")
# Solana launchpads follow the existing global trading mode (LIVE allowed);
# EVM launchpads may at most paper-trade until the operator sets LIVE.
DEFAULT_MODE = {Chain.SOLANA: "LIVE", Chain.BSC: "PAPER", Chain.ROBINHOOD: "PAPER"}
SOURCES = ("sniper", "copy", "manual")

ENGINE_CHAIN = {"solana_fresh": Chain.SOLANA, "solana_momentum": Chain.SOLANA, "solana_migration": Chain.SOLANA}


async def load(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = (await session.execute(select(TradingControl))).scalars().all()
    return {r.key: {"enabled": r.enabled, "mode": r.mode, "note": r.note, "updated_by": r.updated_by,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows}


def blocked_by(controls: dict[str, dict[str, Any]], chain: Chain | str | None, source: str) -> str | None:
    """The first switch that blocks a new entry, or None."""
    def off(key: str) -> bool:
        return controls.get(key, {}).get("enabled", True) is False

    if off("new_entries"):
        return "NEW ENTRIES OFF"
    if chain is not None:
        c = chain.value if isinstance(chain, Chain) else str(chain)
        if off(f"chain:{c}"):
            return f"{c.upper()} OFF"
    if source == "sniper" and off("sniper"):
        return "SNIPER OFF"
    if source == "copy" and off("copy"):
        return "COPY TRADING OFF"
    return None


def launchpad_mode(controls: dict[str, dict[str, Any]], key: str, chain: Chain) -> str:
    row = controls.get(f"launchpad:{key}") or {}
    if row.get("enabled") is False:
        return "OFF"
    return row.get("mode") or DEFAULT_MODE[chain]


async def set_control(session: AsyncSession, key: str, *, enabled: bool | None = None, mode: str | None = None,
                      note: str | None = None, user: str | None = None, now: datetime | None = None) -> None:
    if key not in SWITCHES and not key.startswith("launchpad:"):
        raise ValueError(f"unknown control {key}")
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    now = now or datetime.now(timezone.utc)
    values: dict[str, Any] = {"key": key, "updated_by": user, "updated_at": now, "note": note}
    if enabled is not None:
        values["enabled"] = enabled
    if mode is not None:
        values["mode"] = mode
    update = {k: v for k, v in values.items() if k != "key"}
    await session.execute(insert(TradingControl).values(**{"enabled": True, **values})
                          .on_conflict_do_update(index_elements=["key"], set_=update))
