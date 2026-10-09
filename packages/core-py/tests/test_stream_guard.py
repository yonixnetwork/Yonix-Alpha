"""Fresh-token stream guard (2026-10-09): launches PumpPortal announced but
the RPC WebSocket never delivered are read from their own transaction; a
silent or lossy stream is detected; the WebSocket can be forced onto the
next provider. Needs the local test Redis."""

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.solana import pump_stream, pumpportal_ws, stream_guard
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.solana.pumpfun import CREATE_EVENT, EVENT_IX_TAG, PUMP_PROGRAM_ID, TRADE_EVENT
from yonixalpha_core.solana.ws import SolanaWsClient
from yonixalpha_core.testing.pump import CREATOR, CURVE, MINT, i64, logs_of, pk, s

OTHER = "8" * 43 + "p"


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def create_event(mint: str = MINT) -> bytes:
    body = s("Moon") + s("MOON") + s("https://x/m.json") + pk(mint) + pk(CURVE) + pk(CREATOR) + pk(CREATOR)
    return CREATE_EVENT + body + i64(1_790_000_000)


class FakeRpc:
    def __init__(self, txs: dict):
        self.txs, self.calls = txs, []

    async def call(self, method, params=None, priority=None):
        self.calls.append((method, params[0]))
        if params[0] == "boom":
            raise RuntimeError("rpc down")
        return self.txs.get(params[0])


async def announce(redis, mint: str, sig: str, age: float, now: float) -> None:
    await pumpportal_ws.ingest(redis, {"txType": "create", "mint": mint, "signature": sig, "name": "Moon"}, now - age)


def test_create_lines_read_logs_then_self_cpi_and_ignore_other_mints():
    in_logs = {"meta": {"logMessages": logs_of(create_event())}}
    assert len(stream_guard.create_lines(in_logs, MINT)) == 1
    assert stream_guard.create_lines(in_logs, OTHER) == []  # a create for another mint is not ours
    # emit_cpi form: no "Program data:" line, the event is a self-invoked inner instruction
    cpi = {"transaction": {"message": {"instructions": []}},
           "meta": {"logMessages": ["Program log: Instruction: CreateV2"], "innerInstructions": [
               {"index": 0, "instructions": [{"programId": PUMP_PROGRAM_ID, "accounts": [],
                                              "data": b58encode(EVENT_IX_TAG + create_event())}]}]}}
    lines = stream_guard.create_lines(cpi, MINT)
    assert len(lines) == 1 and lines[0].startswith("Program data: ")
    assert stream_guard.create_lines({"meta": {"logMessages": logs_of(TRADE_EVENT)}}, MINT) == []
    assert stream_guard.create_lines(None, MINT) == []


async def test_gap_fill_ingests_only_missed_launches_from_chain(redis):
    now = time.time()
    await announce(redis, MINT, "sigA", 30, now)  # missed by the stream
    await announce(redis, OTHER, "sigB", 30, now)  # the stream has it
    await pump_stream.ingest_logs(redis, logs_of(create_event(OTHER)), "sigB", datetime.now(timezone.utc))
    await announce(redis, "9" * 43 + "p", "sigC", 5, now)  # too young: the stream may still deliver it
    hb_before = await redis.get(pump_stream.HEARTBEAT)
    rpc = FakeRpc({"sigA": {"meta": {"logMessages": logs_of(create_event())}}})
    counts = await stream_guard.gap_fill(redis, rpc, now)
    assert counts == {"checked": 1, "filled": 1, "no_create_event": 0, "errors": 0} and rpc.calls == [("getTransaction", "sigA")]
    meta = await redis.hgetall(pump_stream.meta_key(MINT))
    assert meta["name"] == "Moon" and meta["signature"] == "sigA"
    stats = await pump_stream.stats(redis)
    assert stats["create"] == 1 and stats["create_gap_filled"] == 1  # stream count stays the stream's own
    assert await redis.get(pump_stream.HEARTBEAT) == hb_before  # a gap fill is not a live-stream heartbeat
    # never looked up twice
    assert (await stream_guard.gap_fill(redis, rpc, now))["checked"] == 0 and len(rpc.calls) == 1


async def test_gap_fill_adds_nothing_without_a_create_event_and_survives_rpc_errors(redis):
    now = time.time()
    await announce(redis, MINT, "sigA", 30, now)
    await announce(redis, OTHER, "boom", 30, now)
    rpc = FakeRpc({"sigA": {"meta": {"logMessages": logs_of(TRADE_EVENT)}}})
    counts = await stream_guard.gap_fill(redis, rpc, now)
    assert counts["checked"] == 2 and counts["filled"] == 0 and counts["no_create_event"] == 1 and counts["errors"] == 1
    assert not await redis.exists(pump_stream.meta_key(MINT))


async def test_stream_problem_flags_silence_and_low_coverage(redis):
    now = time.time()
    assert await stream_guard.stream_problem(redis, now) is None  # nothing known yet: no alarm
    await redis.set(pump_stream.HEARTBEAT, datetime.fromtimestamp(now - 120, timezone.utc).isoformat())
    assert "no pump.fun event received" in await stream_guard.stream_problem(redis, now)
    await redis.set(pump_stream.HEARTBEAT, datetime.fromtimestamp(now - 1, timezone.utc).isoformat())
    for i in range(12):  # 12 announced launches, the stream delivered 3
        mint = "ABCDEFGHJKLM"[i] + "7" * 42 + "p"
        await announce(redis, mint, f"s{i}", 120, now)
        if i < 3:
            await pump_stream.ingest_logs(redis, logs_of(create_event(mint)), f"s{i}", datetime.now(timezone.utc))
        elif i < 6:
            await redis.set(f"{stream_guard.GAP_DONE}:{mint}", "1")  # filled from chain: still missed by the stream
    cov = await stream_guard.stream_coverage(redis, now)
    assert cov == {"announced": 12, "delivered_by_stream": 3, "coverage": 0.25}
    assert "delivered 3 of 12" in await stream_guard.stream_problem(redis, now)


async def test_request_reconnect_closes_the_current_connection():
    client = SolanaWsClient(url_provider=lambda: "wss://x", subscriptions=[], on_message=None)
    assert client.request_reconnect() is False  # not connected

    class Ws:
        closed = False

        async def close(self):
            self.closed = True

    client._ws = Ws()
    assert client.request_reconnect() is True
    await asyncio.sleep(0)
    assert client._ws.closed
