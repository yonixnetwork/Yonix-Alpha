"""Exchange adapters on recorded-shape responses (MockTransport, no network)
and candle hygiene."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from yonixalpha_crypto.candles import Candle, clean
from yonixalpha_crypto.exchanges import market_data
from yonixalpha_crypto.publichttp import ExchangeUnavailable

from tests.conftest import NOW, candles


def bybit_handler(request: httpx.Request) -> httpx.Response:
    p, q = request.url.path, dict(request.url.params)
    assert request.method == "GET" and "X-BAPI-API-KEY" not in request.headers
    if p == "/v5/market/instruments-info":
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": [{
            "symbol": q["symbol"], "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
            "lotSizeFilter": {"basePrecision": "0.000001", "quotePrecision": "0.00000001", "minOrderQty": "0.000048",
                              "maxOrderQty": "71.73", "minOrderAmt": "1", "maxOrderAmt": "2000000"},
            "priceFilter": {"tickSize": "0.01"}}]}})
    if p == "/v5/market/kline":
        t0 = int((NOW - timedelta(minutes=10)).timestamp()) // 300 * 300 * 1000
        rows = [[str(t0 + i * 300000), "1", "2", "0.5", str(100 + i), "3", "300"] for i in range(3)]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": list(reversed(rows))}})
    if p == "/v5/market/tickers":
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "time": int(NOW.timestamp() * 1000),
                                         "result": {"list": [{"symbol": "BTCUSDT", "bid1Price": "99.9",
                                                              "bid1Size": "1.5", "ask1Price": "100.1",
                                                              "ask1Size": "2"}]}})
    if p == "/v5/market/time":
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {}})
    return httpx.Response(404)


async def test_bybit_spot_instrument_candles_and_book():
    md = market_data("bybit", transport=httpx.MockTransport(bybit_handler))
    await md.ping()
    s = await md.instrument("btc/usdt")
    assert (s.symbol, s.venue_symbol, s.tradable) == ("BTC/USDT", "BTCUSDT", True)
    assert s.qty_step == Decimal("0.000001") and s.tick_size == Decimal("0.01") and s.min_notional == Decimal("1")
    cs = await md.candles("BTC/USDT", "5m", 3)
    assert [c.close for c in cs] == [Decimal(100), Decimal(101), Decimal(102)]  # newest-first input re-sorted
    b = await md.top_of_book("BTC/USDT")
    assert (b.bid, b.ask, b.ask_qty, b.at) == (Decimal("99.9"), Decimal("100.1"), Decimal("2"), NOW.replace(microsecond=0))
    await md.aclose()


async def test_bybit_error_codes_are_unavailable_not_data():
    md = market_data("bybit", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"retCode": 10001, "retMsg": "params error"})))
    with pytest.raises(ExchangeUnavailable, match="10001"):
        await md.instrument("BTC/USDT")
    await md.aclose()


async def test_binance_restricted_location_is_reported_as_451():
    body = {"code": 0, "msg": "Service unavailable from a restricted location"}
    md = market_data("binance", transport=httpx.MockTransport(lambda r: httpx.Response(451, json=body)))
    with pytest.raises(ExchangeUnavailable) as e:
        await md.ping()
    assert e.value.status == 451 and "restricted location" in e.value.detail
    await md.aclose()


@pytest.mark.parametrize("notional_filter", ["NOTIONAL", "MIN_NOTIONAL"])
async def test_binance_and_binance_us_filters(notional_filter):
    def handler(r):
        if r.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
                                                          "status": "TRADING", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00010000", "minQty": "0.00010000"},
                {"filterType": notional_filter, "minNotional": "5.00000000"}]}]})
        return httpx.Response(404)

    for ex in ("binance", "binance_us"):
        md = market_data(ex, transport=httpx.MockTransport(handler))
        s = await md.instrument("ETH/USDT")
        assert s.exchange == ex and s.min_notional == Decimal(5) and s.qty_step == Decimal("0.0001")
        assert s.round_qty(Decimal("0.123456")) == Decimal("0.1234")
        assert s.round_price(Decimal("100.001"), up=True) == Decimal("100.01")
        assert s.round_price(Decimal("100.009"), up=False) == Decimal("100.00")
        await md.aclose()


def test_candle_hygiene_duplicates_order_forming_gaps_stale():
    cs = candles(["1", "2", "3", "4"])
    forming = Candle(cs[-1].open_time + timedelta(minutes=5), *[Decimal(9)] * 5, "5m")
    gap = [c for i, c in enumerate(cs) if i != 1]  # one bar missing
    raw = list(reversed(gap)) + [gap[0], forming]  # out of order, a duplicate, a forming candle
    out = clean(raw, "5m", NOW)
    assert [c.close for c in out.candles] == [Decimal(1), Decimal(3), Decimal(4)]
    assert out.dropped_duplicates == 1 and out.dropped_forming == 1 and len(out.gaps) == 1 and not out.stale
    assert clean(cs, "5m", NOW + timedelta(minutes=30)).stale
    assert clean([], "5m", NOW).stale_reason == "no closed candle"


# ---- Binance's public market-data-only host (data-api.binance.vision) -----

def _data_api(seen):
    t0 = int((NOW - timedelta(hours=3)).timestamp()) // 3600 * 3600 * 1000

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        assert r.method == "GET" and r.url.host == "data-api.binance.vision"
        assert not {"x-mbx-apikey", "authorization"} & {k.lower() for k in r.headers}
        assert "signature" not in r.url.params and "timestamp" not in r.url.params
        if r.url.path == "/api/v3/klines":
            rows = [[t0 + i * 3600000, "1", "2", "0.5", str(100 + i), "3", t0 + (i + 1) * 3600000 - 1] for i in range(3)]
            return httpx.Response(200, json=rows)
        if r.url.path == "/api/v3/ping":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"code": -1, "msg": "not mocked"})
    return handler


async def test_public_data_only_provider_reads_candles_without_any_credential():
    seen = []
    md = market_data("binance_data", transport=httpx.MockTransport(_data_api(seen)))
    await md.ping()
    cs = await md.candles("BTC/USDT", "1h", 3)
    assert [c.close for c in cs] == [Decimal(100), Decimal(101), Decimal(102)]
    assert cs[0].open_time.tzinfo is not None and md.exchange == "binance_data"  # millisecond open times -> UTC datetimes
    assert [r.url.path for r in seen] == ["/api/v3/ping", "/api/v3/klines"]
    await md.aclose()


@pytest.mark.parametrize("status", [451, 403, 429, 503])
async def test_public_data_only_provider_reports_blocks_and_limits_as_unavailable(status):
    md = market_data("binance_data", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"code": 0, "msg": "refused"}, headers={"Retry-After": "2"})))
    with pytest.raises(ExchangeUnavailable) as e:
        await md.ping()
    assert e.value.status == status
    await md.aclose()


def test_public_data_only_provider_refuses_private_paths_before_sending():
    from yonixalpha_crypto.publichttp import RealOrderBlocked, check_request
    for method, path in [("GET", "/api/v3/account"), ("POST", "/api/v3/order"), ("POST", "/api/v3/order/test"),
                         ("GET", "/api/v3/myTrades")]:
        with pytest.raises(RealOrderBlocked):
            check_request("binance_data", method, path, {}, {})
    with pytest.raises(RealOrderBlocked):
        check_request("binance_data", "GET", "/api/v3/klines", {"signature": "x"}, {})
    check_request("binance_data", "GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"}, {})  # allowed


def test_public_data_only_provider_is_health_checked_but_not_selectable_for_strategies():
    from yonixalpha_crypto import registry
    from yonixalpha_crypto.exchanges import EXCHANGES
    assert "binance_data" in EXCHANGES  # the worker's health check probes it from the server
    assert "binance_data" not in registry.SPOT_EXCHANGES  # strategies cannot use it until the operator decides
