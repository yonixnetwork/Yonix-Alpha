"""Launchpad activity health (master upgrade §5-6).

Separate from the verification status (chains/verification.py, which says
whether a launchpad's capabilities are PROVEN): this says whether the venue
is actually being USED, from what this system itself recorded on chain.

  ACTIVE      a launch, trade or migration in the last 24 hours
  QUIET       activity in the last 7 days, none in the last 24 hours
  INACTIVE    no launch, trade or migration for 7 days, and the venue has
              been monitored for at least 7 days (otherwise: UNVERIFIED)
  UNVERIFIED  no activity recorded yet and less than 7 days of monitoring,
              so neither ACTIVE nor INACTIVE can be claimed
  DEGRADED    the venue's discovery monitor stopped advancing (EVM cursor
              older than MONITOR_STALE), so recent activity is unknown
  DISABLED    the venue is inactive in the registry or the operator set OFF

The active launchpad list shows ACTIVE, QUIET and DEGRADED; INACTIVE and
UNVERIFIED venues are listed under "archived / inactive adapters". Nothing is
deleted and discovery keeps scanning them, so a venue whose activity returns
becomes ACTIVE again on the next computation (auto-reactivation).

Sources:
  EVM     launchpad_activity (daily rollup written by the discovery service
          in the same transaction as the events; idempotent, because only
          newly inserted launches / trades / migrations are counted), the
          discovery cursor, and launchpad_verify DISCOVERY / EVENTS passes.
  Solana  Pump.fun launches from token_observations (every observed fresh
          launch is decided and stored), PumpSwap migrations from
          token_events. Solana per-trade activity is not stored by venue, so
          its trade counters are None (not 0). Observe-only venues (Raydium
          LaunchLab, Meteora DBC, Moonshot) from the activity probe
          (solana/venue_probe.py): last successful transaction, transaction
          rate and the newest launch / trade / migration SEEN in its samples;
          7-day counts are not measured (None).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains.base import Chain, LaunchpadSpec
from yonixalpha_core.db.models import (EvmCursor, LaunchpadActivity, LaunchpadCheck, TokenEvent,
                                       TokenObservation)

ACTIVE, QUIET, INACTIVE, UNVERIFIED, DEGRADED, DISABLED = (
    "ACTIVE", "QUIET", "INACTIVE", "UNVERIFIED", "DEGRADED", "DISABLED")
LISTED = (ACTIVE, QUIET, DEGRADED)  # shown in the active launchpad filter
ACTIVE_WINDOW = timedelta(hours=24)
INACTIVITY = timedelta(days=7)
MONITOR_STALE = timedelta(minutes=15)
E18 = Decimal(10) ** 18


@dataclass
class DayCounts:
    launches: int = 0
    trades: int = 0
    migrations: int = 0
    volume: int = 0  # quote units, 18 decimals (BNB / ETH wei when the quote is native)
    last_launch: datetime | None = None
    last_trade: datetime | None = None
    last_migration: datetime | None = None


@dataclass
class Accumulator:
    """Per-day counts collected while a scan is persisted."""

    days: dict[date, DayCounts] = field(default_factory=lambda: defaultdict(DayCounts))

    def _day(self, at: datetime) -> DayCounts:
        return self.days[at.astimezone(timezone.utc).date()]

    def launch(self, at: datetime) -> None:
        d = self._day(at)
        d.launches += 1
        d.last_launch = max(filter(None, (d.last_launch, at)))

    def trade(self, at: datetime, quote_amount: int) -> None:
        d = self._day(at)
        d.trades += 1
        d.volume += int(quote_amount or 0)
        d.last_trade = max(filter(None, (d.last_trade, at)))

    def migration(self, at: datetime) -> None:
        d = self._day(at)
        d.migrations += 1
        d.last_migration = max(filter(None, (d.last_migration, at)))


async def record(session: AsyncSession, chain: str, launchpad: str, acc: Accumulator, now: datetime) -> None:
    """Adds one scan's newly stored events to the daily rollup (caller commits)."""
    t = LaunchpadActivity
    for day, c in acc.days.items():
        if not (c.launches or c.trades or c.migrations):
            continue
        stmt = insert(t).values(chain=chain, launchpad=launchpad, day=day, launches=c.launches, trades=c.trades,
                                migrations=c.migrations, volume=c.volume, last_launch_at=c.last_launch,
                                last_trade_at=c.last_trade, last_migration_at=c.last_migration, updated_at=now)
        ex = stmt.excluded
        await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "launchpad", "day"], set_={
            "launches": t.launches + ex.launches, "trades": t.trades + ex.trades,
            "migrations": t.migrations + ex.migrations, "volume": t.volume + ex.volume,
            "last_launch_at": func.greatest(t.last_launch_at, ex.last_launch_at),
            "last_trade_at": func.greatest(t.last_trade_at, ex.last_trade_at),
            "last_migration_at": func.greatest(t.last_migration_at, ex.last_migration_at),
            "updated_at": ex.updated_at}))


