"""Database / API health (read-only): where the dashboard's time goes.

    $C exec api python -m yonixalpha_core.tools.db_health [--skip-timings]

Prints, in order:
  1. host load and memory (the containers share the droplet's 2 vCPU / 2 GB)
  2. database connections by state, and every statement running > 2 s
  3. lock waits (who is blocked, by whom)
  4. the largest tables (estimated rows, total size)
  5. timings of the queries behind the pages that ended in 504 on
     2026-10-07 (ML Review, opportunity outcomes, EVM ML), each in its own
     read-only transaction with a 120 s limit
  6. the API's own slow / failed requests (app.request_timing), the review
     pages' background results (apps/api review_cache: when each was
     computed, how long it took, the last error) and the ml service's steps

Use --skip-timings for a quick run: section 5 can take minutes on a busy
server.

Written for the 2026-10-07 audit. Nothing is written to the database; the
timed queries run in READ ONLY transactions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis

TABLES_SHOWN = 15
TIMING_LIMIT_MS = 120_000
REVIEW_CACHE_PREFIX = "yx:api:cache:"  # apps/api app.api.review_cache.PREFIX
SLOW_REQUESTS_KEY = "yx:api:slow_requests"  # app.request_timing.SLOW_KEY


def host_lines() -> list[str]:
    out = []
    try:
        load = open("/proc/loadavg").read().split()[:3]
        out.append(f"load average (1/5/15 min): {' '.join(load)}  (2 vCPU: above 2 means work is waiting for a CPU)")
    except OSError:
        out.append("load average: not readable")
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
        total, avail = mem.get("MemTotal", 0) // 1024, mem.get("MemAvailable", 0) // 1024
        swap_used = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024
        out.append(f"memory: {avail} MB available of {total} MB; swap used {swap_used} MB")
    except (OSError, ValueError):
        out.append("memory: not readable")
    return out


ACTIVITY_SQL = """
SELECT pid, state, coalesce(wait_event_type, '') || ':' || coalesce(wait_event, '') AS waiting,
       extract(epoch FROM now() - query_start)::int AS seconds, left(regexp_replace(query, '\\s+', ' ', 'g'), 160) AS q
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'
  AND now() - query_start > interval '2 seconds'
ORDER BY query_start
"""

LOCKS_SQL = """
SELECT w.pid AS waiting_pid, extract(epoch FROM now() - w.query_start)::int AS seconds,
       left(regexp_replace(w.query, '\\s+', ' ', 'g'), 100) AS waiting_query,
       b.pid AS blocking_pid, left(regexp_replace(b.query, '\\s+', ' ', 'g'), 100) AS blocking_query
