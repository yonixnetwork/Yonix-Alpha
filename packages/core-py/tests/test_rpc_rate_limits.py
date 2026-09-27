"""Rate limits and refusals don't turn into a request flood: optional
lookups (early-buyer funding) are not sent while every endpoint is cooling
down after HTTP 429, a provider that keeps rate-limiting rests longer, and a
provider that answers 401/403 for a method is not asked for that method
again for a while (its other methods keep working)."""

import httpx
import pytest

from yonixalpha_core.solana import funding
from yonixalpha_core.solana import rpc as rpc_mod
from yonixalpha_core.solana.rpc import RpcManager, RpcRateLimitedError, call_optional

PRIMARY = "https://primary.example/?api-key=SECRET1"
BACKUP = "https://backup.example/SECRET2"


@pytest.fixture(autouse=True)
def quiet_alerts(monkeypatch):
    async def fake_alert(*a, **kw):
        return True
    monkeypatch.setattr(rpc_mod, "alert_error", fake_alert)


def _manager(handler) -> RpcManager:
    return RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             primary_url=PRIMARY, backup_url=BACKUP)


async def test_optional_lookups_are_not_sent_while_every_endpoint_is_rate_limited():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        return httpx.Response(429)

    rpc = _manager(handler)
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await call_optional(rpc, "getSignaturesForAddress", ["w"])
    assert sent == ["primary.example", "backup.example"] and rpc.cooling_down() is not None
    with pytest.raises(RpcRateLimitedError):
        await call_optional(rpc, "getSignaturesForAddress", ["w"])
    assert len(sent) == 2  # nothing sent during the cooldown
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getBalance", ["w"])  # a call a trade depends on is still attempted
    assert len(sent) == 4


async def test_funding_check_stops_at_the_first_rate_limit_instead_of_trying_every_wallet():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        return httpx.Response(429)

    res = await funding.funding_links(_manager(handler), None, [f"w{i}" for i in range(6)], "creator", 6)
    assert res["checked"] == 0 and len(sent) == 2 and "stopped after 0 wallets" in res["errors"][0]


async def test_repeated_429_backs_off_longer_and_a_success_resets_it():
    status = {"code": 429}

    def handler(request):
        return httpx.Response(status["code"], json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=PRIMARY)
    waits = []
    for _ in range(4):
        with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
            await rpc.call("getSlot")
        waits.append(round(rpc.cooling_down()))
    assert waits == [30, 60, 120, 240]
    status["code"] = 200
    assert await rpc.call("getSlot") == 1 and rpc.endpoints[0].rate_limit_streak == 0


async def test_403_for_a_method_skips_that_provider_for_that_method_only():
    sent = []

    def handler(request):
        body = request.read().decode()
        sent.append((request.url.host, "getSignaturesForAddress" in body))
        if request.url.host == "backup.example" and "getSignaturesForAddress" in body:
            return httpx.Response(403)
        if request.url.host == "primary.example":
            return httpx.Response(429)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

    rpc = _manager(handler)
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getSignaturesForAddress", ["w"])
    assert rpc.health_snapshot()[1]["forbidden_methods"] == ["getSignaturesForAddress"]
    sent.clear()
    assert await rpc.call("getSlot") == []  # the backup still serves other methods
    assert sent == [("backup.example", False)]
