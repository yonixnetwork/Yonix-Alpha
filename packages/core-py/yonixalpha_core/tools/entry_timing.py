"""Entry timing report (read-only): where the time goes between a token's
launch and our entry, and how far the price moved meanwhile.

    $C exec api python -m yonixalpha_core.tools.entry_timing [--hours 24] [--entered-only]

Prints, from the database (and Redis where held):
  1. latency per stage over recent pump-stream candidates: median and p90
     (detection, promotion, first gate evaluation, gate wait, queue,
     quote, build/sign, submission, landing, confirmation, launch -> entry)
  2. waiting time split by cause (strategy / data / risk / execution /
     safety) and the gate codes that blocked most often
  3. entered tokens: first detection, first entry-strategy signal (shadow,
     after this upgrade), entry, the price change between them and whether
     the token was already decelerating
  4. the entry-strategy signals recorded so far, per strategy and baseline

Works on history from before this upgrade (database timestamps); the
stream-receive time and the shadow signals exist only after it. A value
that was not recorded is shown as "-". Nothing is written.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from yonixalpha_core import entry_store, entry_timing
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory


def _n(v, d: int = 1) -> str:
    return "-" if v is None else f"{v:.{d}f}"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--entered-only", action="store_true")
    ap.add_argument("--limit", type=int, default=150)
    a = ap.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    settings = get_settings()
    engine = make_engine(settings)
    redis = None
    try:
        from yonixalpha_core.db.redis import make_redis

        redis = make_redis(settings)
    except Exception:  # noqa: BLE001 - the database part still works
        redis = None
    try:
        async with make_session_factory(engine)() as s:
            summary = await entry_timing.latency_summary(s, redis, since, limit=a.limit, entered_only=a.entered_only)
            print(f"entry_timing: last {a.hours:g} h (since {since:%Y-%m-%d %H:%M} UTC), "
                  f"{summary['candidates']} candidates, {summary['entered']} entered\n")
            print("1. Latency per stage (seconds): median / p90 (n)")
            for k, v in summary["latency_seconds"].items():
                print(f"  {k:32s} {_n(v['median'])} / {_n(v['p90'])}  (n={v['n']})")
            print("\n2. Waiting before the approving evaluation, by cause (median seconds per candidate; total)")
            for k, v in summary["waiting_seconds_median"].items():
                print(f"  {k:10s} {_n(v)}   total {_n(summary['waiting_seconds_total'].get(k))}")
            print("  most frequent blocking codes:")
            for code, n in summary["top_blocking_codes"].items():
                print(f"    {code:36s} {n}")
            late = await entry_timing.late_entries(s, redis, since)
            print(f"\n3. Entered tokens: {late['entries']}, entered 50%+ above the first detection price: "
                  f"{late['late_by_50pct_or_more']}")
            for r in late["rows"][:25]:
                print(f"  {r['mode'] or '?':5s} {r['symbol'] or r['mint'][:8]:12s} detect->entry {_n(r['seconds_detection_to_entry'])} s"
                      f" {_n(r['price_change_detection_to_entry_pct'])}%  signal->entry {_n(r['seconds_signal_to_entry'])} s"
                      f" {_n(r['price_change_signal_to_entry_pct'])}%  decelerating={r['decelerating_at_entry']}"
                      f"  pnl {_n(r['realized_pnl_pct'])}%  [{r['prices_from'] or 'no prices'}]")
            counts = await entry_store.counts(s, since)
            print("\n4. Entry-strategy signals recorded (shadow) and labelled")
            if not counts:
                print("  none yet (recorded only after this upgrade is deployed)")
            for k, v in sorted(counts.items()):
                print(f"  {k:32s} {v['signals']:>6} signals, {v['labelled']:>6} labelled")
            await s.rollback()
    finally:
        if redis is not None:
            await redis.aclose()
        await engine.dispose()
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
