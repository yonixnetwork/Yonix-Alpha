"""EVM layer: ABI codec, RPC failover, and every launchpad adapter against a
fake JSON-RPC node (httpx.MockTransport). No network: these prove the
decoding / quoting logic, not that the real contracts behave this way
(that is what tools/launchpad_verify records on the server)."""

import json
from decimal import Decimal

import httpx
import pytest

from yonixalpha_core.chains.base import LaunchpadAdapter
from yonixalpha_core.chains.evm import EVM_LAUNCHPADS, adapter_for, dex
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, decode_output, encode_call, event, selector
from yonixalpha_core.chains.evm.flap import Flap
from yonixalpha_core.chains.evm.fourmeme import EVENTS as FOUR_EVENTS
from yonixalpha_core.chains.evm.fourmeme import FourMeme
from yonixalpha_core.chains.evm.odyssey import CURVE_EVENTS, OdysseyCurve
from yonixalpha_core.chains.evm.pons import V2_EVENTS, PonsV2, curve_amount_out
from yonixalpha_core.chains.evm.rpc import EvmRpc, EvmRpcError, EvmRpcUnavailableError
from yonixalpha_core.chains.registry import BSC_PANCAKE_V2, LAUNCHPADS, ROBINHOOD_UNISWAP_V3, ROBINHOOD_WETH
from yonixalpha_core.testing.evm_node import TX, Node, enc, log_of, rpc_for

TOKEN = "0x1111111111111111111111111111111111111111"
TRADER = "0x2222222222222222222222222222222222222222"
CURVE = "0x3333333333333333333333333333333333333333"
POOL = "0x4444444444444444444444444444444444444444"
FOREIGN = "0x5555555555555555555555555555555555555555"


# --- ABI ------------------------------------------------------------------------------------------

def test_abi_topics_match_known_constants_and_logs_round_trip():
    transfer = event("Transfer", ("from", "address", True), ("to", "address", True), ("value", "uint256"))
    assert transfer.topic == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    # Uniswap V3 pool Swap (int256 / uint160 / int24 canonicalisation)
    assert dex.V3_SWAP.topic == "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
    assert selector("balanceOf(address)").hex() == "70a08231"
    log = transfer.encode_log({"from": TOKEN, "to": TRADER, "value": 5}, TOKEN)
    assert transfer.decode(log) == {"from": TOKEN, "to": TRADER, "value": 5}
    data = encode_call("quoteExactInput((address,address,uint256))", (ZERO_ADDRESS, TOKEN, 7))
    assert data.startswith("0x" + selector("quoteExactInput((address,address,uint256))").hex())
    assert decode_output(["uint256"], enc(["uint256"], [42])) == (42,)
    with pytest.raises(ValueError):
        transfer.decode({"topics": [transfer.topic], "data": "0x"})  # missing indexed topics


# --- RPC ------------------------------------------------------------------------------------------

