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

AGE_BANDS = ("<5m", "5-30m", "30m-2h", "2-24h", ">24h", "UNKNOWN")
REGIMES = ("QUIET", "NORMAL", "BUSY", "UNKNOWN")
# Filters shared by every position query: Solana engines, an optional engine,
# exits inside [since, until).
WHERE_CLOSED = ("pp.engine LIKE 'solana%' AND (CAST(:engine AS text) IS NULL OR pp.engine = CAST(:engine AS text)) "
                "AND pp.status = 'closed' AND pp.exit_at IS NOT NULL AND pp.exit_at >= :since AND pp.exit_at < :until")

POSITIONS_CTE = f"""
WITH p0 AS (
  SELECT pp.id, coalesce(pp.execution_mode, 'PAPER') AS mode, coalesce(pp.exit_reason, '?') AS exit_reason,
         pp.engine, coalesce(pp.realized_pnl, 0) AS pnl, pp.realized_pnl_pct AS pnl_pct,
         coalesce(pp.fees_paid_quote, 0) AS fees, pp.fees_paid_quote IS NULL AS fees_missing,
         pp.exit_at, extract(epoch FROM pp.exit_at - pp.entry_at) AS hold_s, o.id IS NULL AS no_ledger,
         to_char(pp.exit_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
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
         CASE WHEN o.snapshot->>'age_seconds' !~ '{NUM}' OR o.snapshot->>'age_seconds' IS NULL THEN 'UNKNOWN'
              WHEN (o.snapshot->>'age_seconds')::numeric < 300 THEN '<5m'
              WHEN (o.snapshot->>'age_seconds')::numeric < 1800 THEN '5-30m'
              WHEN (o.snapshot->>'age_seconds')::numeric < 7200 THEN '30m-2h'
              WHEN (o.snapshot->>'age_seconds')::numeric < 86400 THEN '2-24h'
              ELSE '>24h' END AS age,
         CASE WHEN o.regime->>'launches_last_hour_seen' ~ '{NUM}'
              THEN (o.regime->>'launches_last_hour_seen')::numeric END AS launches,
         CASE WHEN pp.exit_at - pp.entry_at < interval '10 seconds' THEN '<10s'
              WHEN pp.exit_at - pp.entry_at < interval '30 seconds' THEN '10-30s'
              WHEN pp.exit_at - pp.entry_at < interval '1 minute' THEN '30s-1m'
              WHEN pp.exit_at - pp.entry_at < interval '5 minutes' THEN '1-5m'
              WHEN pp.exit_at - pp.entry_at < interval '15 minutes' THEN '5-15m'
              WHEN pp.exit_at - pp.entry_at < interval '60 minutes' THEN '15-60m'
              ELSE '>60m' END AS hold
  FROM paper_positions pp
  LEFT JOIN LATERAL (SELECT id, snapshot, regime FROM opportunity_outcomes oo
                     WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
  WHERE {WHERE_CLOSED}
), p AS (
  -- Market activity at the decision: launches the stream saw in the hour
  -- before it, split into thirds of this window's trades (a relative label).
  SELECT p0.*, CASE WHEN launches IS NULL THEN 'UNKNOWN'
                    ELSE (ARRAY['QUIET', 'NORMAL', 'BUSY'])[ntile(3) OVER (PARTITION BY launches IS NULL ORDER BY launches)]
               END AS regime
  FROM p0
)
"""

DIMS = ("engine", "stage", "hold", "entry_quality", "exit_reason", "age", "regime", "day")
TRADES_SQL = POSITIONS_CTE + """
SELECT mode,
       CASE """ + " ".join(f"WHEN grouping({d}) = 0 THEN '{d}'" for d in DIMS) + """ ELSE 'overall' END AS dim,
       coalesce(""" + ", ".join(DIMS) + """, 'ALL') AS key,
       count(*) AS n, count(*) FILTER (WHERE pnl > 0) AS wins, count(*) FILTER (WHERE pnl < 0) AS losses,
       avg(pnl_pct) FILTER (WHERE pnl > 0) AS avg_win_pct, avg(pnl_pct) FILTER (WHERE pnl < 0) AS avg_loss_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE pnl > 0) AS median_win_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct) FILTER (WHERE pnl < 0) AS median_loss_pct,
       avg(pnl) FILTER (WHERE pnl > 0) AS avg_win, avg(pnl) FILTER (WHERE pnl < 0) AS avg_loss,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl) FILTER (WHERE pnl > 0) AS median_win,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl) FILTER (WHERE pnl < 0) AS median_loss,
       max(pnl) AS largest, min(pnl) AS smallest,
       sum(pnl) FILTER (WHERE pnl > 0) AS gross_win, sum(pnl) FILTER (WHERE pnl < 0) AS gross_loss,
       sum(pnl) AS net_pnl, sum(fees) AS fees, avg(mfe) AS mfe, avg(mae) AS mae,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_s) AS median_hold_s, sum(hold_s) AS hold_total_s
FROM p
GROUP BY GROUPING SETS ((mode), """ + ", ".join(f"(mode, {d})" for d in DIMS) + """)
"""

