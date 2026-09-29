"""Intelligence report: is the launch intelligence, wallet intelligence,
ledger v2 and shadow ML actually running on this deployment? Read-only
(SELECT queries and Redis reads); writes nothing, prints no secret.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.intel_report [--hours 24] [--json]
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core import wallet_intel
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis

INTEL_CODES = ("MAYHEM_OR_NONSTANDARD_CURVE", "MAYHEM_FLAG_UNKNOWN", "MANIPULATION_HIGH", "MANIPULATION_MEDIUM", "INSTANT_BOND",
               "BOOST_WINDOW", "POST_MIGRATION_DUMPING", "DUMP_CLUSTER_HIGH", "MANUFACTURED_PUMP_PATTERN",
               "LOW_EFFECTIVE_BUYERS", "LOW_ORGANIC_DEMAND", "HIGH_COORDINATION", "CREATOR_CONCENTRATION",
               "HIGH_SMART_MONEY_CONCENTRATION", "POOR_DEPLOYER_HISTORY")

QUERIES: dict[str, str] = {
    "ledger_status": "SELECT status, count(*) FROM opportunity_outcomes WHERE decided_at >= :since GROUP BY 1 ORDER BY 1",
    "ledger_intel_coverage": """SELECT stage, count(*) AS rows, count(*) FILTER (WHERE snapshot ? 'intel' AND snapshot->'intel' IS NOT NULL
               AND snapshot->'intel' <> 'null'::jsonb AND NOT (snapshot->'intel' ? 'error')) AS with_intel,
               count(*) FILTER (WHERE snapshot->'intel' ? 'error') AS intel_errors
               FROM opportunity_outcomes WHERE decided_at >= :since GROUP BY 1 ORDER BY 1""",
    "feature_versions": "SELECT coalesce(feature_version, '(none)'), count(*) FROM opportunity_outcomes WHERE decided_at >= :since GROUP BY 1",
    "counterfactual": """SELECT analysis->'counterfactual'->>'classification', count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since AND analysis ? 'counterfactual' GROUP BY 1 ORDER BY 2 DESC""",
    "exit_classes": """SELECT post_exit->>'classification', count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since AND post_exit IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""",
    "executable_unknown_reasons": """SELECT left(path->'T+5m'->'executable'->>'unknown', 70) AS reason, count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since AND status = 'COMPLETE' AND path->'T+5m'->'executable' ? 'unknown'
               GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
    "signal_vs_execution": """SELECT count(*), round(avg(theoretical_return_pct), 2), round(avg(executable_return_pct), 2)
               FROM opportunity_outcomes WHERE decided_at >= :since AND executable_return_pct IS NOT NULL""",
    "manipulation_levels": """SELECT snapshot->'intel'->'manipulation'->>'level', count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since AND snapshot ? 'intel' GROUP BY 1 ORDER BY 2 DESC""",
    "dump_cluster_levels": """SELECT snapshot->'intel'->'wallets'->'dump_cluster'->>'level', count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since AND snapshot ? 'intel' GROUP BY 1 ORDER BY 2 DESC""",
    "manipulation_families": """SELECT k, count(*) FROM opportunity_outcomes,
               jsonb_object_keys(snapshot->'intel'->'manipulation'->'families') k
               WHERE decided_at >= :since AND jsonb_typeof(snapshot->'intel'->'manipulation'->'families') = 'object'
               GROUP BY 1 ORDER BY 2 DESC""",
    # Does the signal predict anything? 30-minute outcome by level at the
    # decision (rows old enough for their 30-minute peak / drawdown).
    "outcome_by_manipulation (n | % up 50+ | % down 50+)": """SELECT snapshot->'intel'->'manipulation'->>'level', count(*),
               round(100.0 * avg((peak_pct >= 50)::int), 1), round(100.0 * avg((drawdown_pct <= -50)::int), 1)
               FROM opportunity_outcomes WHERE decided_at >= :since AND decided_at <= now() - interval '31 minutes'
               AND snapshot->'intel' ? 'manipulation' GROUP BY 1 ORDER BY 1""",
    "outcome_by_family (n | % up 50+ | % down 50+)": """SELECT k, count(*), round(100.0 * avg((peak_pct >= 50)::int), 1),
               round(100.0 * avg((drawdown_pct <= -50)::int), 1)
               FROM opportunity_outcomes, jsonb_object_keys(snapshot->'intel'->'manipulation'->'families') k
               WHERE decided_at >= :since AND decided_at <= now() - interval '31 minutes'
               AND jsonb_typeof(snapshot->'intel'->'manipulation'->'families') = 'object' GROUP BY 1
               UNION ALL SELECT '(all measured launches)', count(*), round(100.0 * avg((peak_pct >= 50)::int), 1),
               round(100.0 * avg((drawdown_pct <= -50)::int), 1) FROM opportunity_outcomes
               WHERE decided_at >= :since AND decided_at <= now() - interval '31 minutes'
               AND snapshot->'intel'->'manipulation'->>'level' IN ('NONE', 'LOW', 'MEDIUM', 'HIGH')
               ORDER BY 2 DESC""",
    "outcome_by_dump_cluster (n | % up 50+ | % down 50+)": """SELECT snapshot->'intel'->'wallets'->'dump_cluster'->>'level', count(*),
               round(100.0 * avg((peak_pct >= 50)::int), 1), round(100.0 * avg((drawdown_pct <= -50)::int), 1)
               FROM opportunity_outcomes WHERE decided_at >= :since AND decided_at <= now() - interval '31 minutes'
               AND snapshot->'intel' ? 'wallets' GROUP BY 1 ORDER BY 1""",
    # What a trade would have got: the counterfactual TP (+30%) / stop (-25%)
    # rule entered at the decision, and the executable return at T+5m.
    "rule_by_manipulation (n | % TP first | % stop first | % missed win | avg exec T+5m %)": """
               SELECT snapshot->'intel'->'manipulation'->>'level', count(*),
               round(100.0 * avg((analysis->'counterfactual'->>'rule_exit' = 'TP')::int), 1),
               round(100.0 * avg((analysis->'counterfactual'->>'rule_exit' = 'STOP')::int), 1),
               round(100.0 * avg((analysis->'counterfactual'->>'classification' = 'MISSED_WIN')::int), 1),
               round(avg(executable_return_pct), 2)
               FROM opportunity_outcomes WHERE decided_at >= :since AND analysis ? 'counterfactual'
               AND snapshot->'intel' ? 'manipulation' GROUP BY 1 ORDER BY 1""",
    "rule_by_dump_cluster (n | % TP first | % stop first | % missed win | avg exec T+5m %)": """
               SELECT snapshot->'intel'->'wallets'->'dump_cluster'->>'level', count(*),
               round(100.0 * avg((analysis->'counterfactual'->>'rule_exit' = 'TP')::int), 1),
               round(100.0 * avg((analysis->'counterfactual'->>'rule_exit' = 'STOP')::int), 1),
               round(100.0 * avg((analysis->'counterfactual'->>'classification' = 'MISSED_WIN')::int), 1),
               round(avg(executable_return_pct), 2)
               FROM opportunity_outcomes WHERE decided_at >= :since AND analysis ? 'counterfactual'
               AND snapshot->'intel' ? 'wallets' GROUP BY 1 ORDER BY 1""",
    "launch_buyers": """SELECT count(*) AS rows, count(DISTINCT mint) AS launches, count(DISTINCT wallet) AS wallets,
               count(*) FILTER (WHERE outcome_resolved_at IS NOT NULL) AS resolved,
               count(*) FILTER (WHERE sold_early) AS sold_early, count(*) FILTER (WHERE sold_early IS NULL AND early_window_closed) AS sold_early_unknown
               FROM launch_buyers WHERE recorded_at >= :since""",
    "launch_outcomes": "SELECT outcome, count(DISTINCT mint) FROM launch_buyers WHERE outcome_resolved_at >= :since GROUP BY 1",
    "gate_intel_findings": """SELECT f->>'code', count(*) FROM risk_assessments, jsonb_array_elements(assessment->'findings') f
               WHERE evaluated_at >= :since AND f->>'code' = ANY(:codes) GROUP BY 1 ORDER BY 2 DESC""",
    "intel_assembly_errors": """SELECT left(e, 90), count(*) FROM risk_assessments,
               jsonb_array_elements_text(coalesce(assessment->'inputs_snapshot'->'errors', '[]'::jsonb)) e
               WHERE evaluated_at >= :since AND (e LIKE 'intel:%' OR e LIKE 'wallet_intel:%') GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
    "shadow_models": """SELECT name, version, trained_at, metrics->'holdout'->>'roc_auc', metrics->'holdout'->>'pr_auc',
               metrics->'holdout'->>'n', metrics->'holdout'->>'positives' FROM model_versions WHERE status = 'shadow' ORDER BY name""",
    "shadow_scored_rows": "SELECT count(*) FROM opportunity_outcomes WHERE decided_at >= :since AND ml_shadow IS NOT NULL",
    # Why P_MIGRATE has no positives: do tracked launches ever migrate, and
    # does the stream record migrations at all?
    "migrations (ledger rows migrated within 60m | fresh rows | max migrations/hour seen at a decision)": """
               SELECT count(*) FILTER (WHERE migrated_at IS NOT NULL), count(*) FILTER (WHERE engine = 'solana_fresh'),
               max((regime->>'migrations_last_hour_seen')::int) FROM opportunity_outcomes WHERE decided_at >= :since""",
    # --- scanner intelligence (wallet graph, organic demand, deployer, manufactured pump)
    "scanner: relationships status | demand status | rows": """SELECT snapshot->'intel'->'relationships'->>'status',
               snapshot->'intel'->'relationships'->'demand'->>'status', count(*) FROM opportunity_outcomes
               WHERE decided_at >= :since GROUP BY 1, 2 ORDER BY 3 DESC""",
    "scanner: avg raw buyers | avg effective buyers | avg funding-known share | rows with a dependent cluster | rows": """
               SELECT round(avg((r->'buyers'->>'raw_unique_buyers')::float)::numeric, 2),
               round(avg((r->'buyers'->>'effective_unique_buyers')::float)::numeric, 2),
               round(avg((r->'coverage'->>'funding_known_share')::float)::numeric, 3),
               count(*) FILTER (WHERE (r->>'largest_dependent_cluster')::int >= 2), count(*)
               FROM (SELECT snapshot->'intel'->'relationships' r FROM opportunity_outcomes WHERE decided_at >= :since) q
               WHERE r->>'status' = 'MEASURED'""",
    "scanner: manufactured-pump risk | rows | fast dump % | rug % | upside_100 % (completed)": """
               SELECT snapshot->'intel'->'manufactured_pump'->>'risk', count(*),
               round(100.0 * avg((labels->>'fast_dump')::boolean::int), 1), round(100.0 * avg((labels->>'rug_60m')::boolean::int), 1),
               round(100.0 * avg((labels->>'upside_100')::boolean::int), 1) FROM opportunity_outcomes
               WHERE decided_at >= :since AND status = 'COMPLETE' GROUP BY 1 ORDER BY 2 DESC""",
    "scanner: deployer status | rows | avg risk score (measured)": """SELECT snapshot->'intel'->'deployer'->>'status', count(*),
               round(avg((snapshot->'intel'->'deployer'->>'deployer_risk_score')::float)::numeric, 3) FROM opportunity_outcomes
               WHERE decided_at >= :since GROUP BY 1 ORDER BY 2 DESC""",
    "deployer_launches: noted | resolved | distinct creators | creators with >= 3 resolved": """
               SELECT count(*), count(resolved_at), count(DISTINCT creator),
               (SELECT count(*) FROM (SELECT creator FROM deployer_launches WHERE resolved_at IS NOT NULL
                GROUP BY creator HAVING count(*) >= 3) c) FROM deployer_launches WHERE first_seen_at >= :since""",
    "follow-up after rejection: rows | independent buyers arrived % | organic demand rose % | cluster exited % | creator sold %": """
               SELECT count(*), round(100.0 * avg((f->>'independent_buyers_arrived')::boolean::int), 1),
               round(100.0 * avg((f->>'organic_demand_increased')::boolean::int), 1),
               round(100.0 * avg((f->>'cluster_exited')::boolean::int), 1),
               round(100.0 * avg((f->>'creator_sold_after_decision')::boolean::int), 1)
               FROM (SELECT analysis->'relationship_followup' f FROM opportunity_outcomes
                     WHERE decided_at >= :since AND traded = false AND analysis ? 'relationship_followup') q""",
}


async def collect(session, redis, since: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"since": since.isoformat()}
    for name, sql in QUERIES.items():
        try:
            rows = (await session.execute(text(sql), {"since": since, "codes": list(INTEL_CODES)})).all()
            out[name] = [[str(v) if v is not None else None for v in r] for r in rows]
        except Exception as exc:  # noqa: BLE001 - e.g. migration 0019 not applied yet
            await session.rollback()
            out[name] = f"query failed: {type(exc).__name__}: {str(exc)[:160]}"
    try:
        out["wallet_base_counters"] = {**await redis.hgetall(wallet_intel.BASE),
                                       "rebuilt_at": await redis.get(wallet_intel.BUILT)}
    except Exception as exc:  # noqa: BLE001
        out["wallet_base_counters"] = f"redis failed: {type(exc).__name__}"
    try:
        from yonixalpha_core.solana import pump_stream

        out["stream_migrations_recorded (since | total kept 24 h)"] = [[
            str(await redis.zcount(pump_stream.MIGRATED, since.timestamp(), "+inf")),
            str(await redis.zcard(pump_stream.MIGRATED))]]
    except Exception as exc:  # noqa: BLE001
        out["stream_migrations_recorded (since | total kept 24 h)"] = f"redis failed: {type(exc).__name__}"
    try:
        from yonixalpha_core.ml.readiness import ABLATION_KEY

        raw = await redis.get(ABLATION_KEY)
        a = json.loads(raw) if raw else None
        out["feature ablation (last run: status | computed_at | samples)"] = [[
            a.get("status"), a.get("computed_at"), str(a.get("samples"))]] if a else "not run yet (ml service, every 6 h)"
    except Exception as exc:  # noqa: BLE001
        out["feature ablation (last run: status | computed_at | samples)"] = f"redis failed: {type(exc).__name__}"
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--since", help="ISO time (UTC), e.g. 2026-09-28T21:10; overrides --hours "
                                    "(use the deploy time to see only rows recorded under the current code)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    redis = make_redis(settings)
    if a.since:
        since = datetime.fromisoformat(a.since)
        since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    async with make_session_factory(engine)() as s:
        report = await collect(s, redis, since)
    await engine.dispose()
    await redis.aclose()
    if a.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"Intelligence report since {report['since']}")
    for k, v in report.items():
        if k == "since":
            continue
        print(f"\n== {k}")
        if isinstance(v, list):
            for r in v or [["(none)"]]:
                print("   " + " | ".join("—" if x is None else str(x) for x in r))
        else:
            print(f"   {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
