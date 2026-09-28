"""RPC compatibility: transaction versions, provider capabilities, Retry-After
and request priority.

Production errors reproduced here:
  getTransaction -> alchemy: "Transaction version (1) is not supported by the
  requesting client" (we declared maxSupportedTransactionVersion 0);
  ankr: "the method getTransaction does not exist/is not available".
"""

import httpx
import pytest

from yonixalpha_core.solana import funding, pumpswap
from yonixalpha_core.solana import rpc as rpc_mod
from yonixalpha_core.solana.live_exec import parse_fill
from yonixalpha_core.solana.rpc import (
    MAX_SUPPORTED_TRANSACTION_VERSION,
    RpcManager,
    RpcMethodUnsupportedError,
    RpcRateLimitedError,
    RpcUnsupportedTransactionVersionError,
    get_transaction_params,
    with_priority,
)
from yonixalpha_core.solana.txversion import UnsupportedTransactionLayout, require_version

PRIMARY = "https://primary.example/?api-key=SECRET1"
ALCHEMY = "https://alchemy.example/v2/SECRET2"
ANKR = "https://ankr.example/solana/SECRET3"
WALLET = "Wa11et1111111111111111111111111111111111111"
MINT = "Mint111111111111111111111111111111111111111"

alerts: list = []


@pytest.fixture(autouse=True)
def capture_alerts(monkeypatch):
    alerts.clear()

    async def fake_alert(service, event, detail=None, **kw):
        alerts.append((event, detail))
        return True
    monkeypatch.setattr(rpc_mod, "alert_error", fake_alert)


def manager(handler, urls=(PRIMARY, ALCHEMY, ANKR)) -> RpcManager:
    return RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=urls[0],
                             backup_url=urls[1] if len(urls) > 1 else None, extra_backup_urls=list(urls[2:]))


def rpc_error(request, code, message):
    import json
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                     "error": {"code": code, "message": message}})


def rpc_result(request, result):
    import json
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": result})


def body(request) -> dict:
    import json
    return json.loads(request.content)


# jsonParsed getTransaction responses in the documented RPC shape.
LEGACY_TX = {
    "slot": 300, "blockTime": 1790000000, "version": "legacy",
    "meta": {"err": None, "fee": 5000, "preBalances": [1_000_000_000, 0], "postBalances": [989_995_000, 0],
             "preTokenBalances": [], "logMessages": ["Program log: x"],
             "postTokenBalances": [{"accountIndex": 1, "mint": MINT, "owner": WALLET,
                                    "uiTokenAmount": {"amount": "12345", "decimals": 6}}]},
    "transaction": {"signatures": ["sig"], "message": {"accountKeys": [
        {"pubkey": WALLET, "signer": True, "writable": True, "source": "transaction"},
        {"pubkey": "Ata1111111111111111111111111111111111111111", "signer": False, "writable": True,
         "source": "transaction"}], "instructions": [], "recentBlockhash": "bh"}},
}
V0_TX = {
    **LEGACY_TX, "version": 0,
    "meta": {**LEGACY_TX["meta"], "loadedAddresses": {"writable": ["Pool111111111111111111111111111111111111111"],
                                                      "readonly": []}},
    "transaction": {"signatures": ["sig"], "message": {**LEGACY_TX["transaction"]["message"],
                                                       "addressTableLookups": [{"accountKey": "Alt1", "writableIndexes": [0],
                                                                                "readonlyIndexes": []}]}},
}


# --- transaction versions ----------------------------------------------------------

def test_every_get_transaction_declares_the_supported_version():
    params = get_transaction_params("sig")
    assert params[1]["maxSupportedTransactionVersion"] == MAX_SUPPORTED_TRANSACTION_VERSION == 1
    assert get_transaction_params("sig", encoding="json")[1]["encoding"] == "json"


async def test_version_1_transaction_is_fetched_instead_of_failing_every_endpoint():
    """The production failure: version 0 declared, a v1 transaction on chain."""
    seen = []

    def handler(request):
        b = body(request)
        seen.append((request.url.host, b["params"][1]["maxSupportedTransactionVersion"]))
        if b["params"][1]["maxSupportedTransactionVersion"] < 1:
            return rpc_error(request, -32015, "Transaction version (1) is not supported by the requesting client. "
                                              "Please try the request again with the following configuration parameter: "
                                              "\"maxSupportedTransactionVersion\": 1")
        return rpc_result(request, {**V0_TX, "version": 1})

    rpc = manager(handler)
    tx = await rpc.call("getTransaction", get_transaction_params("sig"))
    assert tx["version"] == 1 and seen == [("primary.example", 1)]
    assert rpc.endpoints[0].tx_versions == {"1": 1}