DRAWDOWN_SQL = POSITIONS_CTE + """
SELECT mode, max(peak - cum) AS max_drawdown, count(*) AS n
FROM (SELECT mode, cum, greatest(0, max(cum) OVER (PARTITION BY mode ORDER BY exit_at, id
                                                   ROWS UNBOUNDED PRECEDING)) AS peak
      FROM (SELECT mode, id, exit_at, sum(pnl) OVER (PARTITION BY mode ORDER BY exit_at, id
                                                     ROWS UNBOUNDED PRECEDING) AS cum FROM p) a) b
GROUP BY mode
"""

COVERAGE_SQL = POSITIONS_CTE + """
SELECT mode, count(*) AS closed, count(*) FILTER (WHERE no_ledger) AS no_ledger_record,
       count(*) FILTER (WHERE fees_missing) AS fees_not_recorded, count(*) FILTER (WHERE age = 'UNKNOWN') AS age_unknown,
       count(*) FILTER (WHERE regime = 'UNKNOWN') AS activity_unknown,
       count(*) FILTER (WHERE entry_quality = 'UNKNOWN') AS entry_quality_unknown,
       min(launches) FILTER (WHERE regime = 'QUIET') AS quiet_min, max(launches) FILTER (WHERE regime = 'QUIET') AS quiet_max,
       min(launches) FILTER (WHERE regime = 'BUSY') AS busy_min, max(launches) FILTER (WHERE regime = 'BUSY') AS busy_max
FROM p GROUP BY mode
"""

OPEN_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, engine, status, count(*) AS n, sum(entry_cost_quote) AS cost,
       min(entry_at) AS oldest_entry
FROM paper_positions
WHERE engine LIKE 'solana%' AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text)) AND status <> 'closed'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

CAPITAL_SQL = "SELECT starting_balance FROM paper_accounts WHERE name = 'solana'"

DECISIONS_SQL = """
SELECT coalesce(execution_target, '?') AS target, count(*) AS evaluated, count(*) FILTER (WHERE executable) AS qualified
FROM risk_assessments WHERE evaluated_at >= :since AND evaluated_at < :until AND engine LIKE 'solana%'
  AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text))
GROUP BY 1 ORDER BY 1
"""

ENTRIES_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, count(*) AS entries,
       count(*) FILTER (WHERE status = 'closed') AS exited, count(*) FILTER (WHERE status = 'open') AS still_open
FROM paper_positions WHERE engine LIKE 'solana%' AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text))
  AND entry_at >= :since AND entry_at < :until
GROUP BY 1 ORDER BY 1
"""

# The rule label as ML Review shows it (opportunities.review): the first
# reason up to its first ":" or " (", at most 60 characters.
MISSED_SQL = """
SELECT stage, decision,
       left(coalesce(nullif(split_part(split_part(reasons->>0, ':', 1), ' (', 1), ''), '?'), 60) AS rule,
       count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_pct) AS median_peak_pct
FROM opportunity_outcomes
WHERE decided_at >= :since AND decided_at < :until AND engine LIKE 'solana%' AND traded IS false
  AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text))
  AND analysis->'counterfactual'->>'classification' = 'MISSED_WIN'
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 15
"""

FALSE_POSITIVE_SQL = f"""
SELECT coalesce(execution_mode, 'PAPER') AS mode, coalesce(loss_analysis->>'classification', 'UNCLASSIFIED') AS cls,
       count(*) AS n
FROM opportunity_outcomes
WHERE decided_at >= :since AND decided_at < :until AND engine LIKE 'solana%' AND traded IS true
  AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text))
  AND trade_result->>'pnl_sol' ~ '{NUM}' AND (trade_result->>'pnl_sol')::numeric <= 0
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""