def classify(*, disabled_reason: str | None, last_activity: datetime | None, monitored_since: datetime | None,
             monitor_at: datetime | None, now: datetime) -> tuple[str, str]:
    if disabled_reason:
        return DISABLED, disabled_reason
    if monitor_at is not None and now - monitor_at > MONITOR_STALE:
        return DEGRADED, f"discovery monitor has not advanced for {_ago(now - monitor_at)}: recent activity unknown"
    if last_activity is not None and now - last_activity <= ACTIVE_WINDOW:
        return ACTIVE, f"last activity {_ago(now - last_activity)} ago"
    if last_activity is not None and now - last_activity <= INACTIVITY:
        return QUIET, f"no activity in 24 h; last {_ago(now - last_activity)} ago"
    covered = now - monitored_since if monitored_since else timedelta(0)
    if covered >= INACTIVITY:
        last = f"last activity {_ago(now - last_activity)} ago" if last_activity else "no activity ever recorded"
        return INACTIVE, f"no launch, trade or migration for 7 days ({last}); adapter kept, reactivates on activity"
    return UNVERIFIED, (f"no activity recorded in {_ago(covered)} of monitoring; "
                        "7 days are needed before a venue is called inactive")


def _ago(d: timedelta) -> str:
    s = int(d.total_seconds())
    if s < 3600:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def _probe_venues() -> tuple[str, ...]:
    from yonixalpha_core.solana.venue_probe import VENUES

    return tuple(VENUES)


def _dt(v) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _max(*vals):
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


async def _evm(session: AsyncSession, spec: LaunchpadSpec, now: datetime) -> dict[str, Any]:
    t = LaunchpadActivity
    since = (now - INACTIVITY).date()
    row = (await session.execute(select(
        func.coalesce(func.sum(t.launches).filter(t.day >= since), 0),
        func.coalesce(func.sum(t.trades).filter(t.day >= since), 0),
        func.coalesce(func.sum(t.migrations).filter(t.day >= since), 0),
        func.coalesce(func.sum(t.volume).filter(t.day >= since), 0),
        func.max(t.last_launch_at), func.max(t.last_trade_at), func.max(t.last_migration_at), func.min(t.day),
    ).where(t.chain == spec.chain.value, t.launchpad == spec.key))).one()
    cursor = await session.get(EvmCursor, (spec.chain.value, spec.key))
    return {"launches_7d": int(row[0]), "trades_7d": int(row[1]), "migrations_7d": int(row[2]),
            "volume_7d": str(Decimal(row[3]) / E18), "last_launch": row[4], "last_trade": row[5],
            "last_migration": row[6],
            "rollup_since": datetime.combine(row[7], datetime.min.time(), timezone.utc) if row[7] else None,
            "monitor_at": cursor.updated_at if cursor else None}


