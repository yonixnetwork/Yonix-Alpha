from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.client import BinanceApiError, BinanceFuturesClient

pytestmark = pytest.mark.asyncio


def _client(responder, testnet=True) -> BinanceFuturesClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return BinanceFuturesClient(http_client, api_key="test-api-key", api_secret="test-secret", testnet=testnet)


async def test_testnet_base_url():
    client = _client(lambda r: httpx.Response(200, json={}), testnet=True)
    assert "testnet" in client.base_url


async def test_production_base_url():
    client = _client(lambda r: httpx.Response(200, json={}), testnet=False)
    assert client.base_url == "https://fapi.binance.com"


async def test_signed_request_includes_api_key_header_and_signature():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"result": "ok"})

    client = _client(responder)
    result = await client.get_account()

    assert result == {"result": "ok"}
    assert captured["headers"]["X-MBX-APIKEY"] == "test-api-key"
    assert "signature" in captured["query"]
    assert "timestamp" in captured["query"]
    assert "recvWindow" in captured["query"]


async def test_get_position_risk_includes_symbol_when_given():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json=[])

    client = _client(responder)
    await client.get_position_risk(symbol="BTCUSDT")

    assert captured["path"] == "/fapi/v2/positionRisk"
    assert captured["query"]["symbol"] == ["BTCUSDT"]


async def test_place_order_forwards_all_params():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"orderId": 123, "status": "NEW"})

    client = _client(responder)
    result = await client.place_order(
        symbol="BTCUSDT", side="BUY", type="MARKET", quantity="0.01", newClientOrderId="my-id-1"
    )

    assert result == {"orderId": 123, "status": "NEW"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/fapi/v1/order"
    assert captured["query"]["symbol"] == ["BTCUSDT"]
    assert captured["query"]["newClientOrderId"] == ["my-id-1"]


async def test_cancel_order_uses_delete_and_orig_client_order_id():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"status": "CANCELED"})

    client = _client(responder)
    await client.cancel_order(symbol="BTCUSDT", orig_client_order_id="my-id-1")

    assert captured["method"] == "DELETE"
    assert captured["query"]["origClientOrderId"] == ["my-id-1"]


async def test_listen_key_endpoints_have_no_signature():
    """listenKey endpoints need only the API key header — signing them
    would be wrong (and Binance would reject an unexpected signature param).
    """
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"listenKey": "abc123"})

    client = _client(responder)
    listen_key = await client.start_user_data_stream()

    assert listen_key == "abc123"
    assert captured["headers"]["X-MBX-APIKEY"] == "test-api-key"
    assert "signature" not in captured["query"]
    assert "timestamp" not in captured["query"]


async def test_error_response_raises_binance_api_error():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"code":-2010,"msg":"Account has insufficient balance"}')

    client = _client(responder)
    with pytest.raises(BinanceApiError) as exc_info:
        await client.get_account()

    assert exc_info.value.status_code == 400
    assert "insufficient balance" in exc_info.value.body


async def test_get_income_forwards_income_type_and_symbol():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json=[])

    client = _client(responder)
    await client.get_income(symbol="BTCUSDT", income_type="FUNDING_FEE")

    assert captured["query"]["symbol"] == ["BTCUSDT"]
    assert captured["query"]["incomeType"] == ["FUNDING_FEE"]
