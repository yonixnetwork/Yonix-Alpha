import httpx
import pytest

from app.rest.client import BinanceMarketDataClient, BinanceRestError

pytestmark = pytest.mark.asyncio


async def test_ping_success():
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/ping"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http_client:
        client = BinanceMarketDataClient(http_client)
        assert await client.ping() is True


async def test_get_klines_passes_params_and_parses_response():
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert dict(request.url.params) == {"symbol": "BTCUSDT", "interval": "1m", "limit": "5"}
        return httpx.Response(200, json=[[1700000000000, "50000.0", "50100.0", "49900.0", "50050.0", "10.5"]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http_client:
        client = BinanceMarketDataClient(http_client)
        result = await client.get_klines("BTCUSDT", "1m", 5)
        assert result[0][4] == "50050.0"


async def test_testnet_base_url_used_when_requested():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as http_client:
        client = BinanceMarketDataClient(http_client, testnet=True)
        assert "testnet" in client.base_url


async def test_http_error_raises_binance_rest_error():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "server error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http_client:
        client = BinanceMarketDataClient(http_client)
        with pytest.raises(BinanceRestError):
            await client.ping()
