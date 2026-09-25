"""Execution providers against mocked venues (httpx.MockTransport): request
shapes, signatures, status mapping and error handling. No request leaves
the process."""

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from yonixalpha_core.execution import CANCELED, FILLED, OPEN, PARTIAL, REJECTED, ExecutionError, NotConfigured
from yonixalpha_core.execution.binance import BinanceProvider
from yonixalpha_core.execution.bybit import BybitProvider
from yonixalpha_core.execution.hyperliquid import HyperliquidProvider, cloid_for
from yonixalpha_core.execution.mt5_bridge import MT5BridgeProvider
from yonixalpha_core.execution.registry import build_providers
from yonixalpha_core.venues.common import VenueError

CLOCK = lambda: 1_700_000_000.0  # noqa: E731


def mock(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), calls


def q(request: httpx.Request) -> dict:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


# -- Binance -------------------------------------------------------------------

EXCHANGE_INFO = {"symbols": [{"symbol": "BTCUSDT", "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"}, {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]}


def binance_handler(request: httpx.Request):
    path = request.url.path
    if path == "/fapi/v1/exchangeInfo":
        return httpx.Response(200, json=EXCHANGE_INFO)
    assert request.headers["X-MBX-APIKEY"] == "k"
    params = q(request)
    raw = urlsplit(str(request.url)).query
    unsigned = raw[:raw.index("&signature=")]
    assert params["signature"] == hmac.new(b"s", unsigned.encode(), hashlib.sha256).hexdigest()
    assert params["timestamp"] == "1700000000000"
    if path == "/fapi/v1/order" and request.method == "POST":
        return httpx.Response(200, json={"orderId": 42, "status": "FILLED", "executedQty": params["quantity"],
                                         "avgPrice": "65000.5", "clientOrderId": params["newClientOrderId"]})
    if path == "/fapi/v1/order" and request.method == "GET":
        if params["origClientOrderId"] == "missing":
            return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
        return httpx.Response(200, json={"orderId": 43, "status": "PARTIALLY_FILLED", "executedQty": "0.004", "avgPrice": "65000"})
    if path == "/fapi/v1/userTrades":
        return httpx.Response(200, json=[{"orderId": 42, "commission": "0.013", "commissionAsset": "USDT", "qty": "0.002",
                                          "price": "65000.5", "side": "BUY", "realizedPnl": "0", "time": 1}])
    if path == "/fapi/v1/algoOrder":
        return httpx.Response(200, json={"algoId": 777, "clientAlgoId": params["clientAlgoId"], "algoStatus": "NEW"})
    if path == "/fapi/v1/positionSide/dual":
        return httpx.Response(200, json={"dualSidePosition": params.get("x") == "hedge"})
    if path == "/fapi/v1/leverage":
        return httpx.Response(200, json={"leverage": int(params["leverage"]), "symbol": params["symbol"]})
    if path == "/fapi/v2/positionRisk":
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "positionAmt": "-0.002", "entryPrice": "65000",
                                          "markPrice": "64000", "unRealizedProfit": "2"}])
    if path == "/fapi/v2/balance":
        return httpx.Response(200, json=[{"asset": "BNB", "availableBalance": "1"}, {"asset": "USDT", "availableBalance": "512.5"}])
    if path == "/fapi/v1/openAlgoOrders":
        return httpx.Response(200, json=[{"algoId": 777, "orderType": "STOP_MARKET", "triggerPrice": "60000", "side": "SELL"}])
    return httpx.Response(404, json={"code": -1, "msg": "unexpected"})


async def test_binance_market_order_signed_and_confirmed_with_fees():
    client, calls = mock(binance_handler)
    p = BinanceProvider(client, "k", "s", testnet=True, clock=CLOCK)
    st = await p.market_order("BTCUSDT", "BUY", Decimal("0.002"), False, "yx-entry-1")
    assert st.status == FILLED and st.filled_qty == Decimal("0.002") and st.avg_price == Decimal("65000.5")
    assert st.fee == Decimal("0.013") and st.exchange_id == "42"
    order = q(calls[0])
    assert calls[0].url.host == "testnet.binancefuture.com"
    assert order["type"] == "MARKET" and order["newClientOrderId"] == "yx-entry-1" and "reduceOnly" not in order
    await p.market_order("BTCUSDT", "SELL", Decimal("0.002"), True, "yx-exit-1")
    assert q(calls[2])["reduceOnly"] == "true"