async def test_rpc_fails_over_on_429_checks_chain_id_and_never_leaks_urls():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        body = json.loads(req.content)
        if req.url.host == "a.example":
            return httpx.Response(429, headers={"retry-after": "30"})
        if req.url.host == "wrong.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(1)})
        res = hex(56) if body["method"] == "eth_chainId" else hex(123)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": res})

    rpc = EvmRpc("bsc", 56, ["https://a.example/k1-SECRET", "https://wrong.example/k2-SECRET",
                             "https://b.example/k3-SECRET"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.block_number() == 123
    h = rpc.health()
    states = {e["url"]: e["state"] for e in h["endpoints"]}
    assert states == {"https://a.example/…": "COOLDOWN", "https://wrong.example/…": "WRONG_CHAIN",
                      "https://b.example/…": "OK"}
    assert h["endpoints"][0]["cooldown_s"] > 25  # Retry-After honoured
    assert "SECRET" not in json.dumps(h)
    seen.clear()
    assert await rpc.block_number() == 123
    assert seen == ["b.example"]  # cooling-down and wrong-chain endpoints are skipped


async def test_rpc_revert_is_not_an_endpoint_failure_and_outage_is_explicit():
    node = Node(56)
    rpc = rpc_for(node)
    with pytest.raises(EvmRpcError):
        await rpc.eth_call(TOKEN, "0x12345678")
    assert rpc.health()["endpoints"][0]["state"] == "OK"
    wrong = EvmRpc("bsc", 56, ["https://x.example"], client=httpx.AsyncClient(transport=Node(97).transport()))
    with pytest.raises(EvmRpcUnavailableError):
        await wrong.block_number()
    with pytest.raises(EvmRpcUnavailableError, match="wrong chain"):
        await wrong.block_number()


async def test_get_logs_halves_a_range_the_node_refuses():
    spans = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(56)})
        f = body["params"][0]
        span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        spans.append(span)
        if span > 250:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32005, "message": "block range too large"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": []})

    rpc = EvmRpc("bsc", 56, ["https://n.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.get_logs([TOKEN], [[]], 0, 999, max_span=1000) == []
    assert spans[:3] == [1000, 500, 250] and sum(s for s in spans if s <= 250) == 1000



async def test_node_refusing_logs_at_any_range_is_skipped_for_the_next_endpoint():
    """bsc-dataseed.binance.org answers every eth_getLogs with "limit exceeded",
    even for one block (seen on the server). It must not end discovery: the
    node is marked as not serving logs and the next endpoint is asked."""
    asked = []

    def handler(req):
        body = json.loads(req.content)
        rid = body["id"]
        if body["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": hex(56)})
        asked.append(req.url.host)
        if req.url.host == "dataseed.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                             "error": {"code": -32005, "message": "limit exceeded"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": [{"logIndex": "0x0"}]})

    rpc = EvmRpc("bsc", 56, ["https://dataseed.example", "https://publicnode.example"],
                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.get_logs([TOKEN], [[]], 0, 99, max_span=100) == [{"logIndex": "0x0"}]
    ep = rpc.health()["endpoints"][0]
    assert ep["unsupported_methods"] == ["eth_getLogs"] and "one block" in ep["last_error"]
    asked.clear()
    assert await rpc.get_logs([TOKEN], [[]], 100, 199, max_span=100) == [{"logIndex": "0x0"}]
    assert asked == ["publicnode.example"]


async def test_no_endpoint_serving_logs_is_an_explicit_outage():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(56)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"code": -32005, "message": "limit exceeded"}})

    rpc = EvmRpc("bsc", 56, ["https://only.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(EvmRpcUnavailableError, match="eth_getLogs"):
        await rpc.get_logs([TOKEN], [[]], 0, 9, max_span=10)


def _ok(body, result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


async def test_a_short_429_on_the_only_endpoint_is_waited_out_and_paces_later_requests():
    """Robinhood Chain has one public RPC and it rate-limits eth_getLogs (seen
    on the server). One 429 used to fail every launchpad of the pass with
    "all cooling down"; now the call waits the short cooldown and the
    endpoint is asked more slowly afterwards."""
    calls = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(4663))
        calls.append(body["method"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.2"})
        return _ok(body, [])

    rpc = EvmRpc("robinhood", 4663, ["https://rh.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.get_logs([TOKEN], [[]], 0, 9, max_span=10) == []
    assert await rpc.get_logs([TOKEN], [[]], 10, 19, max_span=10) == []
    assert calls == ["eth_getLogs"] * 3
    ep = rpc.health()["endpoints"][0]
    assert ep["rate_limited"] == 1 and ep["min_gap_s"] > 0


async def test_a_long_429_is_still_an_immediate_explicit_outage():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(4663))
        return httpx.Response(429, headers={"retry-after": "60"})

    rpc = EvmRpc("robinhood", 4663, ["https://rh.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(EvmRpcUnavailableError, match="HTTP 429"):
        await rpc.block_number()
    with pytest.raises(EvmRpcUnavailableError, match="cooling down"):
        await rpc.block_number()


async def test_http_403_for_a_large_log_range_is_halved_not_a_dead_node():
    """bsc-rpc.publicnode.com answered HTTP 403 to the service's 2000-block
    eth_getLogs, yet served 10 / 100 / 1000-block requests from
    tools/evm_rpc_probe (seen on the server). A 403 for logs from a node that
    answers other methods is first treated as "range too large"."""
    spans = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        if body["method"] == "eth_blockNumber":
            return _ok(body, hex(7))
        f = body["params"][0]
        span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        spans.append(span)
        return httpx.Response(403) if span > 1000 else _ok(body, [])

    rpc = EvmRpc("bsc", 56, ["https://publicnode.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.block_number() == 7
    assert await rpc.get_logs([TOKEN], [[]], 0, 3999, max_span=2000) == []
    assert spans[:2] == [2000, 1000] and sum(s for s in spans if s <= 1000) == 4000
    ep = rpc.health()["endpoints"][0]
    assert ep["state"] == "OK" and ep["unsupported_methods"] == []


async def test_http_403_for_logs_at_any_range_skips_only_logs_on_that_node_and_expires():
    asked = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        asked.append((req.url.host, body["method"]))
        if req.url.host == "publicnode.example" and body["method"] == "eth_getLogs":
            return httpx.Response(403)
        return _ok(body, hex(7) if body["method"] == "eth_blockNumber" else [])

    rpc = EvmRpc("bsc", 56, ["https://publicnode.example", "https://logs.example"],
                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.block_number() == 7
    assert await rpc.get_logs([TOKEN], [[]], 0, 9, max_span=10) == []
    assert asked[-1] == ("logs.example", "eth_getLogs")
    ep = rpc.health()["endpoints"][0]
    assert ep["state"] == "OK" and ep["unsupported_methods"] == ["eth_getLogs"] and "403" in ep["last_error"]
    asked.clear()
    assert await rpc.block_number() == 7
    assert await rpc.get_logs([TOKEN], [[]], 10, 19, max_span=10) == []
    assert asked == [("publicnode.example", "eth_blockNumber"), ("logs.example", "eth_getLogs")]
    rpc.endpoints[0].unsupported["eth_getLogs"] = 0.0  # the refusal window elapsed
    asked.clear()
    await rpc.get_logs([TOKEN], [[]], 20, 29, max_span=10)
    assert asked[0] == ("publicnode.example", "eth_getLogs")


async def test_a_403_from_a_node_that_never_answered_is_an_endpoint_failure():
    def handler(req):
        return httpx.Response(403)

    rpc = EvmRpc("bsc", 56, ["https://bad-key.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(EvmRpcUnavailableError, match="403"):
        await rpc.get_logs([TOKEN], [[]], 0, 9, max_span=10)
    assert rpc.health()["endpoints"][0]["state"] == "FAILING"


async def test_a_403_from_a_node_that_already_served_logs_is_a_short_cooldown_not_a_30_minute_skip():
    """Seen on the server: publicnode served a burst of log requests, then
    answered 403 even for single blocks for a while. Our client took that as
    "does not serve logs" and skipped logs on it for 30 minutes (BSC's only
    logs endpoint). A node that served logs before is throttling: it cools
    down briefly and is asked again."""
    served, refusals = [], {"left": 3}

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        if body["method"] == "eth_blockNumber":
            return _ok(body, hex(7))
        if served and refusals["left"] > 0:  # throttled after the first answer
            refusals["left"] -= 1
            return httpx.Response(403)
        served.append(body["params"][0]["fromBlock"])
        return _ok(body, [])

    rpc = EvmRpc("bsc", 56, ["https://publicnode.example", "https://dataseed.example"],
                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rpc.endpoints[1].mark_unsupported("eth_getLogs")  # like bsc-dataseed: never serves logs
    assert await rpc.block_number() == 7
    assert await rpc.get_logs([TOKEN], [[]], 0, 7, max_span=4) == []
    ep = rpc.endpoints[0]
    assert ep.unsupported.get("eth_getLogs", 0.0) == 0.0  # never skipped for 30 minutes
    assert len(served) == 2 and ep.logs_served == 2 and refusals["left"] == 0


# --- Four.meme ------------------------------------------------------------------------------------

def four_info(quote=ZERO_ADDRESS, liquidity_added=False):
    return enc(["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256",
                "uint256", "uint256", "bool"],
               [2, LAUNCHPADS["fourmeme"].contracts["manager_v2"], quote, 5 * 10 ** 9, 100, 0, 1, 7 * 10 ** 26,
                8 * 10 ** 26, 3 * 10 ** 18, 24 * 10 ** 18, liquidity_added])


async def test_fourmeme_scan_decodes_launch_trade_migration_and_counts_bad_logs():
    node = Node(56)
    mgr = LAUNCHPADS["fourmeme"].contracts["manager_v2"]
    ev = FOUR_EVENTS.by_name
    node.logs = [
        log_of(ev["TokenCreate"], {"creator": TRADER, "token": TOKEN, "requestId": 9, "name": "Moon", "symbol": "MN",
                                   "totalSupply": 10 ** 27, "launchTime": 1, "launchFee": 0}, mgr, 10, 0),
        log_of(ev["TokenPurchase"], {"token": TOKEN, "account": TRADER, "price": 5 * 10 ** 9, "amount": 10 ** 24,
                                     "cost": 10 ** 17, "fee": 10 ** 15, "offers": 1, "funds": 2}, mgr, 11, 3),
        log_of(ev["LiquidityAdded"], {"base": TOKEN, "offers": 1, "quote": ZERO_ADDRESS, "funds": 2}, mgr, 12, 0),
        {**log_of(ev["TokenSale"], {"token": TOKEN, "account": TRADER, "price": 1, "amount": 1, "cost": 1, "fee": 0,
                                    "offers": 0, "funds": 0}, mgr, 13, 0), "data": "0x00"},  # truncated data
        log_of(ev["TokenPurchase"], {"token": TOKEN, "account": TRADER, "price": 1, "amount": 1, "cost": 1, "fee": 0,
                                     "offers": 0, "funds": 0}, FOREIGN, 14, 0),  # not the manager: filtered
    ]
    res = await FourMeme(rpc_for(node)).scan(0, 100)
    assert [x.token for x in res.launches] == [TOKEN] and res.launches[0].symbol == "MN"
    t = res.trades[0]
    assert (t.is_buy, t.token_amount, t.quote_amount, t.fee) == (True, 10 ** 24, 10 ** 17, 10 ** 15)
    assert t.price == Decimal(5 * 10 ** 9) / Decimal(10 ** 18) and t.event_id == f"bsc:{TX}:3"
    assert res.migrations[0]["token"] == TOKEN
    assert len(res.decode_errors) == 1 and len(res.trades) == 1


async def test_fourmeme_quotes_curve_bep20_and_migrated():
    node = Node(56)
    lp = FourMeme(rpc_for(node))
    node.on(lp.helper, "getTokenInfo(address)", four_info())
    node.on(lp.helper, "tryBuy(address,uint256,uint256)",
            enc(["address", "address"] + ["uint256"] * 6, [TOKEN, ZERO_ADDRESS, 12345, 10 ** 17, 10 ** 15, 0, 0, 0]))
    node.on(lp.helper, "trySell(address,uint256)", enc(["address", "address", "uint256", "uint256"],
                                                       [TOKEN, ZERO_ADDRESS, 9 * 10 ** 16, 10 ** 15]))
    q = await lp.quote_buy(TOKEN, 10 ** 17)
    assert q.ok and q.amount_out == 12345 and q.fee == 10 ** 15 and q.exact
    s = await lp.quote_sell(TOKEN, 12345)
    assert s.ok and s.amount_out == 9 * 10 ** 16
    st = await lp.get_token_state(TOKEN)
    assert st.stage == "CURVE" and st.progress == Decimal(3) / Decimal(24) and st.buy_tax_bps == 0
    node.on(lp.helper, "getTokenInfo(address)", four_info(quote=FOREIGN))
    assert not (await lp.quote_buy(TOKEN, 10 ** 17)).ok
    node.on(lp.helper, "getTokenInfo(address)", four_info(liquidity_added=True))
    node.on(BSC_PANCAKE_V2["router"], "getAmountsOut(uint256,address[])", enc(["uint256[]"], [[10 ** 17, 777]]))
    m = await lp.quote_buy(TOKEN, 10 ** 17)
    assert m.ok and m.amount_out == 777 and m.source == "pancakeswap_v2.getAmountsOut"
    assert (await lp.detect_migration(TOKEN))["venue"] == "pancakeswap"


async def test_unavailable_rpc_is_a_failed_quote_never_a_price():
    def down(req):
        return httpx.Response(503)

    lp = FourMeme(EvmRpc("bsc", 56, ["https://down.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(down))))
    q = await lp.quote_buy(TOKEN, 10 ** 17)
    assert not q.ok and q.amount_out is None and q.error.startswith("RPC unavailable")


# --- Flap -----------------------------------------------------------------------------------------

def flap_state(status=1, quote=ZERO_ADDRESS):
    return enc(["(uint8,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256,address,bool,bytes32,uint256,"
                "uint256,address,uint256,uint8,uint8)"],
               [(status, 4 * 10 ** 18, 2 * 10 ** 26, 3 * 10 ** 10, 8, 1, 2, 3, 8 * 10 ** 26, quote, False, b"\0" * 32,
                 100, 300, ZERO_ADDRESS, 0, 0, 0)])


async def test_flap_quotes_state_and_untradable_status():
    node = Node(56)
    lp = Flap(rpc_for(node))
    node.on(lp.portal, "getTokenV8Safe(address)", flap_state())

    def quote(p):
        tin, tout, amt = decode_output(["(address,address,uint256)"], "0x" + p[0]["data"][10:])[0]
        assert p[0]["from"] == dex.SIM_ACCOUNT
        return enc(["uint256"], [amt * 1000 if tin == ZERO_ADDRESS else amt // 1000])

    node.on(lp.portal, "quoteExactInput((address,address,uint256))", quote)
    assert (await lp.quote_buy(TOKEN, 5)).amount_out == 5000
    assert (await lp.quote_sell(TOKEN, 5000)).amount_out == 5
    st = await lp.get_token_state(TOKEN)
    assert (st.stage, st.buy_tax_bps, st.sell_tax_bps, st.progress) == ("CURVE", 100, 300, Decimal("0.25"))
    assert st.extra["extension_enabled"] is False
    node.on(lp.portal, "getTokenV8Safe(address)", flap_state(status=3))
    q = await lp.quote_buy(TOKEN, 5)
    assert not q.ok and "KILLED" in q.error
    node.on(lp.portal, "getTokenV8Safe(address)", flap_state(quote=FOREIGN))
    assert not (await lp.quote_sell(TOKEN, 5)).ok


async def test_flap_graduation_event_from_a_real_bsc_log_is_a_migration():
    """LaunchedToDEX exactly as the BSC Portal emitted it (server, 2026-09-30,
    tx 0xb7aaace8...c2dc): every field in data, no indexed topic."""
    from yonixalpha_core.chains.evm.flap import EVENTS as FLAP_EVENTS

    node = Node(56)
    lp = Flap(rpc_for(node))
    real = ("0x00000000000000000000000041df9249b0c4e34eab6e9591e02d5d7a31547777"
            "00000000000000000000000029bd8aa0d60206e835bf916b7c0ddd5ed2e9e7bb"
            "000000000000000000000000000000000000000000a56fa5b99019a5c8000000"
            "000000000000000000000000000000000000000000000004d71694157ae43235")
    ev = FLAP_EVENTS.by_name["LaunchedToDEX"]
    assert ev.topic == "0x6e4f47630b8745b8cacbd44f42a8a33e7eea7cc08ef22fc7630f4f385784ff7d"
    node.logs = [{"address": lp.portal, "topics": [ev.topic], "data": real, "blockNumber": hex(12),
                  "logIndex": hex(0), "transactionHash": TX}]
    res = await lp.scan(0, 100)
    assert not res.decode_errors and not res.trades
    m = res.migrations[0]
    assert m["token"].lower() == "0x41df9249b0c4e34eab6e9591e02d5d7a31547777"
    assert m["pool"].lower() == "0x29bd8aa0d60206e835bf916b7c0ddd5ed2e9e7bb"
    assert m["tokens_added"] == 200_000_000 * 10 ** 18
    assert m["quote_added"] == 0x4d71694157ae43235 == 89_285_714_282_457_346_613  # ~89.29 BNB
    assert m["evidence"] == "LaunchedToDEX event"


# --- Pons V2 --------------------------------------------------------------------------------------

def pons_setup(node: Node, lp: PonsV2, phase=0, pair=ZERO_ADDRESS):
    node.on(lp.factory, "getLaunchedToken(address)", enc(
        ["(address,address,address,address,address,uint256,uint24,int24,uint16,bool,uint8,uint256,uint256,uint256,bool)"],
        [(TOKEN, CURVE, TRADER, TRADER, pair, 4 * 10 ** 18, 10000, 200, 100, False, phase, 0, 0, 0, True)]))
    node.on(CURVE, "getReserves()", enc(["uint256", "uint256"], [2 * 10 ** 18, 10 ** 27]))
    node.on(CURVE, "feeBps()", enc(["uint256"], [100]))
    node.on(CURVE, "creatorTaxBps()", enc(["uint256"], [100]))
    node.on(CURVE, "sellableTokens()", enc(["uint256"], [8 * 10 ** 26]))
    node.on(CURVE, "graduated()", enc(["bool"], [False]))
    node.on(CURVE, "readyToGraduate()", enc(["bool"], [False]))
    node.on(CURVE, "realQuoteReserve()", enc(["uint256"], [10 ** 18]))


async def test_pons_v2_only_accepts_curves_the_factory_announced():
    node = Node(4663)
    lp = PonsV2(rpc_for(node, "robinhood"))
    ev = V2_EVENTS.by_name
    buy = {"buyer": TRADER, "recipient": TRADER, "quoteIn": 10 ** 16, "tokensOut": 10 ** 22, "fee": 10 ** 14, "tax": 10 ** 14}
    node.logs = [
        log_of(ev["TokenLaunched"], {"token": TOKEN, "curve": CURVE, "deployer": TRADER, "pairToken": ZERO_ADDRESS,
                                     "launchConfigId": 1, "graduationThreshold": 4 * 10 ** 18}, lp.factory, 10, 0),
        log_of(ev["CurveBuy"], buy, CURVE, 11, 1),
        log_of(ev["CurveBuy"], buy, FOREIGN, 11, 2),  # look-alike from an unknown contract
        log_of(ev["TokenLaunched"], {"token": FOREIGN, "curve": FOREIGN, "deployer": TRADER, "pairToken": ZERO_ADDRESS,
                                     "launchConfigId": 1, "graduationThreshold": 1}, FOREIGN, 12, 0),
    ]
    node.honor_address = False  # a node that ignores the address filter
    res = await lp.scan(0, 100)
    assert [x.token for x in res.launches] == [TOKEN]
    assert len(res.trades) == 1 and res.trades[0].token == TOKEN and res.trades[0].fee == 2 * 10 ** 14
    assert res.rejected_foreign == 2


async def test_pons_v2_buy_simulation_fallback_and_sell_formula():
    node = Node(4663)
    lp = PonsV2(rpc_for(node, "robinhood"))
    pons_setup(node, lp)

    def sim(p):
        assert p[0]["value"] == hex(10 ** 16) and dex.SIM_ACCOUNT in p[2]
        return enc(["uint256"], [4242])

    node.on(CURVE, "buy(uint256,uint256,address)", sim)
    q = await lp.quote_buy(TOKEN, 10 ** 16)
    assert q.ok and q.amount_out == 4242 and q.exact
    node.reject_override = True
    q2 = await lp.quote_buy(TOKEN, 10 ** 16)
    net = 10 ** 16 - 2 * 10 ** 14
    assert q2.ok and not q2.exact and q2.amount_out == curve_amount_out(net, 2 * 10 ** 18, 10 ** 27)
    node.reject_override = False
    s = await lp.quote_sell(TOKEN, 10 ** 24)
    gross = curve_amount_out(10 ** 24, 10 ** 27, 2 * 10 ** 18)
    assert s.amount_out == gross - 2 * (gross * 100 // 10_000) and not s.exact
    pons_setup(node, lp, phase=2)
    g = await lp.quote_buy(TOKEN, 10 ** 16)
    assert not g.ok and "V4" in g.error
    assert (await lp.detect_migration(TOKEN))["tradable"] is False


# --- Odyssey --------------------------------------------------------------------------------------

async def test_odyssey_curve_buy_solves_budget_then_follows_migration_to_v3():
    node = Node(4663)
    lp = OdysseyCurve(rpc_for(node, "robinhood"))
    fac = lp.factories[0]
    vq, vt = 10 ** 18, 10 ** 27
    node.on(fac, "getPool(address)", enc(["address", "address", "bool", "bool"] + ["uint256"] * 12,
                                         [TRADER, ZERO_ADDRESS, False, False, vq, vt, vq, 5 * 10 ** 17] + [0] * 8))
    node.on(fac, "feeBps()", enc(["uint256"], [100]))

    def quote_buy(p):
        _tok, out = decode_output(["address", "uint256"], "0x" + p[0]["data"][10:])
        cost = vq * out // (vt - out) + 1
        fee = -(-cost * 100 // 10_000)
        return enc(["uint256", "uint256", "uint256", "uint256", "bool"], [cost, fee, cost + fee, out, False])

    node.on(fac, "quoteBuy(address,uint256)", quote_buy)
    node.on(fac, "quoteSell(address,uint256)", enc(["uint256"] * 3, [100, 1, 99]))
    q = await lp.quote_buy(TOKEN, 10 ** 16)
    assert q.ok and q.amount_in <= 10 ** 16 and q.amount_out > 0 and q.exact
    assert (await lp.quote_sell(TOKEN, 5)).amount_out == 99
    ev = CURVE_EVENTS.by_name
    node.logs = [log_of(ev["PoolMigrated"], {"token": TOKEN, "pool": POOL, "tokenId": 1, "liquidity": 5, "tokenUsed": 1,
                                             "quoteUsed": 2}, fac, 20, 0)]
    res = await lp.scan(0, 100)
    assert res.migrations[0]["pool"] == POOL
    node.on(POOL, "fee()", enc(["uint24"], [10000]))
    node.on(ROBINHOOD_UNISWAP_V3["quoter_v2"], "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            enc(["uint256", "uint160", "uint32", "uint256"], [31337, 0, 1, 90000]))
    v3 = await lp.quote_buy(TOKEN, 10 ** 16)
    assert v3.ok and v3.amount_out == 31337 and v3.route == "uniswap_v3:10000"
    # a Swap on the migrated pool is now a trade on the token
    token_is_0 = int(TOKEN, 16) < int(ROBINHOOD_WETH, 16)
    a0, a1 = (-500, 10 ** 15) if token_is_0 else (10 ** 15, -500)
    node.logs = [log_of(dex.V3_SWAP, {"sender": FOREIGN, "recipient": TRADER, "amount0": a0, "amount1": a1,
                                      "sqrtPriceX96": 1, "liquidity": 1, "tick": 0}, POOL, 30, 0)]
    t = (await lp.scan(0, 100)).trades[0]
    assert t.is_buy and t.token_amount == 500 and t.quote_amount == 10 ** 15 and t.trader == TRADER


def test_every_evm_launchpad_has_an_adapter():
    assert set(EVM_LAUNCHPADS) == {k for k, s in LAUNCHPADS.items() if s.chain.value != "solana"}
    for key in EVM_LAUNCHPADS:
        ad = adapter_for(key, EvmRpc("x", 1, ["https://n.example"]))
        assert isinstance(ad, LaunchpadAdapter) and ad.spec.key == key


async def test_unsupported_method_fails_over_but_a_revert_reason_never_does():
    hosts: list[str] = []

    def handler(req):
        body = json.loads(req.content)
        hosts.append(req.url.host)
        rid, m = body["id"], body["method"]
        if m == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": hex(56)})
        if m == "eth_getLogs" and req.url.host == "a.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                             "error": {"code": -32601, "message": "the method eth_getLogs does not exist"}})
        if m == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid,
                                             "error": {"code": 3, "message": "execution reverted: trading disabled"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": []})

    rpc = EvmRpc("bsc", 56, ["https://a.example", "https://b.example"],
                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.get_logs([TOKEN], [[]], 1, 1) == []
    assert rpc.health()["endpoints"][0]["unsupported_methods"] == ["eth_getLogs"]
    hosts.clear()
    await rpc.get_logs([TOKEN], [[]], 1, 1)
    assert hosts == ["b.example"]  # a is no longer asked for logs
    hosts.clear()
    with pytest.raises(EvmRpcError, match="trading disabled"):
        await rpc.eth_call(TOKEN, "0x12345678")
    assert hosts == ["a.example"]  # the revert was not retried elsewhere


async def test_launchpad_verify_records_only_what_it_proved():
    from yonixalpha_core.tools.launchpad_verify import jsonable, verify

    node = Node(56)
    lp = FourMeme(rpc_for(node))
    mgr = lp.spec.contracts["manager_v2"]
    ev = FOUR_EVENTS.by_name
    node.logs = [
        log_of(ev["TokenCreate"], {"creator": TRADER, "token": TOKEN, "requestId": 9, "name": "M", "symbol": "M",
                                   "totalSupply": 10 ** 27, "launchTime": 1, "launchFee": 0}, mgr, 10, 0),
        log_of(ev["TokenPurchase"], {"token": TOKEN, "account": TRADER, "price": 5, "amount": 10 ** 24,
                                     "cost": 10 ** 17, "fee": 10 ** 15, "offers": 1, "funds": 2}, mgr, 11, 0),
    ]
    node.on(lp.helper, "getTokenInfo(address)", four_info())
    node.on(lp.helper, "tryBuy(address,uint256,uint256)",
            enc(["address", "address"] + ["uint256"] * 6, [TOKEN, ZERO_ADDRESS, 12345, 10 ** 16, 10 ** 14, 0, 0, 0]))
    node.on(lp.helper, "trySell(address,uint256)", enc(["address", "address", "uint256", "uint256"],
                                                       [TOKEN, ZERO_ADDRESS, 9 * 10 ** 15, 10 ** 14]))
    res = {r.check: r for r in await verify(lp, 0, 100, 10 ** 16)}
    assert {k: r.ok for k, r in res.items()} == {"ACTIVE": True, "DISCOVERY": True, "EVENTS": True, "QUOTE": True,
                                                  "LIQUIDITY": True, "MIGRATION_DETECTION": None}
    json.dumps(jsonable(res["QUOTE"].evidence))  # evidence is storable
    assert not ({"SAFETY", "BUY", "SELL", "TX_MONITORING"} & set(res))

    node.no_code.add(lp.helper.lower())
    node.logs = [{**node.logs[1], "data": "0x00"}]  # layout mismatch, no launches
    res = {r.check: r for r in await verify(lp, 0, 100, 10 ** 16)}
    assert (res["ACTIVE"].ok, res["DISCOVERY"].ok, res["EVENTS"].ok) == (False, False, False)
    assert res["QUOTE"].ok is None  # nothing to quote: not recorded, not a failure

    def down(req):
        return httpx.Response(503)

    dead = FourMeme(EvmRpc("bsc", 56, ["https://d.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(down))))
    with pytest.raises(EvmRpcUnavailableError):
        await verify(dead, 0, 100, 10 ** 16)  # the tool records nothing for this launchpad


async def test_evm_rpc_test_tells_a_logs_node_from_one_that_only_answers_blocks():
    """The dashboard TEST CONNECTION and tools/evm_rpc_probe. Seen on the
    server: the probe called a busy publicnode "NO LOGS" because it asked a
    dead contract up to the exact head ("block range extends beyond current
    head block" on a load-balanced node). The test now asks the chain's
    busiest emitter, a few blocks under the head; accepted with 0 logs is
    still serving logs."""
    from yonixalpha_core.chains.evm import rpc_registry as reg

    asked = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        if body["method"] == "eth_blockNumber":
            return _ok(body, hex(5000))
        f = body["params"][0]
        asked.append((req.url.host, f["address"], int(f["toBlock"], 16)))
        if req.url.host == "dataseed.example":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32005, "message": "limit exceeded"}})
        if req.url.host == "free.example" and int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1 > 10:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32600, "message": "block range limit 10"}})
        return _ok(body, [])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    good = await reg.test_evm_rpc(client, "https://logs.example/KEY123", "bsc")
    assert good["status"] == reg.CONNECTED and good["logs_max_span"] == 2000 and good["head"] == 5000
    assert {(a, tb) for h, a, tb in asked if h == "logs.example"} == {(reg.LOGS_PROBE_CONTRACT["bsc"], 4995)}
    assert "KEY123" not in json.dumps(good)
    free = await reg.test_evm_rpc(client, "https://free.example", "bsc")
    assert free["status"] == reg.CONNECTED and free["logs_max_span"] == 10 and "slow" in free["detail"]
    bad = await reg.test_evm_rpc(client, "https://dataseed.example", "bsc")
    assert bad["status"] == reg.NO_LOGS and "limit exceeded" in bad["detail"]
    wrong = await reg.test_evm_rpc(client, "https://x.example", "robinhood")
    assert wrong["status"] == reg.INVALID and "expected 4663" in wrong["detail"] and not wrong["logs"]
    assert (await reg.test_evm_rpc(client, "http://insecure.example", "bsc"))["status"] == reg.INVALID


async def test_get_logs_starts_at_the_span_the_endpoint_accepted_and_probes_larger_now_and_then():
    """A free tier serving 10 blocks is not asked 2000, 1000, ... 16 on every
    call: the accepted span is remembered; twice that is tried again after
    LOGS_SPAN_PROBE_EVERY answers in case the limit was raised."""
    from yonixalpha_core.chains.evm import rpc as rpc_mod

    spans = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        f = body["params"][0]
        span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        spans.append(span)
        if span > 10:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32600, "message": "block range limit 10"}})
        return _ok(body, [])

    rpc = EvmRpc("bsc", 56, ["https://free.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await rpc.get_logs([TOKEN], [[]], 0, 99, max_span=2000)
    first = len(spans)
    spans.clear()
    learned = rpc.endpoints[0].logs_span  # halving 2000 lands on 7: accepted, so kept
    await rpc.get_logs([TOKEN], [[]], 100, 199, max_span=2000)
    assert learned == 7 and first > 8 and max(spans) == learned  # no refused request once learned
    rpc.endpoints[0].logs_since_probe = rpc_mod.LOGS_SPAN_PROBE_EVERY
    spans.clear()
    await rpc.get_logs([TOKEN], [[]], 200, 213, max_span=2000)
    assert spans == [14, 7, 7]  # one larger try (refused), back to the learned span


def test_evm_rpc_replace_urls_keeps_state_of_unchanged_endpoints():
    rpc = EvmRpc("bsc", 56, ["https://a.example", "https://b.example"])
    a = rpc.endpoints[0]
    a.mark_unsupported("eth_getLogs")
    assert rpc.replace_urls(["https://new.example", "https://a.example"])
    assert [e.url for e in rpc.endpoints] == ["https://new.example", "https://a.example"]
    assert rpc.endpoints[1] is a and a.refuses("eth_getLogs", __import__("time").monotonic())
    assert not rpc.replace_urls([]) and len(rpc.endpoints) == 2  # never left without endpoints


async def test_too_many_results_for_logs_is_halved_never_taken_as_method_not_served():
    """launchpad_verify asked publicnode for 2000 busy BSC blocks (Flap: ~600
    trades a minute) and the node was then skipped for logs although the
    service reads logs from it every few seconds. An answer about size, even
    worded "not supported", halves the range instead."""
    spans = []

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        f = body["params"][0]
        span = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        spans.append(span)
        if span > 500:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                "code": -32000, "message": "query exceeds max results 10000, not supported"}})
        return _ok(body, [])

    rpc = EvmRpc("bsc", 56, ["https://publicnode.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await rpc.get_logs([TOKEN], [[]], 0, 1999, max_span=2000) == []
    assert spans[:3] == [2000, 1000, 500] and rpc.health()["endpoints"][0]["unsupported_methods"] == []


async def test_a_null_block_from_a_load_balanced_node_is_asked_again_then_an_explicit_outage():
    """bsc-rpc.publicnode.com answered null for a block just reported as the
    head (seen on the server as "'NoneType' object is not subscriptable")."""
    answers = {"n": 0}

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return _ok(body, hex(56))
        answers["n"] += 1
        if body["params"][0] == hex(5) and answers["n"] == 1:
            return _ok(body, None)
        if body["params"][0] == hex(6):
            return _ok(body, None)
        return _ok(body, {"number": body["params"][0], "timestamp": hex(100)})

    rpc = EvmRpc("bsc", 56, ["https://lb.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await rpc.get_block(5))["timestamp"] == hex(100)
    with pytest.raises(EvmRpcUnavailableError, match="block 6 not available yet"):
        await rpc.get_block(6)
