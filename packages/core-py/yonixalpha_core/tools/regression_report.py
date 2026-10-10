"""Regression report: when did Solana trades start losing, in which token
stage, and was it the price move, the costs or the exits? Read-only; prints
no secrets.

    $C exec -T api python -m yonixalpha_core.tools.regression_report [--days 21] [--split ISO ...] [--json]

Closed Solana positions created in the window (indexed on created_at; at
most MAX_ROWS), PAPER and LIVE kept apart, realized PnL only (open
positions are counted, never valued). Sections:

  1. per UTC day and mode: trades, win rate, net SOL, expectancy, median
     win / loss, profit factor, size, fees as % of size, MFE / MAE, hold
  2. per window between deploy markers (--split; by default the merge
     times of the PRs that changed trading or paper accounting) x mode x
     stage (FRESH / NEAR_MIGRATION / MIGRATED / MOMENTUM, as
     solana_performance defines them)
  3. per window x mode: what the trades paid. price move = exit price /
     market entry price - 1 (a LIVE fill's market price, not its cost basis); cost drag = price move - net PnL %. For
     PAPER, the share of trades charged LIVE fixed costs (PR #53) and the
     measured LIVE price drift (PR #62): these make paper honestly worse
     without any strategy change
  4. exit reasons per window x mode
  5. strategy x model version x mode, size bands x mode
  6. LIVE orders per window: confirmed / failed by side, exit failures

A merge time is not a deploy time: the operator deployed later. Windows
say "after merge of", nothing more. A group with fewer than 20 trades is
marked "(small)". Nothing is estimated: a value that was not recorded is
"-".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana_performance import NEAR_MIGRATION_PROGRESS, NUM, STAGES

MAX_ROWS = 20000
SMALL = 20
# Merge times (UTC) of PRs that changed Solana trading, sizing or paper accounting.
DEFAULT_SPLITS = (
    ("2026-10-07T12:04:32+00:00", "#53 paper charged LIVE fixed costs; plan counts costs"),
    ("2026-10-09T10:00:48+00:00", "#62 paper charged measured LIVE drift"),
    ("2026-10-09T20:52:22+00:00", "#66 sellable-amount exit protection (paper)"),
    ("2026-10-10T08:51:57+00:00", "#69 fast promotion + event re-evaluation"),
)
SIZE_BANDS = ((0.005, "<0.005 SOL"), (0.02, "0.005-0.02"), (0.1, "0.02-0.1"), (float("inf"), ">=0.1"))

ROWS_SQL = f"""
SELECT coalesce(pp.execution_mode, 'PAPER') AS mode, coalesce(pp.engine, '?') AS engine,
       coalesce(pp.strategy, '?') AS strategy, coalesce(pp.model_version, '-') AS model_version,
       coalesce(pp.exit_reason, '?') AS exit_reason, pp.realized_pnl AS pnl, pp.realized_pnl_pct AS pnl_pct,
       pp.fees_paid_quote AS fees, pp.entry_cost_quote AS cost, pp.entry_at, pp.exit_at, pp.created_at,
       pp.entry_price, pp.exit_price, pp.highest_price, pp.lowest_price, pp.exit_failures,
       CASE WHEN pp.plan->'fill'->>'market_price' ~ '{NUM}' THEN (pp.plan->'fill'->>'market_price')::numeric
            ELSE pp.entry_price END AS market_entry,
       pp.plan->>'fixed_cost_quote' AS fixed_cost, pp.plan->'venue'->>'live_drift_pct' AS live_drift,
       CASE WHEN pp.engine = 'solana_momentum' THEN 'MOMENTUM'
            WHEN o.snapshot->>'migrated' = 'true' OR pp.plan->>'lifecycle' = 'MIGRATED'
                 OR pp.lifecycle = 'MIGRATED' OR pp.engine = 'solana_migration' THEN 'MIGRATED'
            WHEN o.snapshot->>'curve_progress' ~ '{NUM}'
                 AND (o.snapshot->>'curve_progress')::numeric >= :near THEN 'NEAR_MIGRATION'
            ELSE 'FRESH' END AS stage
