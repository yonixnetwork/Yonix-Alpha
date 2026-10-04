"""Stream-vs-logs cross-check of EVM detection (master §68)."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.chains.evm import crosscheck as xc

NOW = datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"))
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def _seen(redis, h, at, source="pending_tx", match=("launchpad",)):
    await redis.set(f"yx:evm:seen:bsc:{h}", json.dumps({"hash": h, "seen_at": at.isoformat(), "source": source,
                                                        "match": list(match)}), ex=900)


async def test_logs_to_stream_counts_coverage_and_lead_once(redis):
    await _seen(redis, "0xaa", NOW - timedelta(seconds=4))
    out = await xc.note_logged(redis, "bsc", [("launch", "0xAA"), ("trade", "0xbb"), ("trade", "0xbb"), ("trade", None)], NOW)
    assert out == {"launch_logged": 1, "launch_seen_first": 1, "trade_logged": 1, "trade_seen_first": 0}
    again = await xc.note_logged(redis, "bsc", [("launch", "0xaa")], NOW)  # a re-scan of the same range
    assert again["launch_logged"] == 0
    rep = await xc.report(redis, "bsc", NOW)
    assert rep["launches_seen_first_by_stream"] == {"logged": 1, "seen_first": 1, "rate": 1.0, "lead_s_median": 4.0,
                                                   "lead_s_p95": None}
    assert rep["trades_seen_first_by_stream"]["rate"] == 0.0


async def test_stream_to_logs_checks_each_transaction_once_after_the_grace(redis):
    await _seen(redis, "0x01", NOW - timedelta(minutes=10))  # logged
    await _seen(redis, "0x02", NOW - timedelta(minutes=10), source="sequencer_feed")  # never logged
    await _seen(redis, "0x03", NOW - timedelta(minutes=1))  # too recent
    await _seen(redis, "0x04", NOW - timedelta(minutes=10), match=("copy_target",))  # not a launchpad tx
    await xc.note_logged(redis, "bsc", [("trade", "0x01")], NOW - timedelta(minutes=9))
    out = await xc.sweep(redis, "bsc", NOW)
    assert out == {"checked": 2, "in_logs": 1, "not_in_logs": 1}
    assert await xc.sweep(redis, "bsc", NOW) == {"checked": 0, "in_logs": 0, "not_in_logs": 0}
    s = (await xc.report(redis, "bsc", NOW))["stream_txs_in_logs"]
    assert (s["checked"], s["in_logs"], s["not_in_logs"], s["rate"]) == (2, 1, 1, 0.5)
    assert s["by_source"]["sequencer_feed"] == {"checked": 1, "in_logs": 0, "not_in_logs": 1}


async def test_nothing_counted_is_not_available_never_zero(redis):
    rep = await xc.report(redis, "robinhood", NOW)
    assert rep["hours"] == [] and rep["stream_txs_in_logs"]["rate"] is None
    assert rep["launches_seen_first_by_stream"]["rate"] is None and rep["launches_seen_first_by_stream"]["lead_s_median"] is None
    assert await xc.note_logged(None, "bsc", [("launch", "0x1")], NOW) == {}
