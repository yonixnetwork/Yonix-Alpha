import httpx

from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error

log = get_logger("data-binance.rest")

# USDT-M Futures public market-data endpoints (no API key required — this
# service only ever reads public market data; authenticated account/order
# endpoints are Phase 4's concern). Base URLs per Binance's long-stable
# fapi/fstream split between production and testnet — not live-verified in
# this environment (see ARCHITECTURE_AUDIT.md's network limitation note);
# confirm against current Binance Futures API docs before relying on this
# in production.
PRODUCTION_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://testnet.binancefuture.com"


class BinanceRestError(Exception):
    pass


class BinanceMarketDataClient:
    """Thin wrapper over Binance USDT-M Futures public REST endpoints.
    Every method returns the parsed JSON body as-is (never reshaped here —
    normalization into NormalizedMarketEvent happens in app/ingest.py, kept
    separate so this client stays a faithful, inspectable mirror of what
    Binance actually returned).
    """

    def __init__(self, client: httpx.AsyncClient, testnet: bool = False):
        self._client = client
        self.base_url = TESTNET_BASE_URL if testnet else PRODUCTION_BASE_URL

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self.base_url}{path}"
        try:
            response = await self._client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            log.error("rest.request_failed", url=url, error=str(exc))
            await alert_error("data-binance", "rest.request_failed", {"url": url, "error": str(exc)})
            raise BinanceRestError(f"GET {path} failed: {exc}") from exc

    async def ping(self) -> bool:
        await self._get("/fapi/v1/ping")
        return True

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> list:
        return await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def get_depth(self, symbol: str, limit: int = 20) -> dict:
        return await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    async def get_ticker_24hr(self, symbol: str) -> dict:
        return await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def get_mark_price(self, symbol: str) -> dict:
        """markPrice, indexPrice, lastFundingRate, nextFundingTime."""
        return await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})

    async def get_open_interest(self, symbol: str) -> dict:
        """openInterest, symbol, time — public, no API key needed."""
        return await self._get("/fapi/v1/openInterest", {"symbol": symbol})
