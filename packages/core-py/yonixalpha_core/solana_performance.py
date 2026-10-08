"""Solana performance, PAPER vs LIVE (2026-10-08, Solana-first production).

Read-only, aggregated in the database over a bounded window (`days`):

  decisions   gate decisions by execution target: evaluated (signals),
              executable (qualified), refused (rejected)
  trades      closed Solana positions by mode, overall and split by
              stage (FRESH / NEAR_MIGRATION / MIGRATED / MOMENTUM),
              hold time, entry quality and exit reason: win rate, average
              and median win / loss, profit factor, net PnL, fees, MFE /
              MAE, median hold; max drawdown of the realized PnL curve
  outcomes    missed winners (rejected, then the counterfactual classified
              MISSED_WIN) by the rule that refused them; false positives
              (entered, lost) by loss classification; exit timing classes
  execution   LIVE orders by side / route / status with the measured
              latency and price difference (execution_analysis)

Definitions (each number is measured from stored rows, never estimated):
  stage           MOMENTUM: engine solana_momentum; MIGRATED: migrated at the
                  decision (snapshot), entered as MIGRATED, or the migration
                  engine; NEAR_MIGRATION: curve progress at the decision >=
                  NEAR_MIGRATION_PROGRESS; FRESH otherwise
  entry quality   CLEAN: no deterioration indicator at the decision;
                  DETERIORATING: one or more; UNKNOWN: not recorded
  MFE / MAE       highest / lowest marked price over the entry price - 1
  slippage        LIVE only (all-in price vs the build's expected price);
                  paper fills are simulated from the curve / pool quote and
                  carry no per-trade slippage record
A group with fewer than 20 trades is anecdotal; the report says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NEAR_MIGRATION_PROGRESS = Decimal("0.70")  # safety settings momentum_near_migration_progress default
MIN_SAMPLE = 20
MAX_DAYS = 90
HOLD_BUCKETS = ("<10s", "10-30s", "30s-1m", "1-5m", "5-15m", "15-60m", ">60m")
STAGES = ("FRESH", "NEAR_MIGRATION", "MIGRATED", "MOMENTUM")
NUM = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"

POSITIONS_CTE = f"""
WITH p AS (
  SELECT pp.id, coalesce(pp.execution_mode, 'PAPER') AS mode, coalesce(pp.exit_reason, '?') AS exit_reason,
         coalesce(pp.realized_pnl, 0) AS pnl, pp.realized_pnl_pct AS pnl_pct, coalesce(pp.fees_paid_quote, 0) AS fees,
         pp.exit_at, extract(epoch FROM pp.exit_at - pp.entry_at) AS hold_s,
         CASE WHEN pp.entry_price > 0 AND pp.highest_price IS NOT NULL THEN pp.highest_price / pp.entry_price - 1 END AS mfe,
         CASE WHEN pp.entry_price > 0 AND pp.lowest_price IS NOT NULL THEN pp.lowest_price / pp.entry_price - 1 END AS mae,
         CASE WHEN pp.engine = 'solana_momentum' THEN 'MOMENTUM'
              WHEN o.snapshot->>'migrated' = 'true' OR pp.plan->>'lifecycle' = 'MIGRATED'
                   OR pp.engine = 'solana_migration' THEN 'MIGRATED'
              WHEN o.snapshot->>'curve_progress' ~ '{NUM}'
                   AND (o.snapshot->>'curve_progress')::numeric >= :near THEN 'NEAR_MIGRATION'
              ELSE 'FRESH' END AS stage,
         CASE WHEN jsonb_typeof(o.snapshot->'deterioration_indicators') IS DISTINCT FROM 'array' THEN 'UNKNOWN'
              WHEN jsonb_array_length(o.snapshot->'deterioration_indicators') = 0 THEN 'CLEAN'
              ELSE 'DETERIORATING' END AS entry_quality,
         CASE WHEN pp.exit_at - pp.entry_at < interval '10 seconds' THEN '<10s'
              WHEN pp.exit_at - pp.entry_at < interval '30 seconds' THEN '10-30s'
              WHEN pp.exit_at - pp.entry_at < interval '1 minute' THEN '30s-1m'
              WHEN pp.exit_at - pp.entry_at < interval '5 minutes' THEN '1-5m'
              WHEN pp.exit_at - pp.entry_at < interval '15 minutes' THEN '5-15m'
              WHEN pp.exit_at - pp.entry_at < interval '60 minutes' THEN '15-60m'
              ELSE '>60m' END AS hold
  FROM paper_positions pp
  LEFT JOIN LATERAL (SELECT snapshot FROM opportunity_outcomes oo
                     WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
  WHERE pp.engine LIKE 'solana%' AND pp.status = 'closed' AND pp.exit_at >= :since AND pp.exit_at IS NOT NULL
)
"""

TRADES_SQL = POSITIONS_CTE + """
SELECT mode,
       CASE WHEN grouping(stage) = 0 THEN 'stage' WHEN grouping(hold) = 0 THEN 'hold'
            WHEN grouping(entry_quality) = 0 THEN 'entry_quality' WHEN grouping(exit_reason) = 0 THEN 'exit_reason'
            ELSE 'overall' END AS dim,
       coalesce(stage, hold, entry_quality, exit_reason, 'ALL') AS key,
       count(*) AS n, count(*) FILTER (WHERE pnl > 0) AS wins,
       avg(pnl_pct) FILTER (WHERE pnl > 0) AS avg_win_pct, avg(pnl_pct) FILTER (WHERE pnl <= 0) AS avg_loss_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE pnl > 0) AS median_win_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE pnl <= 0) AS median_loss_pct,
       sum(pnl) FILTER (WHERE pnl > 0) AS gross_win, sum(pnl) FILTER (WHERE pnl <= 0) AS gross_loss,
       sum(pnl) AS net_pnl, sum(fees) AS fees, avg(mfe) AS mfe, avg(mae) AS mae,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_s) AS median_hold_s
