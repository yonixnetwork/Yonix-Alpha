"""The review cache never computes inside the request: a fresh result is
served, then the last result (stale) while a background refresh runs; with no
result yet it waits briefly, then answers 503 REVIEW_COMPUTING; a failed
refresh keeps the last result served and says why. Server 2026-10-07: these
aggregates took over 120 s under load, past the proxy's 60 s."""

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

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


async def test_first_result_is_waited_for_then_served_fresh(redis):
    calls = []

    async def compute(session):
        calls.append(session)
        return {"n": len(calls)}

    v = await review_cache.cached(redis, "k", compute, sessions(), wait_s=2, poll_s=0.01)
    assert v["n"] == 1 and v["cached_at"] and "stale" not in v and calls == ["session"]
    assert (await review_cache.cached(redis, "k", compute, sessions()))["n"] == 1  # fresh: not computed again


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
    assert v["n"] == 1 and v["stale"] is True  # answered at once from the last result
    again = await review_cache.cached(redis, "k", compute, sessions())
    assert again["stale"] is True and n["v"] == 2  # the running refresh is not started twice
    gate.set()
    await settle()
    v = await review_cache.cached(redis, "k", compute, sessions())
    assert v["n"] == 2 and "stale" not in v


async def test_slow_first_result_answers_review_computing(redis):
    gate = asyncio.Event()

    async def compute(_):
        await gate.wait()
        return {"n": 1}

    with pytest.raises(HTTPException) as e:
        await review_cache.cached(redis, "k", compute, sessions(), wait_s=0.1, poll_s=0.01)
    assert e.value.status_code == 503 and e.value.detail.startswith("REVIEW_COMPUTING")
    gate.set()
    await settle()
    assert (await review_cache.cached(redis, "k", compute, sessions()))["n"] == 1


async def test_failed_refresh_keeps_the_last_result_and_says_why(redis):
    state = {"fail": False}

    async def compute(_):
        if state["fail"]:
            raise RuntimeError("canceling statement due to statement timeout")
        return {"n": 1}

    with pytest.raises(HTTPException) as e:
        state["fail"] = True
        await review_cache.cached(redis, "x", compute, sessions(), wait_s=1, poll_s=0.01)
    assert e.value.detail.startswith("REVIEW_FAILED") and "statement timeout" in e.value.detail

    state["fail"] = False
    await review_cache.cached(redis, "k", compute, sessions(), wait_s=1, poll_s=0.01)
    await redis.delete(review_cache.PREFIX + "k")
    state["fail"] = True
    await review_cache.cached(redis, "k", compute, sessions())
    await settle()
    v = await review_cache.cached(redis, "k", compute, sessions())
    assert v["n"] == 1 and v["stale"] is True and "statement timeout" in v["refresh_error"]