async def test_binance_stop_goes_through_the_algo_service():
    client, calls = mock(binance_handler)
    p = BinanceProvider(client, "k", "s", clock=CLOCK)
    oid = await p.set_stop("BTCUSDT", "LONG", Decimal("60000.07"), Decimal("0.002"), "x" * 50)
    algo = q(calls[-1])
    assert calls[-1].url.path == "/fapi/v1/algoOrder" and oid == "777"
    assert algo["algoType"] == "CONDITIONAL" and algo["type"] == "STOP_MARKET" and algo["side"] == "SELL"
    assert Decimal(algo["triggerPrice"]) == Decimal("60000.1")
    assert algo["closePosition"] == "true" and "quantity" not in algo and "reduceOnly" not in algo
    assert algo["workingType"] == "MARK_PRICE" and len(algo["clientAlgoId"]) == 36
    prot = await p.open_protection("BTCUSDT")
    assert prot[0]["type"] == "STOP_MARKET"


async def test_binance_status_account_and_errors():
    client, _ = mock(binance_handler)
    p = BinanceProvider(client, "k", "s", clock=CLOCK)
    st = await p.order_status("BTCUSDT", "any")
    assert st.status == PARTIAL and st.filled_qty == Decimal("0.004")
    assert (await p.order_status("BTCUSDT", "missing")).status == REJECTED
    pos = await p.position("BTCUSDT")
    assert pos.size == Decimal("-0.002") and pos.entry_price == Decimal("65000")
    assert await p.balance() == Decimal("512.5")
    rules = await p.instrument("BTCUSDT")
    assert rules.round_qty(Decimal("0.0029")) == Decimal("0.002") and rules.check(Decimal("0.001"), Decimal(65000)) is not None
    with pytest.raises(NotConfigured):
        await BinanceProvider(client, None, None).balance()


async def test_binance_hedge_mode_is_refused():
    def handler(request):
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": True})
        return httpx.Response(200, json={})
    client, _ = mock(handler)
    with pytest.raises(ExecutionError, match="Hedge Mode"):
        await BinanceProvider(client, "k", "s", clock=CLOCK).prepare("BTCUSDT", 3)


# -- Bybit ---------------------------------------------------------------------