FROM p
GROUP BY GROUPING SETS ((mode), (mode, stage), (mode, hold), (mode, entry_quality), (mode, exit_reason))
"""

DRAWDOWN_SQL = POSITIONS_CTE + """
SELECT mode, max(peak - cum) AS max_drawdown, count(*) AS n
FROM (SELECT mode, cum, greatest(0, max(cum) OVER (PARTITION BY mode ORDER BY exit_at, id
                                                   ROWS UNBOUNDED PRECEDING)) AS peak
      FROM (SELECT mode, id, exit_at, sum(pnl) OVER (PARTITION BY mode ORDER BY exit_at, id
                                                     ROWS UNBOUNDED PRECEDING) AS cum FROM p) a) b
GROUP BY mode
"""

DECISIONS_SQL = """
SELECT coalesce(execution_target, '?') AS target, count(*) AS evaluated, count(*) FILTER (WHERE executable) AS qualified
FROM risk_assessments WHERE evaluated_at >= :since AND engine LIKE 'solana%'
GROUP BY 1 ORDER BY 1
"""

ENTRIES_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, count(*) AS entries,
       count(*) FILTER (WHERE status = 'closed') AS exited, count(*) FILTER (WHERE status = 'open') AS still_open
FROM paper_positions WHERE engine LIKE 'solana%' AND entry_at >= :since
GROUP BY 1 ORDER BY 1
"""

# The rule label as ML Review shows it (opportunities.review): the first
# reason up to its first ":" or " (", at most 60 characters.
MISSED_SQL = """
SELECT stage, decision,
       left(coalesce(nullif(split_part(split_part(reasons->>0, ':', 1), ' (', 1), ''), '?'), 60) AS rule,
       count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_pct) AS median_peak_pct
FROM opportunity_outcomes
WHERE decided_at >= :since AND engine LIKE 'solana%' AND traded IS false
  AND analysis->'counterfactual'->>'classification' = 'MISSED_WIN'
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 15
"""

