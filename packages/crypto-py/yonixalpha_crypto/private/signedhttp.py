"""The signing HTTP client for exchange accounts.

- The host comes from environments (fixed per exchange and environment).
- Only the (method, path) pairs listed here can be sent. Account reads and
  order management are listed; transfer, withdrawal, deposit-address,
  sub-account and key-management endpoints are not, so they are refused
  locally and never reach the exchange.
- A request that can change a LIVE account (ORDER_PATHS) first passes
  livelock.require_live_orders(); with the code lock on, it always raises.
- Keys live only in this object; repr and every error message are scrubbed.

Signing follows the official documentation, the same scheme the removed
providers used (git 7e87552^: yonixalpha_core/execution/{bybit,binance}.py):
- Bybit V5: HMAC-SHA256(secret, timestamp + api_key + recv_window + payload),
  payload = the query string (GET) or the JSON body (POST); headers
  X-BAPI-API-KEY / -TIMESTAMP / -RECV-WINDOW / -SIGN; retCode 0 = success.
- Binance: HMAC-SHA256(secret, query string with timestamp and recvWindow),
  sent as `signature`; header X-MBX-APIKEY.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from yonixalpha_crypto.private import environments, livelock
from yonixalpha_crypto.private.environments import LIVE

RECV_WINDOW = 5000

READ_PATHS: dict[str, frozenset[tuple[str, str]]] = {
    "bybit": frozenset({
        ("GET", "/v5/market/time"), ("GET", "/v5/market/instruments-info"),
        ("GET", "/v5/user/query-api"), ("GET", "/v5/account/wallet-balance"), ("GET", "/v5/account/info"),
        ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history"), ("GET", "/v5/position/list"),
        ("GET", "/v5/execution/list"),
    }),
    "binance": frozenset({
        ("GET", "/api/v3/time"), ("GET", "/api/v3/exchangeInfo"), ("GET", "/api/v3/account"),
        ("GET", "/api/v3/openOrders"), ("GET", "/api/v3/allOrders"), ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/order"), ("GET", "/sapi/v1/account/apiRestrictions"),
    }),
}
ORDER_PATHS: dict[str, frozenset[tuple[str, str]]] = {
    "bybit": frozenset({
        ("POST", "/v5/order/create"), ("POST", "/v5/order/amend"), ("POST", "/v5/order/cancel"),
        ("POST", "/v5/position/trading-stop"), ("POST", "/v5/position/set-leverage"),
    }),
    "binance": frozenset({
        ("POST", "/api/v3/order"), ("DELETE", "/api/v3/order"), ("POST", "/api/v3/order/cancelReplace"),
        ("POST", "/api/v3/orderList/oco"), ("POST", "/api/v3/orderList/oto"), ("DELETE", "/api/v3/orderList"),
    }),
}
UNSIGNED = frozenset({"/v5/market/time", "/v5/market/instruments-info", "/api/v3/time", "/api/v3/exchangeInfo"})
LIVE_ONLY = frozenset({"/sapi/v1/account/apiRestrictions"})  # /sapi does not exist on the Binance testnet


class BlockedRequest(RuntimeError):
    """Not on the allowlist (or not available in this environment). Never sent."""


class ExchangeRequestError(RuntimeError):
    def __init__(self, exchange: str, status: int | None, code: int | None, message: str):
        super().__init__(f"{exchange}: HTTP {status or '-'} code {code if code is not None else '-'}: {message}")
        self.exchange, self.status, self.code, self.message = exchange, status, code, message


def check(exchange: str, environment: str, method: str, path: str) -> bool:
    """True for an order path, False for a read path; raises BlockedRequest otherwise."""
    key = (method.upper(), path)
    if key in ORDER_PATHS.get(exchange, ()):
        return True
    if key not in READ_PATHS.get(exchange, ()):
        raise BlockedRequest(f"{exchange}: {method.upper()} {path} is not an allowed account request")
    if path in LIVE_ONLY and environment != LIVE:
        raise BlockedRequest(f"{exchange}: {path} does not exist in {environment}")
    return False


class SignedHttp:
    def __init__(self, exchange: str, environment: str, api_key: str, api_secret: str, *,
                 connection: dict[str, Any] | None = None, timeout_s: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time):
        self.env = environments.get(exchange, environment)
        self.exchange, self.environment = exchange, environment
        self._key, self._secret = api_key, api_secret
        self._connection = connection  # the saved connection view, for the live gates
        self._clock = clock
        self._client = httpx.AsyncClient(base_url=self.env.base_url, timeout=timeout_s, transport=transport,
                                         headers={"user-agent": "yonixalpha-crypto/0.1"})

    def __repr__(self) -> str:
        return f"SignedHttp({self.exchange}, {self.environment}, key={'…' + self._key[-4:] if self._key else '-'})"

    def scrub(self, text: str) -> str:
        for s in (self._secret, self._key):
            if s:
                text = text.replace(s, "[redacted]")
        return re.sub(r"(signature|sign)=[0-9a-fA-F]+", r"\1=[redacted]", text)[:300]

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        is_order = check(self.exchange, self.environment, method, path)
        if is_order and self.environment == LIVE:
            livelock.require_live_orders(self._connection)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            if self.exchange == "bybit":
                return await self._bybit(method, path, params)
            return await self._binance(method, path, params)
        except httpx.HTTPError as e:
            raise ExchangeRequestError(self.exchange, None, None, type(e).__name__) from None

    async def _bybit(self, method: str, path: str, params: dict[str, Any]) -> Any:
        ts = str(int(self._clock() * 1000))
        if method == "GET":
            payload = urlencode(params)
            url, body = (f"{path}?{payload}" if payload else path), None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url, body = path, payload
        headers = {"content-type": "application/json"}
        if path not in UNSIGNED:
            sign = hmac.new(self._secret.encode(), f"{ts}{self._key}{RECV_WINDOW}{payload}".encode(),
                            hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW), "X-BAPI-SIGN": sign})
        r = await self._client.request(method, url, content=body, headers=headers)
        try:
            data = r.json()
        except ValueError:
            raise ExchangeRequestError("bybit", r.status_code, None, self.scrub(r.text or "no body")) from None
        if r.status_code >= 400 or not isinstance(data, dict) or data.get("retCode") != 0:
            code = data.get("retCode") if isinstance(data, dict) else None
            msg = data.get("retMsg", "") if isinstance(data, dict) else str(data)
            raise ExchangeRequestError("bybit", r.status_code, code, self.scrub(str(msg)))
        return data.get("result")

    async def _binance(self, method: str, path: str, params: dict[str, Any]) -> Any:
        headers = {}
        if path in UNSIGNED:
            query = urlencode(params)
        else:
            query = urlencode({**params, "timestamp": int(self._clock() * 1000), "recvWindow": RECV_WINDOW})
            sig = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={sig}"
            headers["X-MBX-APIKEY"] = self._key
        r = await self._client.request(method, f"{path}?{query}" if query else path, headers=headers)
        try:
            data = r.json()
        except ValueError:
            raise ExchangeRequestError("binance", r.status_code, None, self.scrub(r.text or "no body")) from None
        if r.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg", "") if isinstance(data, dict) else str(data)
            raise ExchangeRequestError("binance", r.status_code, code, self.scrub(str(msg)))
        return data

    async def aclose(self) -> None:
        await self._client.aclose()
