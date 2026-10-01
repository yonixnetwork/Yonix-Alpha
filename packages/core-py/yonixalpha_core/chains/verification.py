"""Launchpad status from evidence.

A launchpad's status is never set by hand. It is computed from check
results (launchpad_checks rows written by on-chain verification, the EVM
discovery service and confirmed orders; for Solana, from the production
system's own records) and the operator mode:

  DISABLED    the venue is inactive, observe-only, or the operator set OFF
  LIVE        every check passed (including a real BUY and SELL) and the
              operator mode is LIVE
  PAPER_ONLY  ACTIVE, DISCOVERY, EVENTS, QUOTE and SAFETY passed
  DEGRADED    a required check that once passed has failed or gone stale
  UNVERIFIED  implemented, never proven on the real chain

Liveness checks (ACTIVE, DISCOVERY, EVENTS, QUOTE, LIQUIDITY, SAFETY) expire
after LIVENESS_HOURS; capability checks (BUY, SELL, MIGRATION_DETECTION,
TX_MONITORING) stay proven once a real transaction proved them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains.base import CHECKS, LIVE_REQUIRED, PAPER_REQUIRED, LaunchpadSpec, LaunchpadStatus
from yonixalpha_core.db.models import ExecutionOrder, LaunchpadCheck, OpportunityOutcome, RiskAssessment

SOLANA_PRODUCTION = ("pumpfun", "pumpswap")
LIVENESS = ("ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "LIQUIDITY", "SAFETY")
LIVENESS_HOURS = 24


def _valid(check: str, row: dict[str, Any], now: datetime) -> bool:
    if row.get("status") != "PASS":
        return False
    if check in LIVENESS:
        at = row.get("at")
        return at is not None and now - at <= timedelta(hours=LIVENESS_HOURS)
    return True


def compute_status(spec: LaunchpadSpec, checks: dict[str, dict[str, Any]], operator_mode: str, now: datetime) -> dict[str, Any]:
    """checks: check -> {"status": PASS|FAIL, "at": datetime, "evidence": ..., "ever_passed": bool}."""
    valid = {c for c in CHECKS if c in checks and _valid(c, checks[c], now)}
    missing_paper = [c for c in PAPER_REQUIRED if c not in valid]
    missing_live = [c for c in LIVE_REQUIRED if c not in valid]
    lost = [c for c in PAPER_REQUIRED if c not in valid and checks.get(c, {}).get("ever_passed")]
    if not spec.active:
        status, why = LaunchpadStatus.DISABLED, spec.inactive_reason or "venue inactive"
    elif not spec.supports_trading:
        status, why = LaunchpadStatus.DISABLED, "observe only: trading not supported for this venue"
    elif operator_mode == "OFF":
        status, why = LaunchpadStatus.DISABLED, "switched off by the operator"
    elif not missing_live and operator_mode == "LIVE":
        status, why = LaunchpadStatus.LIVE, "every check passed, including a real buy and sell"
    elif not missing_paper:
        why = ("paper trading verified; live needs " + ", ".join(missing_live)) if missing_live else \
              "every check passed; operator mode is PAPER"
        status = LaunchpadStatus.PAPER_ONLY
    elif lost:
        status, why = LaunchpadStatus.DEGRADED, "previously verified, now failing or stale: " + ", ".join(lost)
    else:
        status, why = LaunchpadStatus.UNVERIFIED, "not yet proven on the real chain: " + ", ".join(missing_paper)
    return {"status": status.value, "why": why, "operator_mode": operator_mode,
            "paper_allowed": status in (LaunchpadStatus.PAPER_ONLY, LaunchpadStatus.LIVE),
            "live_allowed": status == LaunchpadStatus.LIVE,
            "missing_for_paper": missing_paper, "missing_for_live": missing_live}


async def recorded_checks(session: AsyncSession, launchpad: str) -> dict[str, dict[str, Any]]:
    """Latest row per check, plus whether it ever passed."""
    lc = LaunchpadCheck
    latest = (await session.execute(
        select(lc).where(lc.launchpad == launchpad).distinct(lc.check).order_by(lc.check, lc.checked_at.desc()))).scalars().all()
    ever = dict((await session.execute(select(lc.check, func.count()).where(
        lc.launchpad == launchpad, lc.status == "PASS").group_by(lc.check))).all())
    return {r.check: {"status": r.status, "at": r.checked_at, "evidence": r.evidence, "source": r.source,
                      "ever_passed": bool(ever.get(r.check))} for r in latest}


async def record(session: AsyncSession, launchpad: str, check: str, ok: bool, evidence: dict | None, source: str,
                 now: datetime | None = None) -> None:
    if check not in CHECKS:
        raise ValueError(f"unknown check {check}")
    session.add(LaunchpadCheck(launchpad=launchpad, check=check, status="PASS" if ok else "FAIL",
                               evidence=evidence, source=source, checked_at=now or datetime.now(timezone.utc)))


async def solana_checks(session: AsyncSession, redis, key: str, now: datetime) -> dict[str, dict[str, Any]]:
    """Solana evidence from the production system itself: stream liveness,
    recent assessments, confirmed LIVE orders on the route, migrations."""
    from yonixalpha_core.solana import pump_stream

    out: dict[str, dict[str, Any]] = {}

    def put(check: str, ok: bool, evidence: dict, at: datetime | None = None) -> None:
        out[check] = {"status": "PASS" if ok else "FAIL", "at": at or now, "evidence": evidence,
                      "source": "production records", "ever_passed": ok}

    hb = await pump_stream.heartbeat(redis) if redis is not None else None
    hb_age = (now - hb).total_seconds() if hb else None
    launches = int(await redis.zcount(pump_stream.RECENT, now.timestamp() - 3600, now.timestamp())) if redis is not None else 0
    alive = hb_age is not None and hb_age <= 120
    put("ACTIVE", alive, {"stream_heartbeat_age_s": hb_age})
    if key == "pumpfun":
        put("DISCOVERY", alive and launches > 0, {"launches_last_hour": launches})
        put("EVENTS", alive and launches > 0, {"launches_last_hour": launches})
        put("QUOTE", alive, {"source": "bonding-curve reserves from the stream"})
        put("LIQUIDITY", alive, {"source": "bonding-curve real SOL reserve"})
    else:
        migrated = int(await redis.zcount(pump_stream.MIGRATED, now.timestamp() - 86400, now.timestamp())) if redis else 0
        put("DISCOVERY", migrated > 0, {"migrations_last_24h": migrated})
        put("EVENTS", migrated > 0, {"migrations_last_24h": migrated})
        put("QUOTE", alive, {"source": "PumpSwap pool reserves (RPC)"})
        put("LIQUIDITY", alive, {"source": "PumpSwap pool reserves (RPC)"})
    assessed = (await session.execute(select(func.count()).where(
        RiskAssessment.evaluated_at >= now - timedelta(hours=LIVENESS_HOURS),
        RiskAssessment.engine.in_(("solana_fresh", "solana_momentum", "solana_migration"))))).scalar_one()
    put("SAFETY", assessed > 0, {"gate_assessments_24h": assessed})
    route = "pump" if key == "pumpfun" else "pump-amm"
    eo = ExecutionOrder
    rows = dict((await session.execute(select(eo.side, func.count()).where(
        eo.mode == "LIVE", eo.route == route, eo.status == "CONFIRMED").group_by(eo.side))).all())
    put("BUY", bool(rows.get("BUY")), {"confirmed_live_buys": rows.get("BUY", 0), "route": route})
    put("SELL", bool(rows.get("SELL")), {"confirmed_live_sells": rows.get("SELL", 0), "route": route})
    put("TX_MONITORING", bool(rows), {"confirmed_live_orders": sum(rows.values()), "route": route})
    mig = (await session.execute(select(func.count()).where(OpportunityOutcome.migrated_at.is_not(None)))).scalar_one()
    put("MIGRATION_DETECTION", mig > 0, {"ledger_rows_with_migration": mig})
    return out


async def status_for(session: AsyncSession, redis, spec: LaunchpadSpec, operator_mode: str,
                     now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    # Pump.fun / PumpSwap evidence comes from the production system; every other
    # venue (EVM, and the observe-only Solana venues of the activity probe) from
    # recorded launchpad_checks rows.
    checks = await solana_checks(session, redis, spec.key, now) if spec.key in SOLANA_PRODUCTION \
        else await recorded_checks(session, spec.key)
    st = compute_status(spec, checks, operator_mode, now)
    st["checks"] = {c: {"status": checks[c]["status"], "at": checks[c]["at"].isoformat() if checks[c].get("at") else None,
                        "evidence": checks[c].get("evidence"), "source": checks[c].get("source")}
                    if c in checks else {"status": "NOT_RUN"} for c in CHECKS}
    return st
