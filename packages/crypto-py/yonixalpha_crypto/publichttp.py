"""The only HTTP client in the Crypto Trading engine. It can read public
market data and nothing else:

- GET only;
- only the listed public market-data paths of each exchange;
- no signature / API-key query parameters and no API-key or auth headers.

Anything else raises RealOrderBlocked BEFORE a request is built, so an order
endpoint, a signed request or a credential can never reach an exchange from
this package (tests/test_demo_guard.py). There is no other client.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

PUBLIC_PATHS: dict[str, frozenset[str]] = {
    "bybit": frozenset({"/v5/market/time", "/v5/market/instruments-info", "/v5/market/kline",
                        "/v5/market/tickers", "/v5/market/orderbook"}),
    "binance": frozenset({"/api/v3/ping", "/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/klines",
                          "/api/v3/ticker/bookTicker", "/api/v3/depth"}),
}
PUBLIC_PATHS["binance_us"] = PUBLIC_PATHS["binance"]

FORBIDDEN_PARAMS = frozenset({"signature", "sign", "apikey", "api_key", "recvwindow", "recv_window"})
FORBIDDEN_HEADERS = frozenset({"authorization", "x-mbx-apikey", "x-bapi-api-key", "x-bapi-sign",
                               "x-bapi-timestamp", "x-bapi-recv-window", "x-bapi-sign-type"})


class RealOrderBlocked(RuntimeError):
    """A request that is not public market data. Never sent."""


class ExchangeUnavailable(RuntimeError):
    def __init__(self, exchange: str, status: int | None, detail: str, retry_after: float | None = None):
        super().__init__(f"{exchange}: {status or 'no response'} {detail}")
        self.exchange, self.status, self.detail, self.retry_after = exchange, status, detail, retry_after


@dataclass
class CallRecord:
    exchange: str
    path: str
    status: int | None
    ms: float
    error: str | None = None


def check_request(exchange: str, method: str, path: str, params: dict[str, Any] | None,
                  headers: dict[str, str] | None) -> None:
    if method.upper() != "GET":
        raise RealOrderBlocked(f"{exchange}: {method} is not allowed (public market data is GET only)")
    allowed = PUBLIC_PATHS.get(exchange)
    if allowed is None:
        raise RealOrderBlocked(f"unknown exchange {exchange!r}")
    if path not in allowed:
        raise RealOrderBlocked(f"{exchange}: {path} is not a public market-data path")
    bad = {k for k in (params or {}) if k.lower() in FORBIDDEN_PARAMS}
    if bad:
        raise RealOrderBlocked(f"{exchange}: signed / keyed parameters refused: {sorted(bad)}")
    badh = {k for k in (headers or {}) if k.lower() in FORBIDDEN_HEADERS}
    if badh:
        raise RealOrderBlocked(f"{exchange}: credential headers refused: {sorted(badh)}")


class PublicHttp:
    """One client per exchange. `min_interval_s` spaces requests (each
    exchange's public limits are far above this engine's needs); 429 / 418 /
    5xx become ExchangeUnavailable with the server's Retry-After."""

    def __init__(self, exchange: str, base_url: str, *, timeout_s: float = 10.0, min_interval_s: float = 0.2,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.exchange = exchange
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport,
                                         headers={"user-agent": "yonixalpha-crypto/0.1 (public market data)"})
        self._min_interval = min_interval_s
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.calls: list[CallRecord] = []  # the last few calls, for health reporting

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        check_request(self.exchange, "GET", path, params, None)
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        t0 = time.monotonic()
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            self._record(path, None, t0, f"{type(e).__name__}")
            raise ExchangeUnavailable(self.exchange, None, type(e).__name__) from e
        self._record(path, r.status_code, t0, None if r.status_code < 400 else r.text[:200])
        if r.status_code in (418, 429) or r.status_code >= 500:
            ra = r.headers.get("retry-after")
            raise ExchangeUnavailable(self.exchange, r.status_code, r.text[:200],
                                      float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if r.status_code >= 400:
            raise ExchangeUnavailable(self.exchange, r.status_code, r.text[:200])
        return r.json()

    def _record(self, path: str, status: int | None, t0: float, error: str | None) -> None:
        self.calls.append(CallRecord(self.exchange, path, status, round((time.monotonic() - t0) * 1000, 1), error))
        del self.calls[:-20]

    async def aclose(self) -> None:
        await self._client.aclose()