async def _solana(session: AsyncSession, spec: LaunchpadSpec, now: datetime) -> dict[str, Any]:
    since = now - INACTIVITY
    out: dict[str, Any] = {"launches_7d": None, "trades_7d": None, "migrations_7d": None, "volume_7d": None,
                           "last_launch": None, "last_trade": None, "last_migration": None, "monitor_at": None,
                           "rollup_since": None}
    if spec.key == "pumpfun":
        o = TokenObservation
        # separate index lookups: one count with filter read the whole table
        n = (await session.execute(select(func.count()).where(o.decided_at >= since))).scalar_one()
        last, first = (await session.execute(select(func.max(o.decided_at), func.min(o.decided_at)))).one()
        out.update(launches_7d=int(n), last_launch=last, rollup_since=first)
    elif spec.key in _probe_venues():
        lc = LaunchpadCheck
        rows = (await session.execute(select(lc.checked_at, lc.evidence).where(
            lc.launchpad == spec.key, lc.check == "ACTIVE", lc.checked_at >= since)
            .order_by(lc.checked_at.desc()).limit(2100))).all()  # 7 days of 5-minute probes
        first = (await session.execute(select(func.min(lc.checked_at)).where(lc.launchpad == spec.key))).scalar_one()
        seen: dict[str, datetime] = {}
        last_tx = None
        for _, ev in rows:
            ev = ev or {}
            last_tx = _max(last_tx, _dt(ev.get("last_tx_at")))
            for k, v in (ev.get("last_seen") or {}).items():
                seen[k] = _max(seen.get(k), _dt(v))
        latest = rows[0][1] if rows else {}
        out.update(last_launch=seen.get("launch"), last_trade=seen.get("trade"), last_migration=seen.get("migration"),
                   rollup_since=first, monitor_at=rows[0][0] if rows else None, last_tx=last_tx,
                   probe={k: (latest or {}).get(k) for k in ("rate_per_min", "span_s", "sampled", "sample_kinds",
                                                             "unknown_instructions", "error", "last_tx_at")})
    elif spec.key == "pumpswap":
        e = TokenEvent
        n, last, first = (await session.execute(select(
            func.count().filter(e.occurred_at >= since), func.max(e.occurred_at), func.min(e.occurred_at)).where(
            e.event_type == "migration"))).one()
        out.update(migrations_7d=int(n), last_migration=last, rollup_since=first)
    return out


async def launchpad_activity(session: AsyncSession, spec: LaunchpadSpec, operator_mode: str,
                             verification: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    data = await (_solana if spec.chain == Chain.SOLANA else _evm)(session, spec, now)
    lc = LaunchpadCheck
    first_check, last_event = (await session.execute(select(
        func.min(lc.checked_at),
        func.max(lc.checked_at).filter(lc.status == "PASS", lc.check.in_(("DISCOVERY", "EVENTS"))),
    ).where(lc.launchpad == spec.key))).one()
    if "last_tx" in data:  # activity probe: the newest transaction itself, not when the probe ran
        last_event = None
    last_activity = _max(data["last_launch"], data["last_trade"], data["last_migration"], last_event,
                         data.get("last_tx"))
    disabled = (spec.inactive_reason or "venue inactive") if not spec.active else (
        "switched off by the operator" if operator_mode == "OFF" else None)
    starts = [x for x in (data["rollup_since"], first_check) if x is not None]
    status, why = classify(disabled_reason=disabled, last_activity=last_activity,
                           monitored_since=min(starts) if starts else None, monitor_at=data["monitor_at"], now=now)
    checks = (verification or {}).get("checks") or {}

    def passed(c: str) -> bool:
        return (checks.get(c) or {}).get("status") == "PASS"

    return {"activity_status": status, "activity_why": why, "listed": status in LISTED,
            "last_launch": data["last_launch"], "last_trade": data["last_trade"],
            "last_migration": data["last_migration"], "last_verified_event": last_event,
            "launches_7d": data["launches_7d"], "trades_7d": data["trades_7d"],
            "migrations_7d": data["migrations_7d"], "volume_7d": data["volume_7d"],
            "monitor_at": data["monitor_at"], "probe": data.get("probe"), "last_transaction": data.get("last_tx"),
            "event_monitor_verified": passed("EVENTS"), "buy_verified": passed("BUY"),
            "sell_verified": passed("SELL"), "execution_verified": passed("BUY") and passed("SELL")}
