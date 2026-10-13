"""Aggregated RPC usage and avoided-work counters (low overhead).

Every RpcManager counts its requests in memory (per endpoint, method,
priority and outcome, plus latency and coalesced duplicates) and adds the
deltas to one Redis hash per UTC hour at most once a minute. No database
write per call, no URLs or keys anywhere: endpoints appear by their
dashboard name or failover label.

These are CALL COUNTS, not credits. A provider's credit cost differs by
method (and for streams by data volume); compare them with the provider's own
usage dashboard rather than multiplying by an assumed price.

    yx:rpc:usage:<YYYYMMDDHH>   field "<service>|<endpoint>|<method>|<priority>|<outcome>" -> calls
                                field "<service>|<endpoint>|<method>|latency_ms"          -> summed ms
                                field "<service>|*|<method>|<priority>|coalesced"         -> duplicates served
    yx:gate:entry_blocked:<YYYYMMDD>  field "<engine>|<code>" -> evaluations refused before any token data was fetched

Read with: python -m yonixalpha_core.tools.rpc_usage [--hours 24]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

USAGE_KEY = "yx:rpc:usage:"
BLOCKED_KEY = "yx:gate:entry_blocked:"
TTL_SECONDS = 8 * 86400
FLUSH_EVERY_SECONDS = 60.0


def hour_key(now: datetime | None = None) -> str:
    return USAGE_KEY + (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H")


def day_key(now: datetime | None = None) -> str:
    return BLOCKED_KEY + (now or datetime.now(timezone.utc)).strftime("%Y%m%d")


async def flush(redis: Any, service: str, counts: dict[str, float], flushed: dict[str, float],
                now: datetime | None = None) -> int:
    """Adds counts - flushed (the deltas since the last flush) to this hour's
    hash in one pipeline and records them as flushed. Returns the number of
    fields written. A Redis failure leaves the deltas for the next flush."""
    deltas = {k: v - flushed.get(k, 0) for k, v in counts.items() if v - flushed.get(k, 0)}
    if not deltas or redis is None:
        return 0
    key = hour_key(now)
    pipe = redis.pipeline(transaction=False)
    for field, d in deltas.items():
        if isinstance(d, float) and not d.is_integer():
            pipe.hincrbyfloat(key, f"{service}|{field}", round(d, 3))
        else:
            pipe.hincrby(key, f"{service}|{field}", int(d))
    pipe.expire(key, TTL_SECONDS)
    await pipe.execute()
    for field, d in deltas.items():
        flushed[field] = flushed.get(field, 0) + d
    return len(deltas)


async def count_entry_blocked(redis: Any, engine: str, codes: list[str], now: datetime | None = None) -> None:
    """One evaluation refused by account-level blockers before any token data
    was fetched (counted once per code)."""
    if redis is None:
        return
    key = day_key(now)
    try:
        pipe = redis.pipeline(transaction=False)
        for code in codes:
            pipe.hincrby(key, f"{engine}|{code}", 1)
        pipe.hincrby(key, f"{engine}|*", 1)
        pipe.expire(key, TTL_SECONDS)
        await pipe.execute()
    except Exception:  # noqa: BLE001 - counting must never block a decision
        pass


def _s(x) -> str:
    return x.decode() if isinstance(x, bytes) else str(x)


async def entry_blocked_today(redis: Any, now: datetime | None = None) -> dict[str, int]:
    raw = await redis.hgetall(day_key(now))
    return {_s(k): int(v) for k, v in raw.items()}


async def read_usage(redis: Any, hours: int = 24, now: datetime | None = None) -> dict[str, float]:
    """Summed fields of the last `hours` hourly hashes."""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    total: dict[str, float] = {}
    for i in range(hours):
        raw = await redis.hgetall(hour_key(now - timedelta(hours=i)))
        for k, v in raw.items():
            total[_s(k)] = total.get(_s(k), 0) + float(v)
    return total