EXIT_TIMING_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, post_exit->>'classification' AS cls, count(*) AS n
FROM opportunity_outcomes
WHERE decided_at >= :since AND decided_at < :until AND engine LIKE 'solana%' AND traded IS true
  AND (CAST(:engine AS text) IS NULL OR engine = CAST(:engine AS text)) AND post_exit->>'classification' IS NOT NULL
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
FROM execution_orders WHERE mode = 'LIVE' AND created_at >= :since AND created_at < :until AND side IN ('BUY', 'SELL')
GROUP BY 1, 2, 3 ORDER BY 1, 2, 4 DESC
"""


def _f(v: Any, digits: int = 4) -> float | None:
    return None if v is None else round(float(v), digits)


def _pct(v: Any) -> float | None:
    return None if v is None else round(float(v) * 100, 2)


def profit_factor(gross_win: Any, gross_loss: Any) -> tuple[Any, str | None]:
    """(value, why it is undefined): gross winning PnL / gross losing PnL."""
    gw, gl = float(gross_win or 0), abs(float(gross_loss or 0))
    if gl == 0:
        return ("no losses", "undefined: no losing trade, so there is nothing to divide by") if gw else \
            (None, "undefined: no winning and no losing trade")
    return round(gw / gl, 3), None


def _metrics(r: Any, window_s: float | None = None, days: float | None = None) -> dict[str, Any]:
    n, wins, losses = int(r.n), int(r.wins), int(r.losses)
    pf, pf_note = profit_factor(r.gross_win, r.gross_loss)
    net, fees = float(r.net_pnl or 0), float(r.fees or 0)
    out = {"trades": n, "wins": wins, "losses": losses, "breakeven": n - wins - losses,
           "win_rate_pct": round(100 * wins / n, 1) if n else None,
           "loss_rate_pct": round(100 * losses / n, 1) if n else None,
           "avg_win_pct": _pct(r.avg_win_pct), "avg_loss_pct": _pct(r.avg_loss_pct),
           "median_win_pct": _pct(r.median_win_pct), "median_loss_pct": _pct(r.median_loss_pct),
           "avg_win_sol": _f(r.avg_win, 6), "avg_loss_sol": _f(r.avg_loss, 6),
           "median_win_sol": _f(r.median_win, 6), "median_loss_sol": _f(r.median_loss, 6),
           "largest_win_sol": _f(r.largest, 6) if r.largest is not None and r.largest > 0 else None,
           "largest_loss_sol": _f(r.smallest, 6) if r.smallest is not None and r.smallest < 0 else None,
           "profit_factor": pf, "profit_factor_note": pf_note,
           "net_pnl_sol": _f(net, 6), "fees_sol": _f(fees, 6), "gross_pnl_sol": _f(net + fees, 6),
           "expectancy_sol": _f(net / n, 6) if n else None,
           "avg_mfe_pct": _pct(r.mfe), "avg_mae_pct": _pct(r.mae), "median_hold_s": _f(r.median_hold_s, 1),
           "anecdotal": n < MIN_SAMPLE}
    if window_s:
        # average number of positions open at once over the window (time in a trade / window length)
        out["avg_open_positions"] = _f(float(r.hold_total_s or 0) / window_s, 3)
    if days:
        out["trades_per_day"] = _f(n / days, 2)
    return out


async def report(session: AsyncSession, days: int = 7, now: datetime | None = None,
                 outcomes: bool = True, *, engine: str | None = None, since: datetime | None = None,
                 until: datetime | None = None) -> dict[str, Any]:
    """Closed trades with an exit in [since, until): by default the last
    `days` days up to now. `engine` narrows every section to one strategy."""
    now = now or datetime.now(timezone.utc)
    until = until or now
    if since is None:
        days = max(1, min(int(days), MAX_DAYS))
        since = until - timedelta(days=days)
    window_s = max((until - since).total_seconds(), 1.0)
    span_days = window_s / 86400
    p = {"since": since, "until": until, "near": NEAR_MIGRATION_PROGRESS, "engine": engine}

    modes: dict[str, dict[str, Any]] = {}
    for r in (await session.execute(text(TRADES_SQL), p)).all():
        m = modes.setdefault(r.mode, {"overall": None, **{f"by_{d}": {} for d in DIMS}})
        if r.dim == "overall":
            m["overall"] = _metrics(r, window_s, span_days)
        else:
            m[f"by_{r.dim}"][r.key] = _metrics(r)
    for r in (await session.execute(text(DRAWDOWN_SQL), p)).all():
        if r.mode in modes and modes[r.mode]["overall"] is not None:
            modes[r.mode]["overall"]["max_drawdown_sol"] = _f(r.max_drawdown, 6)
    capital = (await session.execute(text(CAPITAL_SQL))).scalar_one_or_none()
    for mode, m in modes.items():  # buckets and stages in their natural order
        m["by_hold"] = {k: m["by_hold"][k] for k in HOLD_BUCKETS if k in m["by_hold"]}
        m["by_stage"] = {k: m["by_stage"][k] for k in STAGES if k in m["by_stage"]}
        m["by_age"] = {k: m["by_age"][k] for k in AGE_BANDS if k in m["by_age"]}
        m["by_regime"] = {k: m["by_regime"][k] for k in REGIMES if k in m["by_regime"]}
        m["by_day"] = dict(sorted(m["by_day"].items()))
        o = m["overall"]
        if o is not None:
            if mode == "PAPER" and capital:
                o["starting_capital_sol"] = _f(capital, 6)
                o["return_on_starting_capital_pct"] = round(100 * (o["net_pnl_sol"] or 0) / float(capital), 3)
            else:
                o["return_on_starting_capital_pct"] = None
                o["return_note"] = ("LIVE: deposits and withdrawals of the wallet are not in the ledger, so there is no "
                                    "starting capital to divide by" if mode == "LIVE" else "no starting capital recorded")
    coverage = {r.mode: {"closed": int(r.closed), "no_ledger_record": int(r.no_ledger_record),
                         "fees_not_recorded": int(r.fees_not_recorded), "age_unknown": int(r.age_unknown),
                         "activity_unknown": int(r.activity_unknown), "entry_quality_unknown": int(r.entry_quality_unknown),
                         "quiet_launches_per_hour": [_f(r.quiet_min, 0), _f(r.quiet_max, 0)],
                         "busy_launches_per_hour": [_f(r.busy_min, 0), _f(r.busy_max, 0)]}
                for r in (await session.execute(text(COVERAGE_SQL), p)).all()}
    open_positions = [{"mode": r.mode, "engine": r.engine, "status": r.status, "count": int(r.n),
                       "cost_sol": _f(r.cost, 6), "oldest_entry": r.oldest_entry.isoformat() if r.oldest_entry else None}
                      for r in (await session.execute(text(OPEN_SQL), p)).all()]

    decisions = {r.target: {"signals": int(r.evaluated), "qualified": int(r.qualified),
                            "rejected": int(r.evaluated) - int(r.qualified)}
                 for r in (await session.execute(text(DECISIONS_SQL), p)).all()}
    entries = {r.mode: {"entries": int(r.entries), "exited": int(r.exited), "still_open": int(r.still_open)}
               for r in (await session.execute(text(ENTRIES_SQL), p)).all()}
    out: dict[str, Any] = {
        "since": since.isoformat(), "until": until.isoformat(), "days": round(span_days, 3), "engine": engine,
        "chain": "solana", "decisions_by_target": decisions, "entries_by_mode": entries, "trades_by_mode": modes,
        "open_positions": open_positions, "coverage": coverage,
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
        "slippage": "LIVE: all-in fill price vs the transaction's expected price (execution_live). PAPER: the "
                    "simulated price impact is inside the fill price and so inside gross PnL; it is never added again",
        "gross_net": "net = booked realized PnL (proceeds - everything paid); fees = program / AMM, network, priority "
                     "and close costs recorded on the position; gross = net + fees. Each cost is counted once",
        "profit_factor": "gross winning PnL / gross losing PnL; undefined when there is no losing trade",
        "wins_losses": "win: booked PnL > 0; loss: < 0; breakeven: exactly 0",
        "expectancy": "net PnL per closed trade, after every recorded cost",
        "avg_open_positions": "time spent in positions / window length: the average number open at once",
        "age": "token age at the decision (ledger snapshot)",
        "regime": "launches the stream saw in the hour before the decision, split into thirds of this window's "
                  "trades (QUIET / NORMAL / BUSY: relative to this window, see coverage for the ranges)",
        "open_positions": "never valued here and never in any PnL: only closed positions are booked",
        "max_drawdown": "largest fall of the cumulative realized PnL (SOL) from its running peak",
        "missed_winner": "rejected, then the counterfactual was classified MISSED_WIN",
        "false_positive": "entered and closed at a loss",
        "anecdotal": f"fewer than {MIN_SAMPLE} trades in the group",
    }
    out["note"] = ("Measured from stored rows only. PAPER is not evidence of LIVE results; LIVE rows exist only for "
                   "transactions that were actually sent. Open positions are listed apart and never booked.")
    return out


POSITION_LIST_SQL = f"""
SELECT pp.id, coalesce(pp.execution_mode, 'PAPER') AS mode, pp.engine, pp.strategy, pp.model_version, pp.symbol,
       pp.asset_id AS mint, pp.status, pp.exit_reason, pp.entry_at, pp.exit_at, pp.entry_price, pp.exit_price,
       pp.last_price, pp.last_marked_at, pp.entry_cost_quote AS cost, pp.proceeds_quote AS proceeds,
       pp.fees_paid_quote AS fees, pp.realized_pnl AS pnl, pp.realized_pnl_pct AS pnl_pct, pp.remaining_quantity,
       pp.highest_price, pp.lowest_price,
       CASE WHEN pp.plan->>'entry_price' ~ '{NUM}' THEN (pp.plan->>'entry_price')::numeric END AS decision_price,
       CASE WHEN pp.plan->'fill'->>'market_price' ~ '{NUM}' THEN (pp.plan->'fill'->>'market_price')::numeric END
         AS live_market_price,
       CASE WHEN pp.plan->'position_size'->>'value' ~ '{NUM}' THEN (pp.plan->'position_size'->>'value')::numeric END AS size,
       pp.plan->'venue'->>'live_drift_pct' AS paper_drift_pct, pp.plan->>'token_category' AS category,
       ra.evaluated_at AS signal_at, o.decided_at AS ledger_decided_at