def bybit_handler(request: httpx.Request):
    path = request.url.path
    ok = lambda result: httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})  # noqa: E731
    if path == "/v5/market/instruments-info":
        return ok({"list": [{"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
                             "priceFilter": {"tickSize": "0.10"}}]})
    ts, key = request.headers["X-BAPI-TIMESTAMP"], request.headers["X-BAPI-API-KEY"]
    payload = request.content.decode() if request.method == "POST" else urlsplit(str(request.url)).query
    expected = hmac.new(b"s", f"{ts}{key}5000{payload}".encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected
    if path == "/v5/order/create":
        body = json.loads(request.content)
        assert body["orderType"] == "Market" and body["positionIdx"] == 0
        return ok({"orderId": "b-1", "orderLinkId": body["orderLinkId"]})
    if path == "/v5/order/realtime":
        return ok({"list": [{"orderId": "b-1", "orderStatus": "Filled", "cumExecQty": "0.01", "avgPrice": "3000",
                             "cumExecFee": "0.0165"}]})
    if path == "/v5/position/set-leverage":
        return httpx.Response(200, json={"retCode": 110043, "retMsg": "leverage not modified", "result": {}})
    if path == "/v5/position/list":
        return ok({"list": [{"positionIdx": 0, "size": "0.01", "side": "Sell", "avgPrice": "3000", "markPrice": "2990",
                             "unrealisedPnl": "0.1", "stopLoss": "3100"}]})
    if path == "/v5/position/trading-stop":
        return ok({})
    if path == "/v5/account/wallet-balance":
        return ok({"list": [{"totalAvailableBalance": "250.5"}]})
    if path == "/v5/order/history":
        return ok({"list": []})
    return httpx.Response(200, json={"retCode": 10001, "retMsg": "bad", "result": {}})


async def test_bybit_signed_order_status_and_leverage_not_modified():
    client, calls = mock(bybit_handler)
    p = BybitProvider(client, "k", "s", clock=CLOCK)
    await p.prepare("ETHUSDT", 3)  # 110043 is not an error
    st = await p.market_order("ETHUSDT", "SELL", Decimal("0.01"), False, "yx-1")
    assert st.status == FILLED and st.avg_price == Decimal("3000") and st.fee == Decimal("0.0165") and st.exchange_id == "b-1"
    assert json.loads(calls[2].content)["side"] == "Sell"
    pos = await p.position("ETHUSDT")
    assert pos.size == Decimal("-0.01")
    assert (await p.open_protection("ETHUSDT"))[0]["trigger_price"] == "3100"
    await p.set_stop("ETHUSDT", "SHORT", Decimal("3100.04"), Decimal("0.01"), "yx-sl")
    body = json.loads(calls[-1].content)
    assert Decimal(body["stopLoss"]) == Decimal("3100") and body["tpslMode"] == "Full" and body["slTriggerBy"] == "MarkPrice"
    assert await p.balance() == Decimal("250.5")


async def test_bybit_error_codes_raise():
    client, _ = mock(bybit_handler)
    p = BybitProvider(client, "k", "s", clock=CLOCK)
    with pytest.raises(ExecutionError, match="10001"):
        await p.fills_since("ETHUSDT", 0)
    with pytest.raises(VenueError):  # ExecutionError is a VenueError: health tracks it
        await p.fills_since("ETHUSDT", 0)


# -- Hyperliquid ---------------------------------------------------------------

TEST_KEY = "0x" + "11" * 32  # a throwaway test key, not a funded wallet


def hl_handler(state: dict):
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if request.url.path == "/info":
            t = body["type"]
            if t == "meta":
                return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]})
            if t == "allMids":
                return httpx.Response(200, json={"ETH": "3000.0"})
            if t == "userFillsByTime":
                return httpx.Response(200, json=[{"coin": "ETH", "oid": 9, "px": "3001", "sz": "0.5", "side": "B", "time": 5,
                                                  "fee": "0.675", "closedPnl": "0"}])
            if t == "orderStatus":
                return httpx.Response(200, json={"status": "order", "order": {"status": "canceled", "statusTimestamp": 10,
                                                                              "order": {"oid": 9, "origSz": "1.0", "sz": "0.5"}}})
            if t == "clearinghouseState":
                return httpx.Response(200, json={"withdrawable": "900.5", "assetPositions": [
                    {"position": {"coin": "ETH", "szi": "0.5", "entryPx": "3001", "unrealizedPnl": "1"}}]})
            if t == "frontendOpenOrders":
                return httpx.Response(200, json=[{"coin": "ETH", "oid": 11, "isTrigger": True, "orderType": "Stop Market",
                                                  "triggerPx": "2900", "side": "A"}])
        state.setdefault("actions", []).append(body)
        a = body["action"]
        assert body["signature"]["r"].startswith("0x") and body["nonce"] == 1_700_000_000_000
        if a["type"] == "order":
            o = a["orders"][0]
            if o["t"].get("trigger"):
                return httpx.Response(200, json={"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 11}}]}}})
            if state.get("reject"):
                return httpx.Response(200, json={"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Insufficient margin"}]}}})
            return httpx.Response(200, json={"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": o["s"], "avgPx": "3001", "oid": 9}}]}}})
        return httpx.Response(200, json={"status": "ok", "response": {"type": a["type"], "data": {"statuses": ["success"]}}})
    return handler


async def test_hyperliquid_market_order_signed_ioc_with_cloid_and_real_fee():
    state: dict = {}
    client, _ = mock(hl_handler(state))
    p = HyperliquidProvider(client, "0xABC", TEST_KEY, clock=CLOCK)
    st = await p.market_order("ETH", "BUY", Decimal("0.5"), False, "yx-entry-1")
    assert st.status == FILLED and st.avg_price == Decimal("3001") and st.fee == Decimal("0.675") and st.exchange_id == "9"
    o = state["actions"][0]["action"]["orders"][0]
    assert o["a"] == 1 and o["b"] is True and o["r"] is False and o["t"] == {"limit": {"tif": "Ioc"}}
    assert o["p"] == "3060" and o["s"] == "0.5"  # mid 3000 + 2%, 5 significant figures
    assert o["c"] == cloid_for("yx-entry-1") and len(o["c"]) == 34