FROM paper_positions pp
LEFT JOIN LATERAL (SELECT snapshot FROM opportunity_outcomes oo
                   WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
WHERE pp.created_at >= :since AND pp.engine LIKE 'solana%' AND pp.status = 'closed' AND pp.exit_at IS NOT NULL
ORDER BY pp.created_at LIMIT {MAX_ROWS}
"""

OPEN_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, count(*) AS n FROM paper_positions
WHERE created_at >= :since AND engine LIKE 'solana%' AND status <> 'closed' GROUP BY 1
"""

ORDERS_SQL = """
SELECT side, status, created_at FROM execution_orders
WHERE mode = 'LIVE' AND created_at >= :since AND side IN ('BUY', 'SELL') ORDER BY created_at LIMIT 20000
"""


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def metrics(rows: list[dict]) -> dict[str, Any]:
    """Realized results of closed trades (pnl in SOL, pnl_pct as a fraction)."""
    n = len(rows)
    if not n:
        return {"trades": 0}
    pnl = [r["pnl"] or 0.0 for r in rows]
    pct = [r["pnl_pct"] for r in rows if r["pnl_pct"] is not None]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    win_pct = [r["pnl_pct"] for r in rows if (r["pnl"] or 0) > 0 and r["pnl_pct"] is not None]
    loss_pct = [r["pnl_pct"] for r in rows if (r["pnl"] or 0) <= 0 and r["pnl_pct"] is not None]
    cost = [r["cost"] for r in rows if r["cost"]]
    fee_share = [r["fees"] / r["cost"] for r in rows if r["cost"] and r["fees"] is not None]
    # Marks are market prices: compare them with the market price of the fill
    # (a LIVE cost basis includes fees and new-account rent, 30-110% on a tiny buy).
    mfe = [r["highest_price"] / r["market_entry"] - 1 for r in rows if r["market_entry"] and r["highest_price"]]
    mae = [r["lowest_price"] / r["market_entry"] - 1 for r in rows if r["market_entry"] and r["lowest_price"]]
    hold = [(r["exit_at"] - r["entry_at"]).total_seconds() for r in rows if r["exit_at"] and r["entry_at"]]
    cum = peak = mdd = 0.0
    for r in sorted(rows, key=lambda x: x["exit_at"]):
        cum += r["pnl"] or 0.0
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    def med(v):
        return statistics.median(v) if v else None

    return {
        "trades": n, "small_sample": n < SMALL, "win_rate": len(wins) / n,
        "net_sol": sum(pnl), "expectancy_sol": sum(pnl) / n, "mean_pct": statistics.fmean(pct) if pct else None,
        "median_win_pct": med(win_pct), "median_loss_pct": med(loss_pct),
        "avg_win_pct": statistics.fmean(win_pct) if win_pct else None,
        "avg_loss_pct": statistics.fmean(loss_pct) if loss_pct else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown_sol": mdd, "median_size_sol": med(cost), "median_fees_pct_of_size": med(fee_share),
        "avg_mfe_pct": statistics.fmean(mfe) if mfe else None, "avg_mae_pct": statistics.fmean(mae) if mae else None,
        "median_hold_s": med(hold),
    }


def costs(rows: list[dict]) -> dict[str, Any]:
    moves = [(r["exit_price"] / r["market_entry"] - 1, r["pnl_pct"]) for r in rows
             if r["market_entry"] and r["exit_price"] and r["pnl_pct"] is not None]
    out: dict[str, Any] = {"trades": len(rows)}
    if moves:
        out["median_price_move_pct"] = statistics.median(m for m, _ in moves)
        out["median_net_pct"] = statistics.median(p for _, p in moves)
        out["median_cost_drag_pct"] = statistics.median(m - p for m, p in moves)
    if rows and rows[0]["mode"] == "PAPER":
        out["share_charged_live_fixed_costs"] = sum(1 for r in rows if r["fixed_cost"]) / len(rows)
        out["share_charged_live_drift"] = sum(1 for r in rows if r["live_drift"]) / len(rows)
        drift = [_f(r["live_drift"]) for r in rows if r["live_drift"]]
        out["median_entry_drift_charged_pct"] = statistics.median(drift) / 100 if drift else None
    return out


def window_of(ts: datetime, splits: list[tuple[datetime, str]]) -> str:
    label = "before " + splits[0][1].split(" ")[0] if splits else "all"
    for at, name in splits:
        if ts >= at:
            label = "after " + name.split(" ")[0]
    return label


def band(cost: float | None) -> str:
    if cost is None:
        return "size unknown"
    return next(name for limit, name in SIZE_BANDS if cost < limit)


async def build(session, days: int, splits: list[tuple[datetime, str]], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    p = {"since": since, "near": NEAR_MIGRATION_PROGRESS}
    rows = []
    for r in (await session.execute(text(ROWS_SQL), p)).mappings().all():
        d = dict(r)
        for k in ("pnl", "pnl_pct", "fees", "cost", "entry_price", "exit_price", "highest_price", "lowest_price",
                  "market_entry"):
            d[k] = _f(d[k])
        rows.append(d)
    splits = sorted(splits)
    groups: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        w = window_of(r["created_at"], splits)
        groups["day"][(r["exit_at"].date().isoformat(), r["mode"])].append(r)
        groups["window_stage"][(w, r["mode"], r["stage"])].append(r)
        groups["window"][(w, r["mode"])].append(r)
        groups["exit"][(w, r["mode"], r["exit_reason"])].append(r)
        groups["strategy"][(r["mode"], r["engine"], r["strategy"], r["model_version"])].append(r)
        groups["size"][(r["mode"], band(r["cost"]))].append(r)
    orders: dict[tuple, int] = defaultdict(int)
    for o in (await session.execute(text(ORDERS_SQL), p)).mappings().all():
        orders[(window_of(o["created_at"], splits), o["side"], o["status"])] += 1
    exit_fail: dict[tuple, int] = defaultdict(int)
    for r in rows:
        if r["mode"] == "LIVE" and r["exit_failures"]:
            exit_fail[window_of(r["created_at"], splits)] += int(r["exit_failures"])
    return {
        "since": since.isoformat(), "days": days, "closed_trades": len(rows), "truncated": len(rows) >= MAX_ROWS,
        "open_positions": {r.mode: int(r.n) for r in (await session.execute(text(OPEN_SQL), p)).all()},
        "splits": [{"at": at.isoformat(), "change": name} for at, name in splits],
        "by_day": {f"{d} {m}": metrics(v) for (d, m), v in sorted(groups["day"].items())},
        "by_window_stage": {f"{w} | {m} | {s}": metrics(v) for (w, m, s), v in sorted(
            groups["window_stage"].items(), key=lambda kv: (kv[0][1], _wkey(kv[0][0], splits), STAGES.index(kv[0][2])))},
        "costs_by_window": {f"{w} | {m}": costs(v) for (w, m), v in sorted(
            groups["window"].items(), key=lambda kv: (kv[0][1], _wkey(kv[0][0], splits)))},
        "exit_reasons": {f"{w} | {m} | {e}": {"trades": len(v), "win_rate": sum(1 for r in v if (r["pnl"] or 0) > 0) / len(v),
                                             "net_sol": sum(r["pnl"] or 0 for r in v)}
                         for (w, m, e), v in sorted(groups["exit"].items(),
                                                    key=lambda kv: (kv[0][1], _wkey(kv[0][0], splits), -len(kv[1])))},
        "by_strategy": {" | ".join(k): metrics(v) for k, v in sorted(groups["strategy"].items())},
        "by_size": {f"{m} | {b}": metrics(v) for (m, b), v in sorted(groups["size"].items())},
        "live_orders_by_window": {f"{w} | {s} | {st}": n for (w, s, st), n in sorted(orders.items())},
        "live_exit_failures_by_window": dict(exit_fail),
        "note": "realized PnL of closed positions only; PAPER is not evidence of LIVE results; windows are merge "
                "times, the deploy came later",
    }


def _wkey(label: str, splits) -> int:
    names = ["before"] + ["after " + n.split(" ")[0] for _, n in splits]
    return next((i for i, n in enumerate(names) if label.startswith(n)), 99)


def _p(v: Any, d: int = 1) -> str:
    return "-" if v is None else f"{v * 100:.{d}f}%"


def _n(v: Any, d: int = 4) -> str:
    return "-" if v is None else f"{v:.{d}f}"


def _line(k: str, m: dict) -> str:
    if not m.get("trades"):
        return f"  {k}: no trades"
    pf = m.get("profit_factor")
    return (f"  {k:44s} n={m['trades']:<4}{' (small)' if m.get('small_sample') else '        '} win {_p(m['win_rate'], 0):>5}"
            f"  net {_n(m['net_sol'], 4):>8} SOL  exp {_n(m['expectancy_sol'], 5):>8}  med win {_p(m['median_win_pct']):>7}"
            f"  med loss {_p(m['median_loss_pct']):>7}  PF {_n(pf, 2) if pf is not None else '-':>5}"
            f"  DD {_n(m['max_drawdown_sol'], 4)}  size {_n(m['median_size_sol'], 4)}  fees {_p(m['median_fees_pct_of_size'])}"
            f"  MFE {_p(m['avg_mfe_pct'])} MAE {_p(m['avg_mae_pct'])}  hold {_n(m['median_hold_s'], 0)}s")


def render(r: dict[str, Any]) -> str:
    out = [f"REGRESSION REPORT: closed Solana trades created since {r['since'][:16]} UTC ({r['days']} days): "
           f"{r['closed_trades']}{' (TRUNCATED)' if r['truncated'] else ''}; still open (not valued): {r['open_positions']}",
           "Markers (merge times, UTC; the deploy came later):"]
    out += [f"  {s['at'][:16]}  {s['change']}" for s in r["splits"]]
    out.append("\n1. Per day (exit date) and mode")
    out += [_line(k, m) for k, m in r["by_day"].items()]
    out.append("\n2. Per window, mode and stage")
    out += [_line(k, m) for k, m in r["by_window_stage"].items()]
    out.append("\n3. What the trades paid (price move as recorded vs net result)")
    for k, c in r["costs_by_window"].items():
        extra = ""
        if "share_charged_live_fixed_costs" in c:
            extra = (f"  paper charged LIVE fixed costs {_p(c['share_charged_live_fixed_costs'], 0)}, LIVE drift "
                     f"{_p(c['share_charged_live_drift'], 0)} (median drift {_p(c['median_entry_drift_charged_pct'], 2)})")
        out.append(f"  {k:44s} n={c['trades']:<4} median price move {_p(c.get('median_price_move_pct'))}  median net "
                   f"{_p(c.get('median_net_pct'))}  median cost drag {_p(c.get('median_cost_drag_pct'))}{extra}")
    out.append("\n4. Exit reasons")
    out += [f"  {k:60s} n={v['trades']:<4} win {_p(v['win_rate'], 0):>5}  net {_n(v['net_sol'])} SOL"
            for k, v in r["exit_reasons"].items()]
    out.append("\n5a. Strategy | engine | model version")
    out += [_line(k, m) for k, m in r["by_strategy"].items()]
    out.append("\n5b. Position size")
    out += [_line(k, m) for k, m in r["by_size"].items()]
    out.append("\n6. LIVE orders per window (side | status)")
    out += [f"  {k:60s} {n}" for k, n in r["live_orders_by_window"].items()] or ["  none"]
    out.append(f"  LIVE exit failures per window: {r['live_exit_failures_by_window'] or 'none'}")
    out.append(f"\n{r['note']}. Nothing was written.")
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--split", action="append", default=[], help="extra marker, ISO time (UTC)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    splits = [(datetime.fromisoformat(at), name) for at, name in DEFAULT_SPLITS]
    for s in a.split:
        t = datetime.fromisoformat(s)
        splits.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), f"{s} (operator marker)"))
    engine = make_engine(get_settings())
    try:
        async with make_session_factory(engine)() as s:
            r = await build(s, max(1, min(a.days, 90)), splits)
            await s.rollback()
    finally:
        await engine.dispose()
    print(json.dumps(r, default=str, indent=1) if a.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