FALSE_POSITIVE_SQL = f"""
SELECT coalesce(execution_mode, 'PAPER') AS mode, coalesce(loss_analysis->>'classification', 'UNCLASSIFIED') AS cls,
       count(*) AS n
FROM opportunity_outcomes
WHERE decided_at >= :since AND engine LIKE 'solana%' AND traded IS true
  AND trade_result->>'pnl_sol' ~ '{NUM}' AND (trade_result->>'pnl_sol')::numeric <= 0
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""

EXIT_TIMING_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, post_exit->>'classification' AS cls, count(*) AS n
FROM opportunity_outcomes
WHERE decided_at >= :since AND engine LIKE 'solana%' AND traded IS true AND post_exit->>'classification' IS NOT NULL
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""

EXECUTION_SQL = f"""
SELECT side, coalesce(route, '?') AS route, status, count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (diagnostics->'timing'->>'decision_to_submit_ms')::float)
         FILTER (WHERE diagnostics->'timing'->>'decision_to_submit_ms' ~ '{NUM}') AS decision_to_submit_ms,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (diagnostics->'timing'->>'decision_to_confirm_ms')::float)
         FILTER (WHERE diagnostics->'timing'->>'decision_to_confirm_ms' ~ '{NUM}') AS decision_to_confirm_ms,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (diagnostics->'price'->'components_pct'->>'vs_expected_pct')::float)
         FILTER (WHERE diagnostics->'price'->'components_pct'->>'vs_expected_pct' ~ '{NUM}') AS slippage_vs_expected_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (diagnostics->'price'->'components_pct'->>'total_vs_decision_pct')::float)
         FILTER (WHERE diagnostics->'price'->'components_pct'->>'total_vs_decision_pct' ~ '{NUM}') AS total_vs_decision_pct
