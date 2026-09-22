import os

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core import kill_switch


@pytest_asyncio.fixture
async def redis():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
    client = from_url(url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_not_engaged_by_default(redis):
    assert await kill_switch.is_engaged(redis) is False


async def test_engage_sets_engaged_true(redis):
    await kill_switch.engage(redis, "manual test trip")
    assert await kill_switch.is_engaged(redis) is True


async def test_engage_records_reason_with_timestamp(redis):
    await kill_switch.engage(redis, "daily loss limit hit")
    reason = await kill_switch.get_reason(redis)
    assert reason is not None
    assert "daily loss limit hit" in reason


async def test_disengage_clears_state(redis):
    await kill_switch.engage(redis, "test")
    await kill_switch.disengage(redis)
    assert await kill_switch.is_engaged(redis) is False
    assert await kill_switch.get_reason(redis) is None


async def test_disengage_when_never_engaged_is_safe(redis):
    await kill_switch.disengage(redis)  # must not raise
    assert await kill_switch.is_engaged(redis) is False


async def test_reengaging_overwrites_previous_reason(redis):
    await kill_switch.engage(redis, "first reason")
    await kill_switch.engage(redis, "second reason")
    reason = await kill_switch.get_reason(redis)
    assert "second reason" in reason
    assert "first reason" not in reason