async def test_hyperliquid_reject_stop_and_partial_status():
    state: dict = {"reject": True}
    client, _ = mock(hl_handler(state))
    p = HyperliquidProvider(client, "0xabc", TEST_KEY, clock=CLOCK)
    st = await p.market_order("ETH", "BUY", Decimal("0.5"), False, "x")
    assert st.status == REJECTED and "margin" in st.error
    sid = await p.set_stop("ETH", "LONG", Decimal("2900.123"), Decimal("0.5"), "sl-1")
    trig = state["actions"][-1]["action"]["orders"][0]
    assert sid == "11" and trig["r"] is True and trig["b"] is False
    assert trig["t"]["trigger"] == {"isMarket": True, "triggerPx": "2900.1", "tpsl": "sl"}
    s = await p.order_status("ETH", "x")  # IOC cancelled with half filled
    assert s.status == CANCELED and s.filled_qty == Decimal("0.5") and s.avg_price == Decimal("3001")
    assert (await p.position("ETH")).size == Decimal("0.5") and await p.balance() == Decimal("900.5")
    await p.cancel_protection("ETH")
    assert state["actions"][-1]["action"] == {"type": "cancel", "cancels": [{"a": 1, "o": 11}]}


async def test_hyperliquid_needs_both_address_and_agent_key():
    client, _ = mock(hl_handler({}))
    assert not HyperliquidProvider(client, "0xabc", None).configured
    with pytest.raises(NotConfigured):
        await HyperliquidProvider(client, "0xabc", None).prepare("ETH", 2)
    with pytest.raises(NotConfigured, match="not a valid"):
        await HyperliquidProvider(client, "0xabc", "nonsense").prepare("ETH", 2)


# -- MT5 bridge ----------------------------------------------------------------

async def test_mt5_bridge_bearer_auth_and_contract():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/symbols/EURUSD":
            return httpx.Response(200, json={"qty_step": "1000", "min_qty": "1000", "tick_size": "0.00001"})
        if path == "/orders" and request.method == "POST":
            b = json.loads(request.content)
            assert b["qty"] == "10000" and b["client_id"] == "c1"
            return httpx.Response(200, json={"status": "FILLED", "filled_qty": "10000", "avg_price": "1.08001", "fee": "0.7",
                                             "exchange_id": "555"})
        if path == "/orders/gone":
            return httpx.Response(404, json={"detail": "unknown"})
        if path == "/balance":
            return httpx.Response(200, json={"currency": "EUR", "free_margin": "1000.5"})
        if path == "/positions/EURUSD":
            return httpx.Response(404, json={"detail": "flat"})
        return httpx.Response(500, json={"detail": "terminal disconnected"})
    client, _ = mock(handler)
    p = MT5BridgeProvider(client, "http://bridge:9000/", "tok")
    st = await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "c1")
    assert st.status == FILLED and st.avg_price == Decimal("1.08001")
    assert (await p.order_status("EURUSD", "gone")).status == REJECTED
    assert await p.balance() == Decimal("1000.5") and p.quote_currency == "EUR"
    assert await p.position("EURUSD") is None
    with pytest.raises(ExecutionError, match="terminal disconnected"):
        await p.fills_since("EURUSD", 0)
    with pytest.raises(NotConfigured):
        await MT5BridgeProvider(client, None, None).balance()


def test_registry_builds_every_provider_without_credentials():
    class S:
        BINANCE_TESTNET = True
    providers = build_providers(httpx.AsyncClient(), S())
    assert set(providers) == {"binance", "bybit", "hyperliquid", "mt5"}
    assert not any(p.configured for p in providers.values())
    assert providers["binance"].base.startswith("https://testnet")
    assert OPEN == "NEW"
