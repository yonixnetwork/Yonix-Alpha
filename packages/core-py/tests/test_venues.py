"""Venue adapters against mocked HTTP in each venue's documented shape."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from yonixalpha_core.solana.market_data import RateBudget
from yonixalpha_core.venues.binance_public import BinanceFuturesPublic
from yonixalpha_core.venues.bybit import BybitClient, query_string, sign
from yonixalpha_core.venues.common import NotConfigured, VenueError
from yonixalpha_core.venues.hyperliquid import HyperliquidInfo

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
T0 = int(NOW.timestamp() * 1000) - 600_000


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Binance -------------------------------------------------------------------

def binance_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p == "/fapi/v1/klines":
        rows = [[T0 + i * 300_000, "100", "101", "99", str(100 + i), "10", T0 + (i + 1) * 300_000 - 1, "0", 1, "0", "0", "0"]
                for i in range(3)]
        return httpx.Response(200, json=rows)
    if p == "/fapi/v1/depth":
        return httpx.Response(200, json={"bids": [["99.9", "5"], ["99.8", "0"]], "asks": [["100.1", "5"]]})
    if p == "/fapi/v1/premiumIndex":
        return httpx.Response(200, json={"markPrice": "100.05", "lastFundingRate": "0.0001"})
    if p == "/fapi/v1/openInterest":
        return httpx.Response(200, json={"openInterest": "12345.6"})
    if p == "/fapi/v1/ticker/24hr":
        return httpx.Response(200, json={"lastPrice": "100.02", "quoteVolume": "5000000"})
    return httpx.Response(404, json={})


async def test_binance_klines_mark_the_forming_candle_open():
    b = BinanceFuturesPublic(client(binance_handler), RateBudget(100))
    c = await b.klines("ETHUSDT", "5m", now=NOW)
    assert [k.close for k in c] == [Decimal(100), Decimal(101), Decimal(102)]
    assert c[0].closed and c[1].closed and c[-1].closed is False


async def test_binance_book_drops_zero_levels_and_uses_taker_fee():
    b = BinanceFuturesPublic(client(binance_handler), RateBudget(100))
    m = await b.book("ETHUSDT")
    assert m.bids == ((Decimal("99.9"), Decimal("5")),) and m.fee_bps == Decimal("5")


async def test_binance_ticker_combines_mark_funding_oi():
    t = await BinanceFuturesPublic(client(binance_handler), RateBudget(100)).ticker("ETHUSDT")
    assert t.mark_price == Decimal("100.05") and t.funding_rate == Decimal("0.0001") and t.open_interest == Decimal("12345.6")


async def test_binance_http_error_is_venue_error():
    b = BinanceFuturesPublic(client(lambda r: httpx.Response(418, text="teapot")), RateBudget(100))
    with pytest.raises(VenueError):
        await b.book("ETHUSDT")


# --- Bybit ---------------------------------------------------------------------

def test_bybit_signature_matches_documented_scheme():
    qs = query_string({"symbol": "BTCUSDT", "category": "linear", "x": None})
    assert qs == "category=linear&symbol=BTCUSDT"
    expected = hmac.new(b"sec", b"1700000000000key5000category=linear&symbol=BTCUSDT", hashlib.sha256).hexdigest()
    assert sign("sec", "1700000000000", "key", 5000, qs) == expected


def bybit_handler(seen: list):
    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        p = request.url.path
        if p == "/v5/market/kline":
            rows = [[str(T0 + i * 300_000), "1", "2", "0.5", str(10 + i), "3", "30"] for i in range(3)][::-1]
            return httpx.Response(200, json={"retCode": 0, "result": {"list": rows}})
        if p == "/v5/market/orderbook":
            return httpx.Response(200, json={"retCode": 0, "result": {"b": [["99", "1"]], "a": [["101", "1"]]}})
        if p == "/v5/market/tickers":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                {"lastPrice": "100", "markPrice": "100.1", "fundingRate": "0.0002", "openInterest": "5",
                 "bid1Price": "99", "ask1Price": "101", "turnover24h": "1"}]}})
        if p == "/v5/account/wallet-balance":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": [{"totalEquity": "123.4"}]}})
        if p == "/v5/position/list":
            return httpx.Response(200, json={"retCode": 10003, "retMsg": "API key is invalid."})
        return httpx.Response(404)
    return h


async def test_bybit_klines_sorted_oldest_first():
    b = BybitClient(client(bybit_handler([])), RateBudget(100))
    c = await b.klines("BTCUSDT", "5m", now=NOW)
    assert [k.close for k in c] == [Decimal(10), Decimal(11), Decimal(12)]


async def test_bybit_private_calls_need_credentials_and_send_signed_headers():
    seen: list = []
    anon = BybitClient(client(bybit_handler(seen)), RateBudget(100))
    with pytest.raises(NotConfigured):
        await anon.wallet_balance()
    keyed = BybitClient(client(bybit_handler(seen)), RateBudget(100), "key", "sec")
    bal = await keyed.wallet_balance()
    assert bal["totalEquity"] == "123.4"
    req = seen[-1]
    assert req.headers["X-BAPI-API-KEY"] == "key" and req.headers["X-BAPI-SIGN-TYPE"] == "2"
    qs = req.url.query.decode()
    assert req.headers["X-BAPI-SIGN"] == sign("sec", req.headers["X-BAPI-TIMESTAMP"], "key", 5000, qs)
    assert "sec" not in str(req.url)


async def test_bybit_error_code_is_surfaced():
    b = BybitClient(client(bybit_handler([])), RateBudget(100), "key", "sec")
    with pytest.raises(VenueError, match="10003"):
        await b.positions()


async def test_bybit_ticker_and_book():
    b = BybitClient(client(bybit_handler([])), RateBudget(100))
    t = await b.ticker("BTCUSDT")
    assert t.bid == Decimal(99) and t.funding_rate == Decimal("0.0002")
    assert (await b.book("BTCUSDT")).mid == Decimal(100)


# --- Hyperliquid -----------------------------------------------------------------

def hl_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    t = body["type"]
    if t == "allMids":
        return httpx.Response(200, json={"BTC": "65000.5", "ETH": "3000"})
    if t == "l2Book":
        return httpx.Response(200, json={"coin": "BTC", "levels": [[{"px": "64999", "sz": "1", "n": 2}],
                                                                  [{"px": "65001", "sz": "2", "n": 1}]], "time": 1})
    if t == "metaAndAssetCtxs":
        return httpx.Response(200, json=[{"universe": [{"name": "BTC"}, {"name": "ETH"}]},
                                         [{"midPx": "65000", "markPx": "65001", "funding": "0.00001", "openInterest": "10"},
                                          {"midPx": "3000", "markPx": "3000", "funding": "0", "openInterest": "5"}]])
    if t == "candleSnapshot":
        req = body["req"]
        return httpx.Response(200, json=[{"t": req["startTime"], "T": req["startTime"] + 59_999, "o": "1", "h": "2", "l": "1",
                                          "c": "1.5", "v": "3", "n": 4, "i": "1m", "s": "BTC"}])
    if t == "clearinghouseState":
        return httpx.Response(200, json={"marginSummary": {"accountValue": "100"}, "assetPositions": []})
    return httpx.Response(400)


async def test_hyperliquid_market_views():
    h = HyperliquidInfo(client(hl_handler), RateBudget(100))
    assert await h.mid("BTC") == Decimal("65000.5")
    assert (await h.book("BTC")).mid == Decimal(65000)
    assert (await h.ticker("ETH")).mark_price == Decimal(3000)
    assert (await h.candles("BTC", "1m", count=5, now=NOW))[0].close == Decimal("1.5")
    with pytest.raises(VenueError):
        await h.ticker("DOGE")


async def test_hyperliquid_account_needs_only_an_address():
    with pytest.raises(NotConfigured):
        await HyperliquidInfo(client(hl_handler), RateBudget(100)).account()
    acct = await HyperliquidInfo(client(hl_handler), RateBudget(100), account_address="0xabc").account()
    assert acct["marginSummary"]["accountValue"] == "100"
