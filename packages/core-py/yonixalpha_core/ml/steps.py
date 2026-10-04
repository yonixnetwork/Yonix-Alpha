"""Timing of the ml service's steps (Solana training, gate models, shadow
models, EVM / wallet ML, ablation), kept in Redis for ML Review: when each
step last started, finished, how long it took, whether it failed and why.
A step that is RUNNING far longer than its interval is the one holding the
service up. Observability only: nothing reads this to decide anything.
"""

from __future__ import annotations

import json
import resource
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

STEPS_KEY = "yx:ml:steps"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_mb() -> float:
    """The process's peak resident memory so far (Linux reports KiB)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


async def _put(redis, step: str, value: dict[str, Any]) -> None:
    if redis is None:
        return
    try:
        await redis.hset(STEPS_KEY, step, json.dumps(value, default=str))
    except Exception:  # noqa: BLE001 - timing is never worth failing a step for
        pass


async def timed(redis, step: str, fn: Callable[[], Awaitable[Any]], log=None) -> Any:
    """Runs fn(), recording RUNNING / OK / FAILED with timestamps and the
    duration. Re-raises a failure (the caller records and alerts it)."""
    started, t0 = _now(), time.monotonic()
    prev = await read_one(redis, step)
    base = {"started_at": started, "last_ok_at": (prev or {}).get("last_ok_at"),
            "last_seconds": (prev or {}).get("seconds")}
    await _put(redis, step, {"state": "RUNNING", **base})
    try:
        result = await fn()
    except Exception as exc:
        secs = round(time.monotonic() - t0, 1)
        await _put(redis, step, {"state": "FAILED", **base, "finished_at": _now(), "seconds": secs,
                                 "peak_rss_mb": peak_rss_mb(), "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        raise
    secs = round(time.monotonic() - t0, 1)
    finished = _now()
    await _put(redis, step, {"state": "OK", **base, "finished_at": finished, "seconds": secs, "last_ok_at": finished,
                             "peak_rss_mb": peak_rss_mb()})
    if log is not None:
        log.info("ml.step_done", step=step, seconds=secs, peak_rss_mb=peak_rss_mb())
    return result


async def read_one(redis, step: str) -> dict[str, Any] | None:
    if redis is None:
        return None
    try:
        raw = await redis.hget(STEPS_KEY, step)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def read(redis) -> dict[str, dict[str, Any]]:
    """step -> its last record, plus running_s for a step still RUNNING."""
    if redis is None:
        return {}
    try:
        raw = await redis.hgetall(STEPS_KEY)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for k, v in (raw or {}).items():
        try:
            rec = json.loads(v)
        except ValueError:
            continue
        key = k.decode() if isinstance(k, bytes) else k
        if rec.get("state") == "RUNNING" and rec.get("started_at"):
            rec["running_s"] = round((now - datetime.fromisoformat(rec["started_at"])).total_seconds())
        out[key] = rec
    return out


async def mark_interrupted(redis) -> list[dict[str, Any]]:
    """At service start: a step still RUNNING was cut off when the previous
    process died (out of memory, kill, crash). Marks it INTERRUPTED and
    returns them, so the caller can alert: a silent restart loop must not
    look like a slow step."""
    out = []
    for step, rec in (await read(redis)).items():
        if rec.get("state") == "RUNNING":
            rec = {**rec, "state": "INTERRUPTED", "finished_at": _now(), "running_s": None,
                   "error": f"the ml process stopped while this step was running (started {rec.get('started_at')}); "
                            "out of memory is the usual cause on a small server"}
            await _put(redis, step, rec)
            out.append({"step": step, "started_at": rec.get("started_at")})
    return out
