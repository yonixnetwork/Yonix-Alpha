"""RPC request economy (2026-10-13): identical concurrent reads share one
request, sends are never coalesced, token reads are reused only while still
fresher than the gate's data-age limit, and usage is counted in memory and
flushed as aggregated hourly counters (call counts, not credits)."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from redis.asyncio import Redis

from yonixalpha_core import rpc_usage
from yonixalpha_core.solana import assembler
from yonixalpha_core.solana import rpc as rpc_mod
from yonixalpha_core.solana.rpc import RpcManager

PRIMARY = "https://primary.example/?api-key=SECRET1"


@pytest.fixture(autouse=True)
def quiet_alerts(monkeypatch):
    async def fake_alert(*a, **kw):
        return True
    monkeypatch.setattr(rpc_mod, "alert_error", fake_alert)


def _slow_manager(sent: list) -> RpcManager:
    async def handler(request):
        sent.append(request.content)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": {"n": len(sent)}}})
    return RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=PRIMARY)


async def test_identical_concurrent_reads_share_one_request_and_get_their_own_copy():
    sent: list = []
    rpc = _slow_manager(sent)
    results = await asyncio.gather(*[rpc.call("getAccountInfo", ["MINT", {"encoding": "base64"}]) for _ in range(5)])
    assert len(sent) == 1 and all(r == {"value": {"n": 1}} for r in results)
    results[0]["value"]["n"] = 99  # one caller mutating its result cannot change another's
    assert results[1]["value"]["n"] == 1
    assert rpc.usage["*|getAccountInfo|normal|coalesced"] == 4 and rpc.usage["primary|getAccountInfo|normal|ok"] == 1
    # Different params or priority are different requests; afterwards nothing stays in flight.
    await asyncio.gather(rpc.call("getAccountInfo", ["A"]), rpc.call("getAccountInfo", ["B"]),
                         rpc.call("getAccountInfo", ["A"], priority="critical"))
    assert len(sent) == 4 and rpc._inflight_calls == {}


async def test_sends_are_never_coalesced():
    sent: list = []
    rpc = _slow_manager(sent)
    await asyncio.gather(*[rpc.call("sendTransaction", ["tx"]) for _ in range(3)])
    assert len(sent) == 3


async def test_a_failure_reaches_every_waiting_caller_and_is_not_kept():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(500)

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=PRIMARY)
    out = await asyncio.gather(*[rpc.call("getSlot") for _ in range(3)], return_exceptions=True)
    assert calls == 1 and all(isinstance(e, rpc_mod.RpcAllEndpointsFailedError) for e in out)
    assert rpc.usage["primary|getSlot|normal|failed"] == 1
    await asyncio.gather(rpc.call("getSlot"), return_exceptions=True)
    assert calls == 2  # a failed result is never reused


class CountingRpc:
    def __init__(self):
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append(method)
        info = {"mintAuthority": None, "freezeAuthority": None, "decimals": 6, "supply": "1000"}
        return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"type": "mint", "info": info}}}}


async def test_mint_reads_are_reused_only_while_fresher_than_the_data_age_limit(monkeypatch):
    rpc, now = CountingRpc(), datetime.now(timezone.utc)
    clock = [1000.0]
    monkeypatch.setattr(assembler.time, "monotonic", lambda: clock[0])
    a, _ = await assembler.cached_mint(rpc, "MINT", now, max_data_age_seconds=60)
    b, _ = await assembler.cached_mint(rpc, "MINT", now, max_data_age_seconds=60)
    assert len(rpc.calls) == 1 and a is b and b.observation.observed_at == now  # original observation time kept
    clock[0] += assembler.MINT_CACHE_SECONDS + 1
    await assembler.cached_mint(rpc, "MINT", now, max_data_age_seconds=60)
    assert len(rpc.calls) == 2  # expired: fetched again
    # A strict data-age limit disables reuse (never older than the gate accepts).
    other = CountingRpc()
    for _ in range(3):
        await assembler.cached_mint(other, "MINT", now, max_data_age_seconds=10)
    assert len(other.calls) == 3


async def test_usage_is_flushed_as_hourly_deltas():
    redis = Redis(decode_responses=True)
    now = datetime(2026, 10, 13, 12, 5, tzinfo=timezone.utc)
    key = rpc_usage.hour_key(now)
    await redis.delete(key)
    counts, flushed = {"primary|getSlot|normal|ok": 3, "primary|getSlot|latency_ms": 12.5}, {}
    assert await rpc_usage.flush(redis, "svc", counts, flushed, now) == 2
    counts["primary|getSlot|normal|ok"] = 5
    assert await rpc_usage.flush(redis, "svc", counts, flushed, now) == 1  # only the delta
    got = await redis.hgetall(key)
    assert got["svc|primary|getSlot|normal|ok"] == "5" and float(got["svc|primary|getSlot|latency_ms"]) == 12.5
    assert 0 < await redis.ttl(key) <= rpc_usage.TTL_SECONDS
    assert (await rpc_usage.read_usage(redis, hours=1, now=now))["svc|primary|getSlot|normal|ok"] == 5
    await redis.delete(key)
    await redis.aclose()


def test_usage_tool_summarizes_calls_without_urls():
    from yonixalpha_core.tools.rpc_usage import summarize

    rows, cache, totals = summarize({
        "decision-engine|helius|getAccountInfo|normal|ok": 10, "decision-engine|helius|getAccountInfo|normal|http_429": 2,
        "decision-engine|helius|getAccountInfo|latency_ms": 240.0, "decision-engine|*|getAccountInfo|normal|coalesced": 3,
        "decision-engine|*|getAccountInfo:mint|cache|hit": 5, "decision-engine|*|getAccountInfo:mint|cache|miss": 5,
    })
    helius = next(r for r in rows if r[1] == "helius")
    assert helius[:5] == ("decision-engine", "helius", "getAccountInfo", 12, 10) and helius[7] == 2 and helius[11] == 20.0
    assert cache == [("decision-engine|getAccountInfo:mint", 5, 5)] and totals["ok"] == 10
