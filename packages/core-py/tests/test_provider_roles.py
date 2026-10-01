"""Provider roles and plan health (master §48-53): a request prefers the
endpoints holding its method's role, falls back to any usable endpoint
(counted) rather than stalling, and plan health turns observed limits into
UPGRADE_REQUIRED findings with provider, plan, capability, observation and
recommendation."""
import json

import httpx

from yonixalpha_core import provider_roles as pr
from yonixalpha_core.chains.evm.rpc import EvmRpc
from yonixalpha_core.solana.rpc import RpcManager

A, B = "https://logs.example/k1", "https://quotes.example/k2"


def evm_rpc(hits: list[str], cooldown_b: bool = False) -> EvmRpc:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        hits.append(f"{req.url.host}:{body['method']}")
        result = hex(56) if body["method"] == "eth_chainId" else ([] if body["method"] == "eth_getLogs" else "0x01")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    rpc = EvmRpc("bsc", 56, [A, B], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rpc.replace_urls([A, B], {A: ("DISCOVERY", "HISTORICAL_DATA"), B: ("MARKET_DATA",)})
    return rpc


async def test_evm_requests_go_to_the_provider_holding_their_role():
    hits: list[str] = []
    rpc = evm_rpc(hits)
    await rpc.get_logs("0x" + "1" * 40, [], 1, 10)
    await rpc.eth_call("0x" + "2" * 40, "0x")  # latest: MARKET_DATA
    await rpc.eth_call("0x" + "2" * 40, "0x", block=hex(5))  # past block: HISTORICAL_DATA
    calls = [h for h in hits if not h.endswith("eth_chainId")]
    assert calls == ["logs.example:eth_getLogs", "quotes.example:eth_call", "logs.example:eth_call"]
    # nobody holds WALLET_DATA: every endpoint may serve it, counted as a role fallback
    await rpc.get_balance("0x" + "3" * 40)
    assert rpc.role_fallbacks == {"WALLET_DATA": 1}
    # the role holder is cooling down: the request still goes out (to the other endpoint)
    rpc.endpoints[1].cooldown_until = 1e18
    hits.clear()
    await rpc.eth_call("0x" + "2" * 40, "0x")
    assert [h for h in hits if not h.endswith("eth_chainId")] == ["logs.example:eth_call"]
    assert rpc.role_fallbacks["MARKET_DATA"] == 1
    h = rpc.health()
    assert h["endpoints"][0]["roles"] == ["DISCOVERY", "HISTORICAL_DATA"] and h["role_fallbacks"]["WALLET_DATA"] == 1


async def test_without_roles_routing_is_unchanged():
    hits: list[str] = []
    rpc = evm_rpc(hits)
    rpc.replace_urls([A, B])  # roles cleared
    await rpc.eth_call("0x" + "2" * 40, "0x")
    await rpc.get_logs("0x" + "1" * 40, [], 1, 10)
    assert {h for h in hits if not h.endswith("eth_chainId")} == {"logs.example:eth_call", "logs.example:eth_getLogs"}
    assert rpc.role_fallbacks == {}


async def test_solana_execution_role_is_preferred_for_send():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(f"{req.url.host}:{body['method']}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 1})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url="https://data.example")
    rpc.replace_endpoints([{"url": "https://data.example", "label": "data", "roles": ["MARKET_DATA"]},
                           {"url": "https://send.example", "label": "send", "roles": ["EXECUTION", "CONFIRMATION"]}])
    await rpc.call("getAccountInfo", ["x"])
    await rpc.call("sendTransaction", ["tx"], priority="critical")
    await rpc.call("getSignatureStatuses", [["s"]])
    assert seen == ["data.example:getAccountInfo", "send.example:sendTransaction", "send.example:getSignatureStatuses"]
    assert [e["roles"] for e in rpc.health_snapshot()] == [["MARKET_DATA"], ["EXECUTION", "CONFIRMATION"]]


def test_roles_and_method_mapping():
    assert pr.parse_roles(["discovery", "EXECUTION", "discovery"]) == (("DISCOVERY", "EXECUTION"), [])
    assert pr.parse_roles(["everything"])[1]
    assert pr.solana_role("sendTransaction") == "EXECUTION" and pr.solana_role("getAccountInfo") == "MARKET_DATA"
    assert pr.evm_role("eth_getTransactionCount", ["0xab", "latest"]) == "WALLET_DATA"
    assert pr.evm_role("eth_getTransactionCount", ["0xab", "0x10"]) == "HISTORICAL_DATA"
    assert pr.evm_role("eth_getTransactionReceipt", ["0x1"]) == "CONFIRMATION"


def test_solana_plan_findings_from_observed_limits():
    providers = [
        {"name": "Helius", "label": "db:Helius", "enabled": True, "provider_type": "helius", "plan": "Free",
         "refused_methods": ["sendTransaction"], "capabilities": {"getProgramAccounts": {"status": "FORBIDDEN"},
                                                                 "transactionSubscribe": {"status": "UNSUPPORTED"}},
         "successes": 900, "failures": 100, "rate_limited_count": 80, "rpc_url": "https://mainnet.helius-rpc.com/…"},
        {"name": "Chainstack", "label": "db:Chainstack", "enabled": True, "provider_type": "chainstack",
         "auth_failed_now": ["data-solana"], "successes": 0, "failures": 0},
    ]
    f = pr.solana_findings(providers)
    caps = {(x["provider"], x["capability"]): x for x in f}
    send = caps[("Helius", "sendTransaction")]
    assert send["severity"] == "UPGRADE_REQUIRED" and send["current_plan"] == "Free"
    assert "AUTOMATIC SELL LATENCY MAY BE LIMITED BY CURRENT RPC PLAN" in send["impact"]
    assert ("Helius", "getProgramAccounts") in caps and ("Helius", "throughput") in caps
    assert ("Helius", "transactionSubscribe (enhanced WebSocket)") in caps
    assert caps[("Chainstack", "authentication")]["severity"] == "CONFIGURATION"
    assert not any(x["capability"] == "production RPC" for x in f)  # keyed providers are enabled


def test_evm_plan_findings_public_only_and_small_log_span():
    rows = [{"label": "public:bsc:0", "name": "public #1", "url": "https://bsc-rpc.publicnode.com", "source": "public",
             "enabled": True}]
    live = {"endpoints": [{"url": "https://bsc-rpc.publicnode.com", "ok": 300, "errors": 0, "rate_limited": 3,
                           "logs_span": 50, "unsupported_methods": []}]}
    f = pr.evm_findings("bsc", rows, live)
    caps = {x["capability"]: x for x in f}
    assert caps["production RPC"]["severity"] == "UPGRADE_REQUIRED"
    assert "50 blocks" in caps["eth_getLogs block range"]["observed"]
    assert "throughput" not in caps  # 1% 429s is not a plan limit
    assert caps["WebSocket / pending transactions"]["severity"] == "INFO"


def test_stream_findings_only_from_observed_states():
    """Master §9 / §13 / §53: a refused or hash-only pending stream is an
    UPGRADE REQUIRED finding; the sequencer feed reports a wrong chain, a
    delayed-feed fallback and repeated failures; a healthy or unreported
    stream raises nothing. Robinhood needs no provider WSS (the feed is public)."""
    assert pr.stream_findings("bsc", {}) == []
    assert pr.stream_findings("bsc", {"pending_tx": {"state": "CONNECTED", "url": "wss://x"}}) == []
    refused = pr.stream_findings("bsc", {"pending_tx": {"state": "REFUSED", "url": "wss://bsc.example/***",
                                                        "detail": "method not allowed"}})
    assert refused[0]["severity"] == "UPGRADE_REQUIRED" and refused[0]["capability"] == "mempool / pending transactions"
    assert "confirmed" in refused[0]["impact"] and refused[0]["evidence"]["detail"] == "method not allowed"
    limited = pr.stream_findings("bsc", {"pending_tx": {"state": "LIMITED"}})
    assert "hashes only" in limited[0]["observed"]
    wrong = pr.stream_findings("robinhood", {"sequencer_feed": {"state": "WRONG_CHAIN", "last_error": "feed is for chain 42161"}})
    assert wrong[0]["severity"] == "CONFIGURATION" and "42161" in wrong[0]["observed"]
    fb = pr.stream_findings("robinhood", {"sequencer_feed": {"state": "CONNECTED", "fallback_active": True}})
    assert fb[0]["severity"] == "INFO" and "delayed feed" in fb[0]["observed"]
    assert pr.stream_findings("robinhood", {"sequencer_feed": {"state": "RECONNECTING", "failures_in_row": 1}}) == []
    down = pr.stream_findings("robinhood", {"sequencer_feed": {"state": "RECONNECTING", "failures_in_row": 4,
                                                                "last_error": "TimeoutError: x"}})
    assert down[0]["severity"] == "CONFIGURATION" and "4 failed connections in a row" in down[0]["observed"]
    rows = [{"label": "public:robinhood:0", "name": "public #1", "url": "https://rpc.example", "source": "public", "enabled": True}]
    assert not any(f["capability"] == "WebSocket / pending transactions" for f in pr.evm_findings("robinhood", rows, None))


def test_feed_signature_failures_are_reported():
    ok = pr.stream_findings("robinhood", {"sequencer_feed": {"state": "CONNECTED", "messages": 500, "unverified": 3}})
    assert ok == []  # a few forged / odd frames among accepted ones are dropped, not a configuration problem
    bad = pr.stream_findings("robinhood", {"sequencer_feed": {"state": "CONNECTED", "messages": 0, "unverified": 40}})
    assert bad[0]["capability"] == "sequencer feed signature" and "40 messages" in bad[0]["observed"]