FROM pg_stat_activity w
JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bp(pid) ON true
JOIN pg_stat_activity b ON b.pid = bp.pid
WHERE w.datname = current_database()
"""

TABLES_SQL = """
SELECT c.relname, c.reltuples::bigint AS rows, pg_total_relation_size(c.oid) AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC LIMIT :n
"""


def mb(n: int) -> str:
    return f"{n / 1024 / 1024:,.0f} MB" if n >= 1024 * 1024 else f"{n / 1024:,.0f} kB"


async def timed(session_factory, label: str, fn: Callable[[Any], Awaitable[Any]]) -> str:
    async with session_factory() as s:
        try:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            await s.execute(text(f"SET LOCAL statement_timeout = {TIMING_LIMIT_MS}"))
            t0 = time.monotonic()
            await fn(s)
            ms = int((time.monotonic() - t0) * 1000)
            verdict = "OK" if ms < 5000 else "SLOW" if ms < 25000 else "TOO SLOW (over the API's 25 s limit)"
            return f"  {label}: {ms:,} ms  {verdict}"
        except Exception as exc:  # noqa: BLE001
            return f"  {label}: FAILED {type(exc).__name__}: {str(exc)[:140]}"
        finally:
            await s.rollback()


def timings(now: datetime) -> list[tuple[str, Callable[[Any], Awaitable[Any]]]]:
    from sqlalchemy import func, select

    from yonixalpha_core import opportunities
    from yonixalpha_core.db.models import CopyEvent, EvmTrade, OpportunityOutcome, WalletTradeLabel
    from yonixalpha_core.ml import evm_samples

    week, fortnight = now - timedelta(days=7), now - timedelta(days=14)
    o = OpportunityOutcome

    async def losses_all(s):
        await s.execute(select(func.count()).select_from(o).where(o.loss_analysis.is_not(None)))

    async def category_all(s):
        await s.execute(select(func.count()).select_from(o).where(opportunities.category_filter("missed_win")))

    async def wallet_labels(s):
        each = select(func.jsonb_array_elements_text(WalletTradeLabel.labels).label("l")).where(
            WalletTradeLabel.entry_at >= fortnight, WalletTradeLabel.kind.in_(("EPISODE", "MISSED"))).subquery()
        await s.execute(select(each.c.l, func.count()).group_by(each.c.l))

    async def copy_outcomes(s):
        await s.execute(select(func.count()).where(CopyEvent.outcome.is_not(None), CopyEvent.target_at >= fortnight))

    async def copy_poll(s):  # the shape of services/copy-engine target_trades
        await s.execute(select(EvmTrade.event_id).where(EvmTrade.chain == "bsc", EvmTrade.at >= now - timedelta(minutes=10),
                                                         func.lower(EvmTrade.trader).in_(["0x" + "0" * 40])))

    return [
        ("ML Review: ledger review counts (7 days)", lambda s: opportunities.review(s, week)),
        ("Opportunity outcomes: comparison (7 days)", lambda s: opportunities.comparison(s, week)),
        ("Losing trades list: count over ALL history (before the fix)", losses_all),
        ("Ledger category list: count over ALL history (before the fix)", category_all),
        ("EVM ML: samples knowledge (14 days)", lambda s: evm_samples.knowledge(s, fortnight)),
        ("EVM ML: wallet label counts (14 days)", wallet_labels),
        ("EVM ML: copy outcomes count (14 days)", copy_outcomes),
        ("Copy engine: one tick's target poll on BSC (runs every second)", copy_poll),
    ]


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-timings", action="store_true", help="only the cheap checks (activity, locks, sizes)")
    a = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    redis = make_redis(settings)
    now = datetime.now(timezone.utc)
    print(f"db_health at {now:%Y-%m-%d %H:%M:%S} UTC\n")
    print("1. Host")
    for line in host_lines():
        print("  " + line)
    try:
        async with factory() as s:
            print("\n2. Database connections")
            for state, n in (await s.execute(text(
                    "SELECT coalesce(state, 'background'), count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() GROUP BY 1 ORDER BY 2 DESC"))).all():
                print(f"  {state}: {n}")
            maxc = (await s.execute(text("SHOW max_connections"))).scalar_one()
            print(f"  max_connections: {maxc}")
            rows = (await s.execute(text(ACTIVITY_SQL))).all()
            print(f"  statements running longer than 2 s: {len(rows)}")
            for pid, state, waiting, secs, q in rows:
                print(f"    pid {pid} {state} {secs}s wait={waiting} :: {q}")
            print("\n3. Lock waits")
            locks = (await s.execute(text(LOCKS_SQL))).all()
            print("  none" if not locks else "")
            for wpid, secs, wq, bpid, bq in locks:
                print(f"  pid {wpid} waiting {secs}s on pid {bpid}\n    waiting: {wq}\n    holder:  {bq}")
            print("\n4. Largest tables")
            for name, est, size in (await s.execute(text(TABLES_SQL), {"n": TABLES_SHOWN})).all():
                print(f"  {name:32s} ~{max(est, 0):>12,} rows  {mb(size)}")
        if not a.skip_timings:
            print("\n5. Timed page queries (read-only, 120 s limit each)")
            for label, fn in timings(now):
                print(await timed(factory, label, fn), flush=True)
        print("\n6. API slow / failed requests (newest first, max 20)")
        try:
            raw = await redis.lrange(SLOW_REQUESTS_KEY, 0, 19)
            if not raw:
                print("  none recorded (recorded from this version on)")
            for r in raw:
                e = json.loads(r)
                print(f"  {e.get('at', '')[:19]} {e.get('status')} {e.get('ms'):>6} ms {e.get('method')} {e.get('path')} "
                      f"(request {e.get('request_id')})")
        except Exception as exc:  # noqa: BLE001
            print(f"  not readable: {type(exc).__name__}")
        print("\n   review pages, background results (apps/api review_cache)")
        try:
            shown = 0
            async for k in redis.scan_iter(match=REVIEW_CACHE_PREFIX + "*:last", count=200):
                k = k if isinstance(k, str) else k.decode()
                base = k.removesuffix(":last")
                v = json.loads(await redis.get(k) or "{}")
                fresh = "fresh" if await redis.exists(base) else "stale"
                running = " refreshing" if await redis.exists(base + ":lock") else ""
                err = await redis.get(base + ":error")
                print(f"  {base.removeprefix(REVIEW_CACHE_PREFIX)}: computed {str(v.get('cached_at', ''))[:19]} in "
                      f"{v.get('compute_ms', '-')} ms, {fresh}{running}" + (f"; last refresh FAILED: {err}" if err else ""))
                shown += 1
            if not shown:
                print("  none yet (computed when a review page is first opened)")
        except Exception as exc:  # noqa: BLE001
            print(f"  not readable: {type(exc).__name__}")
        print("\n   ml service steps")
        try:
            from yonixalpha_core.ml import steps

            for name, st in sorted((await steps.read(redis)).items()):
                print(f"  {name}: {st.get('state')} started {str(st.get('started_at', ''))[:19]} "
                      f"seconds {st.get('seconds', '-')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  not readable: {type(exc).__name__}")
    finally:
        await engine.dispose()
        await redis.aclose()
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
