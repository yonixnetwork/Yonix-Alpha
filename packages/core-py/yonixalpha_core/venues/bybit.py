"""Bybit V5 adapter: public market data plus read-only account state when
BYBIT_API_KEY/BYBIT_API_SECRET are set. Paths and HMAC signing follow the
official pybit SDK (sign = HMAC_SHA256(secret, timestamp + api_key +
recv_window + sorted query string), headers X-BAPI-*)."""

import hashlib
import hmac
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.safety.liquidity import OrderBookModel, book_from_levels
from yonixalpha_core.solana.market_data import RateBudget
from yonixalpha_core.venues.common import (
    DEFAULT_TAKER_FEE_BPS,
    Candle,
    NotConfigured,
    Ticker,
    VenueError,
    budgeted,
    dec,
    ms_to_dt,
    raise_for,
)

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
RECV_WINDOW = 5000
INTERVALS = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "1d": "D"}
INTERVAL_MS = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000, "30": 1_800_000, "60": 3_600_000,
               "120": 7_200_000, "240": 14_400_000, "D": 86_400_000}


def sign(secret: str, timestamp: str, api_key: str, recv_window: int, query: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def query_string(params: dict[str, Any]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)


class BybitClient:
    venue = "bybit"

    def __init__(self, client: httpx.AsyncClient, budget: RateBudget, api_key: str | None = None,
                 api_secret: str | None = None, testnet: bool = False,
                 taker_fee_bps: Decimal = DEFAULT_TAKER_FEE_BPS["bybit"]):
        self.client, self.budget = client, budget
        self.api_key, self.api_secret = api_key or None, api_secret or None
        self.base = TESTNET if testnet else MAINNET
        self.taker_fee_bps = taker_fee_bps

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def _get(self, path: str, params: dict[str, Any], auth: bool = False) -> Any:
        await budgeted(self.budget, "bybit")
        qs = query_string(params)
        headers = {}
        if auth:
            if not self.has_credentials:
                raise NotConfigured("BYBIT_API_KEY / BYBIT_API_SECRET not set")
            ts = str(int(time.time() * 1000))
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign(self.api_secret, ts, self.api_key, RECV_WINDOW, qs),
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
            }
        url = f"{self.base}{path}" + (f"?{qs}" if qs else "")
        try:
            resp = await self.client.get(url, headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            raise VenueError(f"bybit {path}: transport {type(exc).__name__}") from exc
        body = raise_for(resp, f"bybit {path}")
        if not isinstance(body, dict) or body.get("retCode") != 0:
            raise VenueError(f"bybit {path}: retCode {body.get('retCode') if isinstance(body, dict) else '?'} "
                             f"{(body.get('retMsg') if isinstance(body, dict) else '')}")
        return body.get("result") or {}

    # --- public -------------------------------------------------------------
    async def klines(self, symbol: str, interval: str, limit: int = 200, now: datetime | None = None) -> list[Candle]:
        code = INTERVALS.get(interval)
        if code is None:
            raise VenueError(f"unsupported interval {interval}")
        result = await self._get("/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": code, "limit": limit})
        now_ms = (now or datetime.now(timezone.utc)).timestamp() * 1000
        rows = sorted(result.get("list") or [], key=lambda r: int(r[0]))  # Bybit returns newest first
        out = [Candle(ms_to_dt(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5]),
                      closed=int(r[0]) + INTERVAL_MS[code] <= now_ms) for r in rows]
        if not out:
            raise VenueError("bybit kline: empty")
        return out

    async def book(self, symbol: str, limit: int = 200) -> OrderBookModel:
        result = await self._get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": limit})
        try:
            return book_from_levels(result["b"], result["a"], self.taker_fee_bps)
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(f"bybit orderbook: {exc}") from exc

    async def ticker(self, symbol: str) -> Ticker:
        result = await self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        rows = result.get("list") or []
        if not rows:
            raise VenueError(f"bybit tickers: {symbol} not found")
        t = rows[0]
        return Ticker(symbol, datetime.now(timezone.utc), dec(t.get("lastPrice")), dec(t.get("markPrice")),
                      dec(t.get("fundingRate")), dec(t.get("openInterest")), dec(t.get("bid1Price")), dec(t.get("ask1Price")),
                      dec(t.get("turnover24h")))

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        result = await self._get("/v5/market/recent-trade", {"category": "linear", "symbol": symbol, "limit": limit})
        return list(result.get("list") or [])

    async def funding_history(self, symbol: str, limit: int = 20) -> list[dict]:
        result = await self._get("/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": limit})
        return list(result.get("list") or [])

    async def open_interest(self, symbol: str, interval: str = "5min", limit: int = 50) -> list[dict]:
        result = await self._get("/v5/market/open-interest",
                                 {"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": limit})
        return list(result.get("list") or [])

    # --- account (read-only) ------------------------------------------------
    async def wallet_balance(self) -> dict:
        result = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, auth=True)
        rows = result.get("list") or []
        return rows[0] if rows else {}

    async def positions(self) -> list[dict]:
        return list((await self._get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"}, auth=True)).get("list") or [])

    async def open_orders(self) -> list[dict]:
        return list((await self._get("/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"}, auth=True)).get("list") or [])

    async def executions(self, limit: int = 50) -> list[dict]:
        return list((await self._get("/v5/execution/list", {"category": "linear", "limit": limit}, auth=True)).get("list") or [])

    async def closed_pnl(self, limit: int = 50) -> list[dict]:
        return list((await self._get("/v5/position/closed-pnl", {"category": "linear", "limit": limit}, auth=True)).get("list") or [])