async def test_unsupported_version_is_not_an_outage_and_is_not_retried_elsewhere():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        return rpc_error(request, -32015, "Transaction version (2) is not supported by the requesting client")

    rpc = manager(handler)
    with pytest.raises(RpcUnsupportedTransactionVersionError):
        await rpc.call("getTransaction", get_transaction_params("sig"))
    assert sent == ["primary.example"]  # every node answers the same: no failover
    e = rpc.endpoints[0]
    assert e.failures == 0 and e.consecutive_failures == 0 and e.capabilities["getTransaction"]["status"] == "SUPPORTED"
    assert alerts == []


async def test_pool_trade_history_skips_a_transaction_newer_than_declared():
    def handler(request):
        b = body(request)
        if b["method"] == "getSignaturesForAddress":
            return rpc_result(request, [{"signature": "s1", "err": None}, {"signature": "s2", "err": None}])
        if b["params"][0] == "s1":
            return rpc_error(request, -32015, "Transaction version (2) is not supported by the requesting client")
        return rpc_result(request, {**V0_TX, "meta": {**V0_TX["meta"], "logMessages": []}})

    trades = await pumpswap.recent_pool_trades(manager(handler), None, "Pool111111111111111111111111111111111111111")
    assert trades == []  # s1 skipped, s2 has no trade events; nothing crashed


async def test_funding_lookup_declares_version_1():
    params = []

    def handler(request):
        b = body(request)
        if b["method"] == "getSignaturesForAddress":
            return rpc_result(request, [{"signature": "old", "err": None}])
        params.append(b["params"][1])
        return rpc_result(request, None)

    await funding.wallet_funder(manager(handler), None, "w1")
    assert params and params[0]["maxSupportedTransactionVersion"] == 1


def test_fill_parser_reads_legacy_and_v0_and_refuses_other_layouts():
    f = parse_fill(LEGACY_TX, WALLET, MINT)
    assert f.token_change_raw == 12345 and f.sol_change_lamports == -10_005_000 and f.fee_lamports == 5000
    f0 = parse_fill(V0_TX, WALLET, MINT)
    assert f0.token_change_raw == 12345
    with pytest.raises(UnsupportedTransactionLayout):
        parse_fill({**V0_TX, "version": 1}, WALLET, MINT)  # we never build v1; its layout is not assumed
    assert require_version(None, ("legacy",)) is None


# --- capabilities ------------------------------------------------------------------

async def test_provider_without_the_method_is_remembered_and_not_asked_again():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        if request.url.host == "ankr.example":
            return rpc_error(request, -32601, "the method getTransaction does not exist/is not available")
        if request.url.host == "primary.example":
            return httpx.Response(429)
        return rpc_result(request, V0_TX)

    rpc = manager(handler, (PRIMARY, ANKR, ALCHEMY))
    assert (await rpc.call("getTransaction", get_transaction_params("s")))["version"] == 0
    assert sent == ["primary.example", "ankr.example", "alchemy.example"]
    ankr = rpc.endpoints[1]
    assert ankr.capabilities["getTransaction"]["status"] == "UNSUPPORTED" and ankr.failures == 0
    sent.clear()
    await rpc.call("getTransaction", get_transaction_params("s"))
    assert "ankr.example" not in sent  # capability remembered
    snap = rpc.health_snapshot()[1]
    assert snap["unsupported_methods"] == ["getTransaction"]


async def test_method_nobody_offers_is_a_capability_gap_without_an_alert():
    def handler(request):
        return rpc_error(request, -32601, "Method not found")

    rpc = manager(handler)
    with pytest.raises(RpcMethodUnsupportedError):
        await rpc.call("getTransaction", get_transaction_params("s"))
    assert alerts == []
    with pytest.raises(RpcMethodUnsupportedError):
        await rpc.call("getTransaction", get_transaction_params("s"))  # nothing sent: every endpoint lacks it


