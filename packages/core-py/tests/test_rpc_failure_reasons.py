"""When every RPC endpoint fails, the error says why for each one (e.g. a
rate limit), never contains the endpoint URL (keys live in it), and is sent
to Telegram."""

import httpx
import pytest

from yonixalpha_core.solana import rpc as rpc_mod
from yonixalpha_core.solana.rpc import RpcAllEndpointsFailedError, RpcManager

PRIMARY = "https://primary.example/?api-key=SECRET1"
BACKUP = "https://backup.example/SECRET2"


def _manager(handler) -> RpcManager:
    return RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                             primary_url=PRIMARY, backup_url=BACKUP)


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def fake_alert(service, event, detail=None, **kw):
        sent.append((service, event, detail))
        return True

    monkeypatch.setattr(rpc_mod, "alert_error", fake_alert)
    return sent


async def test_reasons_per_endpoint_without_urls(alerts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            return httpx.Response(429, text="Too Many Requests")
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(RpcAllEndpointsFailedError) as ei:
        await _manager(handler).call("getAccountInfo", ["x"])
    msg = str(ei.value)
    assert "method=getAccountInfo" in msg and "primary: HTTP 429" in msg and "backup: timeout" in msg
    assert "SECRET" not in msg
    assert alerts == [("solana-rpc", "rpc.all_endpoints_failed", "method=getAccountInfo (primary: HTTP 429; backup: timeout)")]


async def test_json_rpc_error_is_reported_redacted(alerts):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32005, "message": f"limit on {PRIMARY}"}})

    with pytest.raises(RpcAllEndpointsFailedError) as ei:
        await _manager(handler).call("getTokenLargestAccounts", ["x"])
    msg = str(ei.value)
    assert "-32005" in msg and "SECRET" not in msg and "https://primary.example/…" in msg


async def test_success_sends_nothing(alerts):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": None}})

    assert await _manager(handler).call("getAccountInfo", ["x"]) == {"value": None}
    assert alerts == []


async def test_up_to_three_backups_are_tried_in_order(alerts):
    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "b3.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 42})
        return httpx.Response(429)

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=PRIMARY,
                            backup_url="https://b1.example/k1", extra_backup_urls=[None, "https://b2.example/k2", "", "https://b3.example/k3"])
    assert [e.label for e in rpc.endpoints] == ["primary", "backup", "backup2", "backup3"]
    assert await rpc.call("getSlot") == 42
    assert tried == ["primary.example", "b1.example", "b2.example", "b3.example"]
    assert alerts == []
