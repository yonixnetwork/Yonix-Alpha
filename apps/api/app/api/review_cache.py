"""Shared cache for the heavy review endpoints (ML Review, opportunity
comparison, EVM ML). Their numbers are aggregates over days of data that
move slowly; every open dashboard tab polled them every 60 s and each poll
ran the full aggregation again (audit 2026-10-07).

The aggregation never runs inside a request. It runs in a background task on
the API's small review pool (its own, longer statement limit,
Settings.API_REVIEW_STATEMENT_TIMEOUT_MS), one at a time per API process, and
only one refresh per key runs across processes (a Redis lock). A request is
answered at once, with `review_status`:
  CURRENT  the result is younger than TTL_SECONDS
  STALE    the last result; a refresh is running (`refreshing`) or deferred
           (`deferred`: the host is CRITICAL or in EMERGENCY mode, so this
           priority-3 work waits), or the last refresh failed (`refresh_error`)
  RUNNING  no result yet; the first one is being computed (HTTP 202)
  PENDING  no result yet and the computation is deferred (HTTP 202)
  FAILED   no result and the computation failed (HTTP 200, with `error`)
With no result, the request waits up to WAIT_SECONDS for a quick first one.
`refresh=True` (explicit) starts a refresh even of a CURRENT result.
Server 2026-10-07: under load these aggregates took over 120 s, longer than
the reverse proxy's 60 s, so computing them in the request could only fail.
The response carries `cached_at` and `age_s` so the page says how old the
numbers are.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

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


def _txt(v) -> str:
    return v if isinstance(v, str) else v.decode()


def _with_age(value: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    value = {**value, "review_status": status, **{k: v for k, v in extra.items() if v is not None}}
    if value.get("cached_at"):
        value["age_s"] = round(time.time() - datetime.fromisoformat(value["cached_at"]).timestamp())
    return value


async def cached(redis, key: str, compute: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
                 session_factory: async_sessionmaker[AsyncSession], ttl: int = TTL_SECONDS,
                 wait_s: float = WAIT_SECONDS, poll_s: float = 0.25, refresh: bool = False,
                 defer_reason: str | None = None) -> dict[str, Any]:
    """The result with its review_status (module docstring). defer_reason:
    resource pressure; no computation is started while it is set."""
    full = PREFIX + key
    raw = await redis.get(full)
    if raw and not (refresh and not defer_reason):
        return _with_age(json.loads(raw), "CURRENT", deferred=defer_reason if refresh else None)
    if not defer_reason:
        await _start_refresh(redis, full, compute, session_factory, ttl)
    if raw:  # explicit refresh of a current result: served while it recomputes
        return _with_age(json.loads(raw), "CURRENT", refreshing=True)
    last = await redis.get(full + ":last")
    err = await redis.get(full + ":error")
    if last:
        return _with_age(json.loads(last), "STALE", refreshing=None if defer_reason else True, deferred=defer_reason,
                         refresh_error=_txt(err) if err else None)
    if defer_reason:
        return {"review_status": "PENDING", "deferred": defer_reason,
                "message": "not computed yet: waiting until the server has resources for background work"}
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        raw = await redis.get(full)
        if raw:
            return _with_age(json.loads(raw), "CURRENT")
        if not await redis.exists(full + ":lock"):
            err = await redis.get(full + ":error")
            if err:
                return {"review_status": "FAILED", "error": _txt(err),
                        "message": "the review numbers could not be computed; retried on the next request"}
            await _start_refresh(redis, full, compute, session_factory, ttl)
    return {"review_status": "RUNNING",
            "message": "being computed in the background (minutes on a busy server); the page updates by itself"}


def pending(result: dict[str, Any]) -> bool:
    """No result to show yet (RUNNING / PENDING): answered as HTTP 202."""
    return result.get("review_status") in ("RUNNING", "PENDING")


async def defer_reason(app_state, settings, explicit: bool = False) -> str | None:
    """Why background review work must wait now: EMERGENCY mode, or a
    CRITICAL host resource level (priority 3 yields; yonixalpha_core.resources).
    An explicit refresh by the operator waits only in EMERGENCY mode."""
    from yonixalpha_core import operating_mode, resources

    try:
        async with app_state.db_session_factory() as session:
            st = await operating_mode.load(session, settings)
        if st["resource_mode"] == "EMERGENCY":
            return "EMERGENCY resource mode"
        lvl, why = resources.level(resources.sample(), settings)
        if lvl == resources.CRITICAL and not explicit:
            return "resource level CRITICAL: " + "; ".join(why)
    except Exception:  # noqa: BLE001 - an unreadable state never blocks the page
        return None
    return None


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
