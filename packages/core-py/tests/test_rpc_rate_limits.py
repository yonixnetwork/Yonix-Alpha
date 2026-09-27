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


async def test_6_rate_limited_provider_cools_down_and_the_backup_serves():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(429)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 7})

    rpc = _manager(handler)
    assert await rpc.call("getSlot") == 7 and rpc.active_label == "backup"
    hits.clear()
    assert await rpc.call("getBalance", ["w"]) == 7
    assert hits == ["backup.example"]  # the rate-limited primary gets no new burst during its cooldown
    assert rpc.health_snapshot()[0]["rate_limited"] is True


async def test_7_provider_refusing_basic_requests_is_authentication_failed():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        if request.url.host == "backup.example":
            return httpx.Response(403)
        return httpx.Response(429)

    rpc = _manager(handler)
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getBalance", ["w"])
    snap = {e["label"]: e for e in rpc.health_snapshot()}
    assert snap["backup"]["auth_failed"] is True and "AUTHENTICATION_FAILED" in snap["backup"]["last_error"]
    hits.clear()
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getAccountInfo", ["x"])
    assert "backup.example" not in hits  # skipped for every method until the key/URL is fixed


async def test_7b_a_success_clears_the_authentication_failure():
    state = {"code": 403}

    def handler(request):
        return httpx.Response(state["code"], json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=PRIMARY)
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getSlot")
    assert rpc.health_snapshot()[0]["auth_failed"] is True
    state["code"] = 200
    assert await rpc.call("getSlot") == 1  # the only endpoint: tried anyway, and its success clears the state
    assert rpc.health_snapshot()[0]["auth_failed"] is False


async def test_8_every_provider_failing_is_one_controlled_error_and_routing_is_recorded():
    def handler(request):
        if request.url.host == "primary.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 5})
        return httpx.Response(503)

    rpc = _manager(handler)
    await rpc.call("getSlot")
    routing = {m["method"]: m for m in rpc.method_snapshot()}
    assert routing["getSlot"]["provider"] == "primary" and routing["getSlot"]["ok"] == 1

    def down(request):
        return httpx.Response(503)

    dead = _manager(down)
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError) as e:
        await dead.call("getSlot")
    assert "HTTP 503" in str(e.value) and "SECRET" not in str(e.value)
    assert {m["method"]: m for m in dead.method_snapshot()}["getSlot"]["fail"] == 2
