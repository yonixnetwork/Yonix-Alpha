"""EVM layer: ABI codec, RPC failover, and every launchpad adapter against a
fake JSON-RPC node (httpx.MockTransport). No network: these prove the
decoding / quoting logic, not that the real contracts behave this way
(that is what tools/launchpad_verify records on the server)."""

import json
from decimal import Decimal

import httpx
import pytest
from eth_abi import encode

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

TOKEN = "0x1111111111111111111111111111111111111111"
TRADER = "0x2222222222222222222222222222222222222222"
CURVE = "0x3333333333333333333333333333333333333333"
POOL = "0x4444444444444444444444444444444444444444"
FOREIGN = "0x5555555555555555555555555555555555555555"
TX = "0x" + "ab" * 32


def enc(types, values) -> str:
    return "0x" + encode(types, values).hex()


class Node:
    """A fake EVM node: eth_call answers keyed by (to, selector); logs are
    filtered like a real node unless `honor_address` is False."""

    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        self.calls: dict[tuple[str, str], object] = {}
        self.logs: list[dict] = []
        self.honor_address = True
        self.requests: list[dict] = []
        self.reject_override = False
        self.no_code: set[str] = set()

    def on(self, to: str, signature: str, result) -> None:
        self.calls[(to.lower(), "0x" + selector(signature).hex())] = result

    def handle(self, body: dict) -> dict:
        self.requests.append(body)
        m, p = body["method"], body.get("params") or []
        ok = lambda r: {"jsonrpc": "2.0", "id": body["id"], "result": r}  # noqa: E731
        err = lambda msg: {"jsonrpc": "2.0", "id": body["id"], "error": {"code": 3, "message": msg}}  # noqa: E731
        if m == "eth_chainId":
            return ok(hex(self.chain_id))
        if m == "eth_getCode":
            return ok("0x" if p[0].lower() in self.no_code else "0x6080604052")
        if m == "eth_blockNumber":
            return ok(hex(1000))
        if m == "eth_getBlockByNumber":
            return ok({"number": p[0], "timestamp": hex(1_790_000_000)})
        if m == "eth_getLogs":
            f = p[0]
            addrs = f.get("address")
            addrs = {a.lower() for a in ([addrs] if isinstance(addrs, str) else addrs or [])}
            t0 = {t.lower() for t in f["topics"][0]} if f["topics"] and isinstance(f["topics"][0], list) \
                else {f["topics"][0].lower()} if f["topics"] else set()
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            out = [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi
                   and (not t0 or x["topics"][0].lower() in t0)
                   and (not self.honor_address or not addrs or x["address"].lower() in addrs)]
            if len(f["topics"]) > 1:
                out = [x for x in out if len(x["topics"]) > 1 and x["topics"][1].lower() == f["topics"][1].lower()]
            return ok(out)
        if m == "eth_call":
            if len(p) > 2 and self.reject_override:
                return err("invalid params: too many arguments")
            key = (p[0]["to"].lower(), p[0]["data"][:10])
            r = self.calls.get(key)
            if r is None:
                return err("execution reverted")
            if isinstance(r, Exception):
                return err(str(r))
            return ok(r(p) if callable(r) else r)
        return err(f"method {m} not faked")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: httpx.Response(200, json=self.handle(json.loads(req.content))))


def rpc_for(node: Node, chain="bsc") -> EvmRpc:
    return EvmRpc(chain, node.chain_id, ["https://node.example/key-SECRET"],
                  client=httpx.AsyncClient(transport=node.transport()))


def log_of(ev, values, address, block=10, index=0) -> dict:
    return ev.encode_log(values, address, blockNumber=hex(block), logIndex=hex(index), transactionHash=TX)


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
