"""RPC usage by service, endpoint and method, read-only (yonixalpha_core.rpc_usage).

  python -m yonixalpha_core.tools.rpc_usage              last 24 hours
  python -m yonixalpha_core.tools.rpc_usage --hours 1

CALL COUNTS, not credits: a provider prices methods (and stream bytes)
differently; compare with the provider's own usage dashboard. Endpoints are
shown by their dashboard name or failover label, never by URL. Counting starts
when a service runs this version; earlier hours show nothing.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from yonixalpha_core import rpc_usage
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.redis import make_redis

OUTCOMES = ("ok", "failed", "rejected", "http_429", "failover_retry")


def summarize(fields: dict[str, float]) -> tuple[list[tuple], list[tuple], dict[str, float]]:
    """(rows per service/endpoint/method, cache rows per method, totals by outcome)."""
    calls: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cache: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    totals: dict[str, float] = defaultdict(float)
    for key, v in fields.items():
        parts = key.split("|")
        if len(parts) == 4 and parts[3] == "latency_ms":
            service, endpoint, method, _ = parts
            calls[(service, endpoint, method)]["latency_ms"] += v
        elif len(parts) == 5:
            service, endpoint, method, priority, outcome = parts
            if priority == "cache":
                cache[f"{service}|{method}"][outcome] += v
                continue
            row = calls[(service, endpoint, method)]
            row[outcome] += v
            row[f"prio_{priority}"] += v if outcome in ("ok", "failed", "rejected", "http_429") else 0
            totals[outcome] += v
    rows = []
    for (service, endpoint, method), r in calls.items():
        sent = sum(r.get(o, 0) for o in ("ok", "failed", "rejected", "http_429"))
        rows.append((service, endpoint, method, int(sent), *(int(r.get(o, 0)) for o in OUTCOMES),
                     int(r.get("coalesced", 0)), int(r.get("shed", 0)),
                     round(r.get("latency_ms", 0) / sent, 1) if sent else None,
                     int(r.get("prio_critical", 0)), int(r.get("prio_normal", 0)), int(r.get("prio_background", 0))))
    rows.sort(key=lambda x: -x[3])
    cache_rows = sorted(((k, int(v.get("hit", 0)), int(v.get("miss", 0))) for k, v in cache.items()), key=lambda x: -x[1])
    return rows, cache_rows, totals


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args(argv)
    redis = make_redis(get_settings())
    now = datetime.now(timezone.utc)
    try:
        fields = await rpc_usage.read_usage(redis, hours=a.hours, now=now)
        blocked = await rpc_usage.entry_blocked_today(redis, now)
    finally:
        await redis.aclose()
    rows, cache_rows, totals = summarize(fields)
    print(f"RPC CALL COUNTS (not credits), last {a.hours} h to {now:%Y-%m-%d %H:%M} UTC")
    if not rows:
        print("  nothing recorded yet (counting starts when the services run this version)")
    print(f"  {'service':<24} {'endpoint':<14} {'method':<28} {'sent':>8} {'ok':>8} {'failed':>7} {'reject':>7} "
          f"{'429':>6} {'retry':>6} {'shared':>7} {'shed':>6} {'avg ms':>7}  crit/norm/bg")
    for r in rows[:60]:
        print(f"  {r[0]:<24} {r[1]:<14} {r[2]:<28} {r[3]:>8} {r[4]:>8} {r[5]:>7} {r[6]:>7} {r[7]:>6} {r[8]:>6} "
              f"{r[9]:>7} {r[10]:>6} {r[11] if r[11] is not None else '-':>7}  {r[12]}/{r[13]}/{r[14]}")
    if cache_rows:
        print("  token-read cache (decision engine): reads served from cache vs fetched")
        for k, hit, miss in cache_rows:
            print(f"    {k:<54} hit {hit:>7}  miss {miss:>7}  saved {hit / (hit + miss):.0%}" if hit + miss else k)
    print("  totals: " + ", ".join(f"{k} {int(v)}" for k, v in sorted(totals.items())))
    print("ENTRIES REFUSED BEFORE ANY TOKEN DATA WAS FETCHED (today, UTC; evaluations, not tokens)")
    if not blocked:
        print("  none")
    for k, v in sorted(blocked.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<48} {v}")
    print("Nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
