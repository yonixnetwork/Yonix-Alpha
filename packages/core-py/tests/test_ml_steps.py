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


async def test_a_step_left_running_by_a_dead_process_is_marked_interrupted(redis):
    await steps._put(redis, "solana_shadow", {"state": "RUNNING", "started_at": "2026-10-04T11:54:30+00:00"})
    await steps._put(redis, "gate_models", {"state": "OK", "started_at": "2026-10-04T11:54:29+00:00"})
    assert await steps.mark_interrupted(redis) == [{"step": "solana_shadow", "started_at": "2026-10-04T11:54:30+00:00"}]
    rec = (await steps.read(redis))
    assert rec["solana_shadow"]["state"] == "INTERRUPTED" and "out of memory" in rec["solana_shadow"]["error"]
    assert rec["gate_models"]["state"] == "OK" and await steps.mark_interrupted(redis) == []

    async def ok():
        return None
    await steps.timed(redis, "x", ok)
    assert (await steps.read(redis))["x"]["peak_rss_mb"] > 0


async def test_a_normal_stop_marks_running_steps_stopped_not_interrupted(redis):
    await steps._put(redis, "evm_wallet_ml", {"state": "RUNNING", "started_at": "2026-10-04T13:20:34+00:00"})
    assert await steps.mark_stopped(redis) == 1
    assert (await steps.read(redis))["evm_wallet_ml"]["state"] == "STOPPED"
    assert await steps.mark_interrupted(redis) == []  # a deploy is not a crash