async def test_an_error_about_the_request_does_not_mark_the_method_unsupported():
    def handler(request):
        return rpc_error(request, -32602, "Invalid param: account does not exist")

    rpc = manager(handler, (PRIMARY,))
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getAccountInfo", ["x"])
    assert rpc.endpoints[0].unsupported == {}


# --- Retry-After / priority --------------------------------------------------------

async def test_retry_after_header_sets_the_cooldown():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    rpc = manager(handler, (PRIMARY,))
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await rpc.call("getBalance", ["w"])
    import time
    left = rpc.endpoints[0].rate_limited_until - time.monotonic()
    assert 115 < left <= 120
    assert rpc.endpoints[0].rate_limited_by_method == {"getBalance": 1}


async def test_background_work_is_shed_while_critical_traffic_still_goes_out():
    sent = []

    def handler(request):
        sent.append(body(request)["method"])
        return httpx.Response(429)

    rpc = manager(handler, (PRIMARY,))
    with pytest.raises(RpcRateLimitedError):
        await rpc.call("getSignaturesForAddress", ["w"], priority="background")
    assert sent == ["getSignaturesForAddress"] and alerts == []  # background failures never alert
    with pytest.raises(RpcRateLimitedError):
        await rpc.call("getSignaturesForAddress", ["w"], priority="background")
    assert len(sent) == 1  # shed: not sent while cooling down
    crit = with_priority(rpc, "critical")
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await crit.call("getLatestBlockhash", [])
    assert sent[-1] == "getLatestBlockhash"  # execution is still attempted


async def test_background_concurrency_is_capped_per_endpoint():
    rpc = manager(lambda r: rpc_result(r, 1), (PRIMARY,))
    rpc.endpoints[0].inflight_background = rpc_mod.BACKGROUND_MAX_INFLIGHT
    with pytest.raises(RpcRateLimitedError):
        await rpc.call("getBalance", ["w"], priority="background")
    assert await rpc.call("getBalance", ["w"], priority="critical") == 1


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def mget(self, keys):
        return [self.d.get(k) for k in keys]

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    async def incr(self, k):
        self.d[k] = int(self.d.get(k, 0)) + 1
        return self.d[k]

    async def expire(self, k, s):
        return True


async def test_a_429_seen_by_one_service_is_respected_by_background_work_in_another():
    shared = FakeRedis()
    a = manager(lambda r: httpx.Response(429), (PRIMARY,))
    b_sent = []
    b = manager(lambda r: (b_sent.append(1), rpc_result(r, 1))[1], (PRIMARY,))
    a.shared = b.shared = shared
    with pytest.raises(rpc_mod.RpcAllEndpointsFailedError):
        await a.call("getBalance", ["w"])
    with pytest.raises(RpcRateLimitedError):
        await b.call("getSignaturesForAddress", ["w"], priority="background")
    assert b_sent == []
    assert await b.call("getBalance", ["w"]) == 1  # normal/critical calls are not blocked by the shared flag


async def test_priority_proxy_passes_through_to_test_doubles():
    class Double:
        active_label = "x"

        async def call(self, method, params=None):
            return method

    p = with_priority(Double(), "critical")
    assert await p.call("getSlot") == "getSlot" and p.active_label == "x"


async def test_capability_probe_reports_methods_and_versions_without_sending_anything():
    from yonixalpha_core.solana.rpc_registry import probe_capabilities
    methods = []

    def handler(request):
        b = body(request)
        methods.append(b["method"])
        if b["method"] == "getTransaction":
            return rpc_error(request, -32601, "the method getTransaction does not exist/is not available")
        if b["method"] == "getSignaturesForAddress":
            return rpc_result(request, [{"signature": "s1"}])
        if b["method"] == "getBalance":
            return httpx.Response(429)
        return rpc_result(request, {"value": {"err": None}})

    res = await probe_capabilities(httpx.AsyncClient(transport=httpx.MockTransport(handler)), ANKR)
    m = res["methods"]
    assert m["getTransaction"]["status"] == "UNSUPPORTED" and m["getBalance"]["status"] == "RATE_LIMITED"
    assert m["getLatestBlockhash"]["status"] == "SUPPORTED" and m["simulateTransaction"]["status"] == "SUPPORTED"
    assert m["sendTransaction"]["status"] == "NOT_PROBED" and "sendTransaction" not in methods
    assert res["max_version_declared"] == 1
