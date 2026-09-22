import time

import httpx

from yonixalpha_core.logging import get_logger

from app.auth import build_signed_query

log = get_logger("engine-binance-futures.client")

# Same base-URL caveat as services/data-binance: implemented against
# Binance's long-stable fapi/fstream split between production and testnet,
# not live-verified in this environment.
PRODUCTION_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

DEFAULT_RECV_WINDOW_MS = 5000


class BinanceApiError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Binance API error {status_code}: {body}")


class BinanceFuturesClient:
    """Authenticated USDT-M Futures REST client: account, positions,
    leverage, orders, income, and user-data-stream listenKey management.
    Deliberately separate from services/data-binance's public-only client
    (Phase 2) — this one holds real trading credentials, and keeping the
    two apart limits which process ever needs them.
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str, api_secret: str, testnet: bool = True):
        self._client = client
        self._api_key = api_key
        self._api_secret = api_secret
        self.base_url = TESTNET_BASE_URL if testnet else PRODUCTION_BASE_URL

    async def _request(self, method: str, url: str, headers: dict) -> dict:
        try:
            response = await self._client.request(method, url, headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            log.error("client.request_failed", method=method, url=url, error=str(exc))
            raise BinanceApiError(0, str(exc)) from exc
        if response.status_code >= 400:
            log.error("client.error_response", method=method, url=url, status=response.status_code, body=response.text)
            raise BinanceApiError(response.status_code, response.text)
        return response.json()

    async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", DEFAULT_RECV_WINDOW_MS)
        query = build_signed_query(params, self._api_secret)
        url = f"{self.base_url}{path}?{query}"
        return await self._request(method, url, {"X-MBX-APIKEY": self._api_key})

    async def _keyed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        """listenKey endpoints: API key header only, no signature/timestamp."""
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
        return await self._request(method, url, {"X-MBX-APIKEY": self._api_key})

    # -- Account / positions --------------------------------------------

    async def get_account(self) -> dict:
        return await self._signed_request("GET", "/fapi/v2/account")

    async def get_position_risk(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return await self._signed_request("GET", "/fapi/v2/positionRisk", params)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        return await self._signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})

    # -- Orders ------------------------------------------------------------

    async def place_order(self, **params) -> dict:
        """`params` should include symbol, side, type, quantity/price/
        stopPrice as applicable, and newClientOrderId — the caller (see
        app/orders.py) is responsible for the idempotency key, this method
        just forwards whatever it's given.
        """
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def cancel_order(
        self, symbol: str, orig_client_order_id: str | None = None, order_id: str | None = None
    ) -> dict:
        params: dict = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        if order_id:
            params["orderId"] = order_id
        return await self._signed_request("DELETE", "/fapi/v1/order", params)

    async def get_order(
        self, symbol: str, orig_client_order_id: str | None = None, order_id: str | None = None
    ) -> dict:
        params: dict = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        if order_id:
            params["orderId"] = order_id
        return await self._signed_request("GET", "/fapi/v1/order", params)

    async def get_open_orders(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return await self._signed_request("GET", "/fapi/v1/openOrders", params)

    # -- Income (realized PnL, funding fees, commission) --------------------

    async def get_income(
        self, symbol: str | None = None, income_type: str | None = None, start_time: int | None = None, limit: int = 100
    ) -> list:
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = start_time
        return await self._signed_request("GET", "/fapi/v1/income", params)

    # -- User data stream (listenKey) ---------------------------------------

    async def start_user_data_stream(self) -> str:
        result = await self._keyed_request("POST", "/fapi/v1/listenKey")
        return result["listenKey"]

    async def keepalive_user_data_stream(self) -> None:
        await self._keyed_request("PUT", "/fapi/v1/listenKey")

    async def close_user_data_stream(self) -> None:
        await self._keyed_request("DELETE", "/fapi/v1/listenKey")