FROM paper_positions pp
LEFT JOIN risk_assessments ra ON ra.id = pp.assessment_id
LEFT JOIN LATERAL (SELECT decided_at FROM opportunity_outcomes oo
                   WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
WHERE pp.engine LIKE 'solana%' AND (CAST(:engine AS text) IS NULL OR pp.engine = CAST(:engine AS text))
  AND (CAST(:mode AS text) IS NULL OR coalesce(pp.execution_mode, 'PAPER') = CAST(:mode AS text))
  AND pp.entry_at >= :since
ORDER BY pp.entry_at DESC LIMIT :limit
"""


def position_row(r: Any) -> dict[str, Any]:
    """One position as the diagnostics show it. Booked PnL only for closed
    positions; an open position's marked value is labelled as such."""
    closed = r.status == "closed"
    entry = r.live_market_price if r.mode == "LIVE" and r.live_market_price else r.entry_price
    slip = None
    if r.decision_price and entry:
        # fill (or LIVE market fill) vs the price the decision saw: price impact, latency drift and, for a
        # PAPER fill, the AMM fee; inside the booked result already, shown here, never subtracted again
        slip = round((float(entry) / float(r.decision_price) - 1) * 100, 3)
    net = float(r.pnl) if closed and r.pnl is not None else None
    fees = float(r.fees) if r.fees is not None else None
    marked = None
    if not closed and r.last_price is not None and r.remaining_quantity is not None and r.cost is not None:
        marked = float(r.remaining_quantity) * float(r.last_price) + float(r.proceeds or 0) - float(r.cost)
    end = r.exit_at if closed else None
    return {
        "id": str(r.id), "mode": r.mode, "engine": r.engine, "strategy": r.strategy, "model_version": r.model_version,
        "symbol": r.symbol, "mint": r.mint, "status": r.status, "category": r.category,
        "signal_at": (r.signal_at or r.ledger_decided_at).isoformat() if (r.signal_at or r.ledger_decided_at) else None,
        "entry_at": r.entry_at.isoformat(), "entry_price": _f(r.entry_price, 12), "decision_price": _f(r.decision_price, 12),
        "exit_at": end.isoformat() if end else None, "exit_price": _f(r.exit_price, 12) if closed else None,
        "size_sol": _f(r.size, 6), "cost_sol": _f(r.cost, 6), "proceeds_sol": _f(r.proceeds, 6),
        "fees_sol": _f(fees, 6), "net_pnl_sol": _f(net, 6),
        "gross_pnl_sol": _f(net + fees, 6) if net is not None and fees is not None else None,
        "net_pnl_pct": _pct(r.pnl_pct) if closed else None,
        "entry_slippage_estimate_pct": slip, "paper_drift_charged_pct": _f(r.paper_drift_pct, 3),
        "hold_s": round((end - r.entry_at).total_seconds(), 1) if end else None,
        "exit_reason": r.exit_reason if closed else None,
        "unrealized_marked_sol": _f(marked, 6),
        "marked_at": r.last_marked_at.isoformat() if not closed and r.last_marked_at else None,
    }


async def positions(session: AsyncSession, *, engine: str | None = None, mode: str | None = None, limit: int = 20,
                    since: datetime | None = None) -> list[dict[str, Any]]:
    since = since or datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = (await session.execute(text(POSITION_LIST_SQL), {"engine": engine, "mode": mode, "since": since,
                                                             "limit": max(1, min(int(limit), 500))})).all()
    return [position_row(r) for r in rows]


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
