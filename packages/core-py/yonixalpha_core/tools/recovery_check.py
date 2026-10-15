"""Checks that the recovery changes R2-R6, R9 and R10 are live (read only).

    $C exec -T api python -m yonixalpha_core.tools.recovery_check [--hours 24]

One READ ONLY transaction (60 s statement timeout), time-bounded queries on
indexed columns, and Redis reads. For each change it prints the effective
settings and what the services did in the last --hours: refusals, stop
floors, settled paper entries, migration crossings, pool-priced outcome
horizons and slowed re-checks. "none yet" means the situation has not
occurred since the deploy, not that the change failed. Nothing is written.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from yonixalpha_core import rpc_usage
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.safety import store

SCOPES = ("GLOBAL", "solana_fresh", "solana_migration", "solana_momentum")
KEYS = ("migration_max_entry_market_cap_sol", "low_volatility_stop_threshold", "low_volatility_min_stop_pct",
        "cooldown_scope", "cooldown_after_loss_seconds", "min_stop_pct")


async def build(s, redis, since: datetime, now: datetime) -> list[str]:
    out = [f"RECOVERY CHECK, {since:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M} UTC (read only)"]

    out.append("\n== settings in effect ==")
    for scope in SCOPES:
        eff, _ = await store.load_settings(s, scope)
        out.append(f"  {scope:17s} " + "  ".join(f"{k}={getattr(eff, k)}" for k in KEYS))

    out.append("\n== R2: established tokens refused by the migration engine ==")
    r2 = (await s.execute(text(
        "SELECT count(*), count(DISTINCT asset_id) FROM risk_assessments WHERE engine = 'solana_migration' "
        "AND evaluated_at >= :since AND assessment->'findings' @> '[{\"code\": \"ESTABLISHED_TOKEN_NOT_MIGRATION\"}]'"),
        {"since": since})).one()
    out.append(f"  refusals {r2[0]} (tokens {r2[1]})" if r2[0] else "  none yet (no migration decision above the cap)")

    out.append("\n== R3: Solana stops widened by the low-volatility floor ==")
    r3 = (await s.execute(text(
        "SELECT count(*) FROM risk_assessments WHERE engine LIKE 'solana%' AND evaluated_at >= :since "
        "AND assessment->'plan'->'stop_loss'->>'method' LIKE '%low-volatility floor%'"), {"since": since})).scalar_one()
    out.append(f"  plans with the floor {r3}" if r3 else "  none yet (no decision with volatility below the threshold)")

    out.append("\n== R4 / R5: paper realism ==")
    rows = (await s.execute(text(
        "SELECT event_type, detail->>'price_source' src, (detail->>'measured')::text measured, count(*) "
        "FROM trade_timeline_events WHERE occurred_at >= :since "
        "AND event_type IN ('paper_entry_delay_fill', 'paper_migration_crossing') GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"),
        {"since": since})).all()
    for r in rows:
        what = "migration crossing (unmanaged for the LIVE sell latency)" if r[0] == "paper_migration_crossing" \
            else f"entry fill via {r[1] or 'pump_stream trades'}, measured={r[2]}"
        out.append(f"  {what}: {r[3]}")
    if not rows:
        out.append("  none yet (no paper entry or migration crossing)")

    out.append("\n== R6: outcome horizons after a migration ==")
    r6 = (await s.execute(text(
        "SELECT count(*) FILTER (WHERE h.value->>'source' = 'pumpswap pool trades (recorded)'), "
        "count(*) FILTER (WHERE h.value->>'unavailable' LIKE '%PumpSwap%'), "
        "count(*) FILTER (WHERE o.migrated_at IS NOT NULL AND h.value->>'price_raw' LIKE '0.00041088%') "
        "FROM opportunity_outcomes o, jsonb_each(o.horizons) h WHERE o.decided_at >= :since"), {"since": since})).one()
    out.append(f"  priced from the pool {r6[0]}, unavailable (pool not read) {r6[1]}")
    out.append(f"  frozen graduation price on migrated tokens {r6[2]} (the old bug: should stay 0)")

    out.append("\n== R9: re-checks slowed after a token-level block (today, UTC) ==")
    backoff = await rpc_usage.token_backoff_today(redis, now) if redis is not None else {}
    out += [f"  {k:<48} {v}" for k, v in sorted(backoff.items(), key=lambda kv: -kv[1])] or ["  none yet"]

    out.append("\n== R10: loss cooldown ==")
    eff, _ = await store.load_settings(s, "GLOBAL")
    out.append(f"  cooldown_scope {eff.cooldown_scope}: " + (
        "any losing exit pauses every strategy (unchanged default)" if eff.cooldown_scope == "ACCOUNT"
        else "only the strategy that lost waits"))
    out.append("\nNothing was written.")
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=max(1, min(a.hours, 24 * 7)))
    settings = get_settings()
    engine = make_engine(settings)
    redis = make_redis(settings)
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            await s.execute(text("SET LOCAL statement_timeout = '60s'"))
            lines = await build(s, redis, since, now)
            await s.rollback()
    finally:
        await redis.aclose()
        await engine.dispose()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
