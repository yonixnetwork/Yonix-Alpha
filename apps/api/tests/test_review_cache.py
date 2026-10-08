"""The review cache never computes inside the request (app.api.review_cache):
CURRENT while fresh; STALE (refreshing) after; RUNNING / PENDING with no
result yet; FAILED with the error; deferred under resource pressure; an
explicit refresh recomputes a current result. Server 2026-10-07: these
aggregates took over 120 s under load, past the proxy's 60 s."""

import asyncio
import contextlib

import pytest

from app.api import review_cache
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.redis import make_redis


@pytest.fixture
async def redis():
    r = make_redis(get_settings())
    await r.flushdb()
    yield r
    await review_cache.shutdown()
    await r.flushdb()
    await r.aclose()


def sessions():
    @contextlib.asynccontextmanager
    async def factory():
        yield "session"
    return factory


async def settle():
    while review_cache._tasks:
        await asyncio.sleep(0.01)


async def test_first_result_is_waited_for_then_served_current(redis):
    calls = []

    async def compute(session):
        calls.append(session)
        return {"n": len(calls)}

    v = await review_cache.cached(redis, "k", compute, sessions(), wait_s=2, poll_s=0.01)
    assert (v["n"], v["review_status"]) == (1, "CURRENT") and v["cached_at"] and v["age_s"] >= 0
    again = await review_cache.cached(redis, "k", compute, sessions())
    assert again["n"] == 1 and calls == ["session"]  # not computed again on a page refresh


async def test_expired_result_is_served_stale_while_the_refresh_runs(redis):
    gate = asyncio.Event()
    n = {"v": 0}

    async def compute(_):
        n["v"] += 1
        if n["v"] > 1:
            await gate.wait()
        return {"n": n["v"]}

    await review_cache.cached(redis, "k", compute, sessions(), wait_s=2, poll_s=0.01)
    await redis.delete(review_cache.PREFIX + "k")  # the fresh copy expired
    v = await asyncio.wait_for(review_cache.cached(redis, "k", compute, sessions()), 1)
    assert (v["n"], v["review_status"], v["refreshing"]) == (1, "STALE", True)
    assert (await review_cache.cached(redis, "k", compute, sessions()))["review_status"] == "STALE" and n["v"] == 2
    gate.set()
    await settle()
    v = await review_cache.cached(redis, "k", compute, sessions())
    assert (v["n"], v["review_status"]) == (2, "CURRENT") and "refreshing" not in v


async def test_slow_first_result_answers_running_then_current(redis):
    gate = asyncio.Event()

    async def compute(_):
        await gate.wait()
        return {"n": 1}

    v = await review_cache.cached(redis, "k", compute, sessions(), wait_s=0.1, poll_s=0.01)
    assert v["review_status"] == "RUNNING" and review_cache.pending(v)
    gate.set()
    await settle()
    assert (await review_cache.cached(redis, "k", compute, sessions()))["review_status"] == "CURRENT"


async def test_failed_refresh_keeps_the_last_result_and_says_why(redis):
    state = {"fail": True}

    async def compute(_):
        if state["fail"]:
            raise RuntimeError("canceling statement due to statement timeout")
        return {"n": 1}

    v = await review_cache.cached(redis, "x", compute, sessions(), wait_s=1, poll_s=0.01)
    assert v["review_status"] == "FAILED" and "statement timeout" in v["error"] and not review_cache.pending(v)

    state["fail"] = False
    await review_cache.cached(redis, "k", compute, sessions(), wait_s=1, poll_s=0.01)
    await redis.delete(review_cache.PREFIX + "k")
    state["fail"] = True
    await review_cache.cached(redis, "k", compute, sessions())
    await settle()
    v = await review_cache.cached(redis, "k", compute, sessions())
    assert (v["n"], v["review_status"]) == (1, "STALE") and "statement timeout" in v["refresh_error"]


async def test_resource_pressure_defers_and_explicit_refresh_recomputes(redis):
    n = {"v": 0}

    async def compute(_):
        n["v"] += 1
        return {"n": n["v"]}

    v = await review_cache.cached(redis, "k", compute, sessions(), defer_reason="resource level CRITICAL: load")
    assert v["review_status"] == "PENDING" and "CRITICAL" in v["deferred"] and n["v"] == 0  # nothing started
    await review_cache.cached(redis, "k", compute, sessions(), wait_s=1, poll_s=0.01)
    await redis.delete(review_cache.PREFIX + "k")
    v = await review_cache.cached(redis, "k", compute, sessions(), defer_reason="EMERGENCY resource mode")
    assert (v["review_status"], v["deferred"]) == ("STALE", "EMERGENCY resource mode") and n["v"] == 1
    await settle()
    await review_cache.cached(redis, "k", compute, sessions(), wait_s=1, poll_s=0.01)
    await settle()  # that call started a refresh of the expired copy: let it finish
    v = await review_cache.cached(redis, "k", compute, sessions(), refresh=True)
    assert v["review_status"] == "CURRENT" and v["refreshing"] is True
    before = v["n"]
    await settle()
    assert (await review_cache.cached(redis, "k", compute, sessions()))["n"] == before + 1
