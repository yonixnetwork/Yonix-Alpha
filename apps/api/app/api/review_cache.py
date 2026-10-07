"""Shared cache for the heavy review endpoints (ML Review, opportunity
comparison, EVM ML). Their numbers are aggregates over days of data that
move slowly; every open dashboard tab polled them every 60 s and each poll
ran the full aggregation again (audit 2026-10-07).

The aggregation never runs inside a request. It runs in a background task on
the API's small review pool (its own, longer statement limit,
Settings.API_REVIEW_STATEMENT_TIMEOUT_MS), one at a time per API process, and
only one refresh per key runs across processes (a Redis lock). A request is
answered at once:
  - from the fresh result while it is younger than TTL_SECONDS;
  - otherwise from the last result, marked `stale` (a refresh is started);
  - with no result yet, it waits up to WAIT_SECONDS for the first one, then
    answers 503 REVIEW_COMPUTING (the page retries), or with the refresh error.
Server 2026-10-07: under load these aggregates took over 120 s, longer than
the reverse proxy's 60 s, so computing them in the request could only fail.
The response carries `cached_at` so the page can say how old the numbers are.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from yonixalpha_core.logging import get_logger

log = get_logger("api.review_cache")

PREFIX = "yx:api:cache:"
TTL_SECONDS = 1800  # server 2026-10-07: one result took 6-10 minutes to compute; 7-day aggregates move slowly
LAST_TTL_SECONDS = 7 * 86400  # the last good result, served while a refresh runs
ERROR_TTL_SECONDS = 600
WAIT_SECONDS = 25.0
LOCK_SECONDS = 45 * 60  # longer than a refresh can take, queued behind the other keys

_tasks: set[asyncio.Task] = set()
_slot: asyncio.Semaphore | None = None


def _one_at_a_time() -> asyncio.Semaphore:
    global _slot
    if _slot is None:
        _slot = asyncio.Semaphore(1)
    return _slot


async def cached(redis, key: str, compute: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
                 session_factory: async_sessionmaker[AsyncSession], ttl: int = TTL_SECONDS,
                 wait_s: float = WAIT_SECONDS, poll_s: float = 0.25) -> dict[str, Any]:
    full = PREFIX + key
    raw = await redis.get(full)
    if raw:
        return json.loads(raw)
    await _start_refresh(redis, full, compute, session_factory, ttl)
    last = await redis.get(full + ":last")
    if last:
        value = json.loads(last)
        value["stale"] = True
        err = await redis.get(full + ":error")
        if err:
            value["refresh_error"] = err if isinstance(err, str) else err.decode()
        return value
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        raw = await redis.get(full)
        if raw:
            return json.loads(raw)
        if not await redis.exists(full + ":lock"):
            err = await redis.get(full + ":error")
            if err:
                raise HTTPException(503, f"REVIEW_FAILED: the review numbers could not be computed: "
                                         f"{err if isinstance(err, str) else err.decode()}")
            await _start_refresh(redis, full, compute, session_factory, ttl)
    raise HTTPException(503, "REVIEW_COMPUTING: the review numbers are being computed in the background "
                             "(they can take minutes on a busy server); the page retries automatically")


async def _start_refresh(redis, full: str, compute, session_factory, ttl: int) -> None:
    if not await redis.set(full + ":lock", "1", nx=True, ex=LOCK_SECONDS):
        return  # a refresh of this key is already running
    task = asyncio.create_task(_refresh(redis, full, compute, session_factory, ttl))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _refresh(redis, full: str, compute, session_factory, ttl: int) -> None:
    started = time.monotonic()
    try:
        async with _one_at_a_time():
            async with session_factory() as session:
                value = json.loads(json.dumps(await compute(session), default=str))
        value["cached_at"] = datetime.now(timezone.utc).isoformat()
        value["compute_ms"] = int((time.monotonic() - started) * 1000)
        body = json.dumps(value)
        await redis.set(full, body, ex=ttl)
        await redis.set(full + ":last", body, ex=LAST_TTL_SECONDS)
        await redis.delete(full + ":error")
    except Exception as exc:  # noqa: BLE001 - kept for the page; the last result stays served
        msg = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
        log.warning("review_cache.refresh_failed", key=full.removeprefix(PREFIX), error=msg)
        try:
            await redis.set(full + ":error", msg, ex=ERROR_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await redis.delete(full + ":lock")
        except Exception:  # noqa: BLE001
            pass


async def shutdown() -> None:
    """Cancel running refreshes (API shutdown); their locks expire."""
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
