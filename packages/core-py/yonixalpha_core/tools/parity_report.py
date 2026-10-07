"""Paper vs live and copy-trading report (read-only).

    $C exec api python -m yonixalpha_core.tools.parity_report [--days 7]

Prints, aggregated in the database:
  1. closed Solana positions by execution mode (PAPER / LIVE): count, win
     rate, mean and median result, fees, hold time and exit reasons
  2. LIVE order outcomes by side (confirmed / failed) and the paper failure
     rates in use
  3. entry decisions by target: how many were executable, and the hard-block
     codes that refused the rest (FIXED_COSTS_EXCEED_RISK and the size checks
     show where paper and live sizing differ)
  4. copy trading: decisions and reasons per chain and side, and the median
     latency of each stage, so every copy opportunity has a disposition

Written for the 2026-10-07 audit. Nothing is written. Numbers are measured
from the database only; a section with no rows says so.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory

POSITIONS_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, count(*) AS n,
       count(*) FILTER (WHERE realized_pnl > 0) AS wins,
       avg(realized_pnl_pct) * 100 AS mean_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY realized_pnl_pct) * 100 AS median_pct,
       sum(realized_pnl) AS total_pnl, avg(fees_paid_quote) AS mean_fees,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM exit_at - entry_at)) AS median_hold_s
FROM paper_positions
WHERE engine LIKE 'solana%' AND status = 'closed' AND exit_at >= :since
GROUP BY 1 ORDER BY 1
"""

EXITS_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, coalesce(exit_reason, '?') AS reason, count(*),
       avg(realized_pnl_pct) * 100
FROM paper_positions
WHERE engine LIKE 'solana%' AND status = 'closed' AND exit_at >= :since
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""

ORDERS_SQL = """
SELECT side, status, count(*) FROM execution_orders
WHERE mode = 'LIVE' AND created_at >= :since AND side IN ('BUY', 'SELL')
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""

DECISIONS_SQL = """
SELECT execution_target, executable, count(*) FROM risk_assessments
WHERE evaluated_at >= :since AND engine LIKE 'solana%'
GROUP BY 1, 2 ORDER BY 1, 2
"""

BLOCKS_SQL = """
SELECT r.execution_target, f->>'code' AS code, count(*)
FROM (SELECT execution_target, assessment FROM risk_assessments
      WHERE evaluated_at >= :since AND engine LIKE 'solana%' AND NOT executable
      ORDER BY evaluated_at DESC LIMIT 50000) r,
     jsonb_array_elements(r.assessment -> 'findings') f
WHERE (f->>'hard_block')::boolean
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25
"""

COPY_SQL = """
SELECT chain, side, decision, coalesce(split_part(reason, ':', 1), '-') AS reason, count(*)
FROM copy_events WHERE detected_at >= :since
GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 5 DESC
"""

COPY_LATENCY_SQL = """
SELECT chain,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (latency_ms->>'detection')::float) AS detection,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (latency_ms->>'decision')::float) AS decision,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (latency_ms->>'total')::float) AS total,
       count(*)
FROM copy_events WHERE detected_at >= :since AND latency_ms IS NOT NULL
GROUP BY 1 ORDER BY 1
"""


def num(v, digits: int = 2) -> str:
    return "-" if v is None else f"{float(v):,.{digits}f}"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    settings = get_settings()
    engine = make_engine(settings)
    p = {"since": since}
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            print(f"parity_report: last {a.days} days (since {since:%Y-%m-%d %H:%M} UTC)\n")
            print("1. Closed Solana positions: PAPER vs LIVE")
            rows = (await s.execute(text(POSITIONS_SQL), p)).all()
            if not rows:
                print("  none closed in the window")
            for mode, n, wins, mean, med, total, fees, hold in rows:
                print(f"  {mode:5s} n={n:<5} win rate {num(100 * wins / n if n else None, 1)}%  mean {num(mean)}%  "
                      f"median {num(med)}%  total {num(total, 4)} SOL  fees/trade {num(fees, 5)} SOL  "
                      f"median hold {num(hold, 0)} s")
            print("  exit reasons (count, mean result):")
            for mode, reason, n, mean in (await s.execute(text(EXITS_SQL), p)).all():
                print(f"    {mode:5s} {reason:28s} {n:>5}  {num(mean)}%")

            print("\n2. LIVE orders by side and status")
            orders = (await s.execute(text(ORDERS_SQL), p)).all()
            print("  none in the window" if not orders else "")
            for side, status, n in orders:
                print(f"  {side:4s} {status:10s} {n}")
            try:
                from yonixalpha_core import paper_execution

                r = await paper_execution.effective_rates(s)
                st = r.get("settings") or {}
                print(f"  paper failure rates in use: entry {r.get('entry_pct')}% ({r.get('entry_source')}), "
                      f"exit {r.get('exit_pct')}% ({r.get('exit_source')}); "
                      f"paper charged LIVE fixed costs: {st.get('charge_live_fixed_costs')}")
            except Exception as exc:  # noqa: BLE001
                print(f"  paper failure rates: not readable ({type(exc).__name__})")

            print("\n3. Solana entry decisions by target")
            for target, executable, n in (await s.execute(text(DECISIONS_SQL), p)).all():
                print(f"  {target:6s} {'executable' if executable else 'refused':10s} {n}")
            print("  top hard-block codes of refused decisions (newest 50k):")
            for target, code, n in (await s.execute(text(BLOCKS_SQL), p)).all():
                print(f"    {target:6s} {code:36s} {n}")

            print("\n4. Copy trading: every opportunity's disposition")
            copies = (await s.execute(text(COPY_SQL), p)).all()
            print("  no copy events in the window" if not copies else "")
            for chain, side, decision, reason, n in copies:
                print(f"  {chain:10s} {side:4s} {decision:8s} {n:>6}  {reason[:70]}")
            print("  median latency (ms): detection = source tx to seen; decision = seen to decided; total")
            for chain, det, dec, tot, n in (await s.execute(text(COPY_LATENCY_SQL), p)).all():
                print(f"    {chain:10s} detection {num(det, 0)}  decision {num(dec, 0)}  total {num(tot, 0)}  (n={n})")
            await s.rollback()
    finally:
        await engine.dispose()
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
