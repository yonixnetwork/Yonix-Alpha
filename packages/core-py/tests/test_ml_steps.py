"""ml service step timing (observability for ML Review)."""

import os

import pytest
import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.ml import steps

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.delete(steps.STEPS_KEY)
    yield r
    await r.delete(steps.STEPS_KEY)
    await r.aclose()


async def test_steps_record_ok_failed_and_running(redis):
    async def ok():
        running = (await steps.read(redis))["a"]
        assert running["state"] == "RUNNING" and running["running_s"] >= 0
        return 42

    assert await steps.timed(redis, "a", ok) == 42
    a = (await steps.read(redis))["a"]
    assert a["state"] == "OK" and a["seconds"] >= 0 and a["last_ok_at"] == a["finished_at"]

    async def boom():
        raise ValueError("no data")
    with pytest.raises(ValueError):
        await steps.timed(redis, "a", boom)
    a2 = (await steps.read(redis))["a"]
    assert a2["state"] == "FAILED" and a2["error"] == "ValueError: no data" and a2["last_ok_at"] == a["last_ok_at"]


async def test_no_redis_is_harmless():
    async def ok():
        return 1
    assert await steps.timed(None, "x", ok) == 1 and await steps.read(None) == {}