FROM execution_orders WHERE mode = 'LIVE' AND created_at >= :since AND side IN ('BUY', 'SELL')
GROUP BY 1, 2, 3 ORDER BY 1, 2, 4 DESC
"""


def _f(v: Any, digits: int = 4) -> float | None:
    return None if v is None else round(float(v), digits)


def _pct(v: Any) -> float | None:
    return None if v is None else round(float(v) * 100, 2)


def _metrics(r: Any) -> dict[str, Any]:
    n, wins = int(r.n), int(r.wins)
    gross_win, gross_loss = r.gross_win or 0, r.gross_loss or 0
    pf = None if not gross_loss else round(float(gross_win) / abs(float(gross_loss)), 3)
    return {"trades": n, "wins": wins, "losses": n - wins, "win_rate_pct": round(100 * wins / n, 1) if n else None,
            "avg_win_pct": _pct(r.avg_win_pct), "avg_loss_pct": _pct(r.avg_loss_pct),
            "median_win_pct": _pct(r.median_win_pct), "median_loss_pct": _pct(r.median_loss_pct),
            "profit_factor": pf if gross_loss else (None if not gross_win else "no losses"),
            "net_pnl_sol": _f(r.net_pnl, 6), "fees_sol": _f(r.fees, 6),
            "avg_mfe_pct": _pct(r.mfe), "avg_mae_pct": _pct(r.mae), "median_hold_s": _f(r.median_hold_s, 1),
            "anecdotal": n < MIN_SAMPLE}


async def report(session: AsyncSession, days: int = 7, now: datetime | None = None,
                 outcomes: bool = True) -> dict[str, Any]:
    days = max(1, min(int(days), MAX_DAYS))
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    p = {"since": since, "near": NEAR_MIGRATION_PROGRESS}

    modes: dict[str, dict[str, Any]] = {}
    for r in (await session.execute(text(TRADES_SQL), p)).all():
        m = modes.setdefault(r.mode, {"overall": None, "by_stage": {}, "by_hold": {}, "by_entry_quality": {},
                                      "by_exit_reason": {}})
        if r.dim == "overall":
            m["overall"] = _metrics(r)
        else:
            m[f"by_{r.dim}"][r.key] = _metrics(r)
    for r in (await session.execute(text(DRAWDOWN_SQL), p)).all():
        if r.mode in modes and modes[r.mode]["overall"] is not None:
            modes[r.mode]["overall"]["max_drawdown_sol"] = _f(r.max_drawdown, 6)
    for m in modes.values():  # buckets and stages in their natural order
        m["by_hold"] = {k: m["by_hold"][k] for k in HOLD_BUCKETS if k in m["by_hold"]}
        m["by_stage"] = {k: m["by_stage"][k] for k in STAGES if k in m["by_stage"]}

    decisions = {r.target: {"signals": int(r.evaluated), "qualified": int(r.qualified),
                            "rejected": int(r.evaluated) - int(r.qualified)}
                 for r in (await session.execute(text(DECISIONS_SQL), p)).all()}
    entries = {r.mode: {"entries": int(r.entries), "exited": int(r.exited), "still_open": int(r.still_open)}
               for r in (await session.execute(text(ENTRIES_SQL), p)).all()}
    out: dict[str, Any] = {
        "since": since.isoformat(), "days": days, "chain": "solana", "decisions_by_target": decisions,
        "entries_by_mode": entries, "trades_by_mode": modes,
        "execution_live": [{"side": r.side, "route": r.route, "status": r.status, "orders": int(r.n),
                            "median_decision_to_submit_ms": _f(r.decision_to_submit_ms, 0),
                            "median_decision_to_confirm_ms": _f(r.decision_to_confirm_ms, 0),
                            "median_slippage_vs_expected_pct": _f(r.slippage_vs_expected_pct, 2),
                            "median_total_vs_decision_pct": _f(r.total_vs_decision_pct, 2)}
                           for r in (await session.execute(text(EXECUTION_SQL), p)).all()],
    }
    if outcomes:
        out["missed_winners_by_rule"] = [{"stage": r.stage, "decision": r.decision, "rule": r.rule, "count": int(r.n),
                                          "median_peak_pct": _f(r.median_peak_pct, 1)}
                                         for r in (await session.execute(text(MISSED_SQL), p)).all()]
        fp: dict[str, dict[str, int]] = {}
        for r in (await session.execute(text(FALSE_POSITIVE_SQL), p)).all():
            fp.setdefault(r.mode, {})[r.cls] = int(r.n)
        out["false_positives_by_mode"] = fp
        et: dict[str, dict[str, int]] = {}
        for r in (await session.execute(text(EXIT_TIMING_SQL), p)).all():
            et.setdefault(r.mode, {})[r.cls] = int(r.n)
        out["exit_timing_by_mode"] = et
    out["definitions"] = {
        "stage": f"MOMENTUM = momentum engine; MIGRATED = migrated at the decision or entered as MIGRATED; "
                 f"NEAR_MIGRATION = curve progress >= {NEAR_MIGRATION_PROGRESS} at the decision; FRESH otherwise",
        "entry_quality": "CLEAN = no deterioration indicator at the decision; DETERIORATING = one or more; "
                         "UNKNOWN = not recorded",
        "mfe_mae": "highest / lowest marked price over the entry price, minus 1",
        "slippage": "LIVE only: all-in fill price vs the transaction's expected price. Paper fills are simulated "
                    "from the curve / pool quote and have no per-trade slippage record",
        "profit_factor": "gross winning PnL / gross losing PnL",
        "max_drawdown": "largest fall of the cumulative realized PnL (SOL) from its running peak",
        "missed_winner": "rejected, then the counterfactual was classified MISSED_WIN",
        "false_positive": "entered and closed at a loss",
        "anecdotal": f"fewer than {MIN_SAMPLE} trades in the group",
    }
    out["note"] = ("Measured from stored rows only. PAPER is not evidence of LIVE results; LIVE rows exist only for "
                   "transactions that were actually sent.")
    return out


if __name__ == "__main__":  # $C exec api python -m yonixalpha_core.solana_performance --days 7
    import argparse
    import asyncio
    import json

    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    async def _main() -> None:
        engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
        try:
            async with make_session_factory(engine)() as s:
                print(json.dumps(await report(s, args.days), indent=1, default=str))
        finally:
            await engine.dispose()

    asyncio.run(_main())
