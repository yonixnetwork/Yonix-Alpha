"""Short shared cache for the heavy review endpoints (ML Review, opportunity
comparison, EVM ML). Their numbers are aggregates over days of data that
move slowly; every open dashboard tab polled them every 60 s and each poll
ran the full aggregation again (audit 2026-10-07).

Single flight: while one request computes a key, the others wait for its
result instead of running the same query in parallel. The response carries
`cached_at` so the page can say how old the numbers are.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

PREFIX = "yx:api:cache:"
TTL_SECONDS = 60
WAIT_SECONDS = 25.0


async def cached(redis, key: str, compute: Callable[[], Awaitable[dict[str, Any]]], ttl: int = TTL_SECONDS,
                 wait_s: float = WAIT_SECONDS, poll_s: float = 0.25) -> dict[str, Any]:
    full = PREFIX + key
    raw = await redis.get(full)
    if raw:
        return json.loads(raw)
    lock = full + ":lock"
    if not await redis.set(lock, "1", nx=True, ex=int(wait_s * 2) + 5):
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            raw = await redis.get(full)
            if raw:
                return json.loads(raw)
            if not await redis.exists(lock):
                break  # the other request failed: compute it here
    try:
        value = json.loads(json.dumps(await compute(), default=str))
        value["cached_at"] = datetime.now(timezone.utc).isoformat()
        await redis.set(full, json.dumps(value), ex=ttl)
        return value
    finally:
        await redis.delete(lock)
