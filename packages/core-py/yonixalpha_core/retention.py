"""Data retention: removes short-lived operational history older than
`days` (platform setting `data_retention`, default 3 days, OFF until the
operator enables it after a dry run), and never anything a trade, the ML
models or an audit depends on.

Removed when older than the cutoff:
  notifications            every row
  system_events            severity info / warning (errors and criticals are kept)
  risk_assessments         decisions that did not trade: not executable and
                           not referenced by a position, an order or an ML
                           feature row
  trading_candidates       finished (rejected / closed / migrated) tokens with
                           no position, no ML feature row and no entry signal
                           linked to them; their timeline events go with them
  trade_timeline_events    events not tied to a position
  token_events             every kind except "migration" (kept MIGRATION_EVENT_DAYS:
                           the launchpad health 7-day activity rule counts them)
  tokens                   tokens nothing refers to any more (no candidate, no
                           event, no position)

Never touched: positions, orders, fills, ML feature rows, opportunity
outcomes (ML labels), entry signals, model versions, validation sets, launch
buyers and deployer history (wallet / creator intelligence), wallet and EVM
tables (their own retention), audit logs, users, settings, risk events.

ML integrity: the gate models group samples by candidate and deduplicate by
assessment (services/ml/app/gate_ml.py, dataset.py). Any candidate or
assessment an ML feature row points to is kept, so no sample loses its
group.

Deletes run in batches of BATCH rows (at most MAX_BATCHES per table per
run), each in its own short transaction, so a 2 GB host is never locked up.
Postgres reuses the freed space; the files on disk shrink only after a
VACUUM FULL, which this job never runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SETTINGS_KEY = "data_retention"
DEFAULT_DAYS = 3
MIN_DAYS, MAX_DAYS = 2, 365
MIGRATION_EVENT_DAYS = 8
BATCH = 5000
MAX_BATCHES = 40  # 200 000 rows per table per run; the backlog is caught up over several runs

# (name, table, predicate). :cutoff is the retention cutoff, :mig_cutoff the
# migration-event cutoff. Order matters: assessments and candidates before
# the tokens they reference.
RULES: list[tuple[str, str, str]] = [
    ("notifications", "notifications", "t.created_at < :cutoff"),
    ("system_events", "system_events", "t.created_at < :cutoff AND t.severity IN ('info', 'warning', 'debug')"),
    ("risk_assessments", "risk_assessments", """t.evaluated_at < :cutoff AND NOT t.executable
        AND NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.assessment_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM execution_orders o WHERE o.assessment_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM ml_features m WHERE m.assessment_id = t.id)"""),
    ("trading_candidates", "trading_candidates", """t.created_at < :cutoff AND t.state IN ('rejected', 'closed', 'migrated')
        AND NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.candidate_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM ml_features m WHERE m.candidate_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM entry_signals e WHERE e.candidate_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM risk_assessments r WHERE r.candidate_id = t.id)"""),
    ("trade_timeline_events", "trade_timeline_events", "t.occurred_at < :cutoff AND t.position_id IS NULL"),
    ("token_events", "token_events", """(t.occurred_at < :cutoff AND t.event_type <> 'migration')
        OR (t.occurred_at < :mig_cutoff AND t.event_type = 'migration')"""),
    ("tokens", "tokens", """t.first_seen_at < :cutoff
        AND NOT EXISTS (SELECT 1 FROM trading_candidates c WHERE c.token_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM token_events e WHERE e.token_id = t.id)
        AND NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.asset_id = t.mint_address)"""),
]
KEPT = ("paper_positions", "execution_orders", "paper_orders", "fills", "ml_features", "opportunity_outcomes",
        "entry_signals", "model_versions", "ml_validation_sets", "ml_validation_reports", "launch_buyers",
        "deployer_launches", "wallet_profiles", "wallet_trade_labels", "audit_logs", "users", "risk_events",
        "platform_settings", "risk_settings", "strategy_configs")


def parse_settings(value: dict | None) -> dict[str, Any]:
    value = value or {}
    days = value.get("days", DEFAULT_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return {"enabled": bool(value.get("enabled", False)), "days": min(MAX_DAYS, max(MIN_DAYS, days))}


async def load_settings(session: AsyncSession) -> dict[str, Any]:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, SETTINGS_KEY)
    return parse_settings(row.value if row else None)


async def save_settings(session: AsyncSession, enabled: bool, days: int, by: str) -> dict[str, Any]:
    from yonixalpha_core.db.models import AuditLog, PlatformSetting

    value = parse_settings({"enabled": enabled, "days": days})
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    before = dict(row.value) if row else None
    if row is None:
        session.add(PlatformSetting(key=SETTINGS_KEY, value=value))
    else:
        row.value = value
    session.add(AuditLog(event_type="retention.settings_updated", detail={"before": before, "after": value, "by": by}))
    await session.commit()
    return value


def _params(now: datetime, days: int) -> dict[str, datetime]:
    return {"cutoff": now - timedelta(days=days), "mig_cutoff": now - timedelta(days=max(days, MIGRATION_EVENT_DAYS))}


async def preview(session_factory, now: datetime, days: int | None = None) -> dict[str, Any]:
    """What a run would remove now (counts only; nothing is written)."""
    async with session_factory() as session:
        s = await load_settings(session)
        d = days or s["days"]
        out: dict[str, Any] = {"days": d, "enabled": s["enabled"], "would_delete": {}, "kept_tables": list(KEPT)}
        for name, table, pred in RULES:
            n = (await session.execute(text(f"SELECT count(*) FROM {table} t WHERE {pred}"), _params(now, d))).scalar_one()
            out["would_delete"][name] = int(n)
    return out


async def run(session_factory, now: datetime, *, force: bool = False, days: int | None = None) -> dict[str, Any]:
    """One retention pass. Does nothing unless enabled (or `force`)."""
    async with session_factory() as session:
        s = await load_settings(session)
    if not (s["enabled"] or force):
        return {"skipped": "disabled"}
    d = days or s["days"]
    params = _params(now, d)
    deleted: dict[str, int] = {}
    for name, table, pred in RULES:
        total = 0
        for _ in range(MAX_BATCHES):
            async with session_factory() as session:
                res = await session.execute(text(
                    f"DELETE FROM {table} WHERE id IN (SELECT t.id FROM {table} t WHERE {pred} LIMIT {BATCH})"), params)
                await session.commit()
            total += res.rowcount or 0
            if (res.rowcount or 0) < BATCH:
                break
        deleted[name] = total
    out = {"days": d, "deleted": deleted, "at": now.isoformat()}
    if any(deleted.values()):
        from yonixalpha_core.db.models import AuditLog

        async with session_factory() as session:
            session.add(AuditLog(event_type="retention.run", detail=out))
            await session.commit()
    return out
