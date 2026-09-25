"""Binance USDⓈ-M futures public market data (no key). Paths match the
official binance-futures-connector-python (um_futures/market.py)."""

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from yonixalpha_core.safety.liquidity import OrderBookModel, book_from_levels
from yonixalpha_core.solana.market_data import RateBudget
from yonixalpha_core.venues.common import (
    DEFAULT_TAKER_FEE_BPS,
    Candle,
    Ticker,
    VenueError,
    budgeted,
    dec,
    ms_to_dt,
    raise_for,
    tracked,
)

BASE = "https://fapi.binance.com"
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}


class BinanceFuturesPublic:
    venue = "binance"

    def __init__(self, client: httpx.AsyncClient, budget: RateBudget, base: str = BASE,
                 taker_fee_bps: Decimal = DEFAULT_TAKER_FEE_BPS["binance"]):
        self.client, self.budget, self.base, self.taker_fee_bps = client, budget, base, taker_fee_bps

    async def _get(self, path: str, params: dict) -> object:
        return await tracked("binance", self._get_raw(path, params))

    async def _get_raw(self, path: str, params: dict) -> object:
        await budgeted(self.budget, "binance")
        try:
            resp = await self.client.get(f"{self.base}{path}", params=params, timeout=10.0)
        except httpx.HTTPError as exc:
            raise VenueError(f"binance {path}: transport {exc!r}") from exc
        return raise_for(resp, f"binance {path}")

    async def klines(self, symbol: str, interval: str, limit: int = 200, now: datetime | None = None) -> list[Candle]:
        if interval not in INTERVALS:
            raise VenueError(f"unsupported interval {interval}")
        rows = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        now_ms = (now or datetime.now(timezone.utc)).timestamp() * 1000
        out = []
        for r in rows if isinstance(rows, list) else []:
            # [openTime, open, high, low, close, volume, closeTime, ...]
            out.append(Candle(ms_to_dt(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5]),
                              closed=int(r[6]) < now_ms))
        if not out:
            raise VenueError("binance klines: empty")
        return out

    async def book(self, symbol: str, limit: int = 100) -> OrderBookModel:
        body = await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        try:
            return book_from_levels(body["bids"], body["asks"], self.taker_fee_bps)
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(f"binance depth: {exc}") from exc

    async def ticker(self, symbol: str) -> Ticker:
        prem = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = await self._get("/fapi/v1/openInterest", {"symbol": symbol})
        t = await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        return Ticker(symbol, datetime.now(timezone.utc), dec(t.get("lastPrice")), dec(prem.get("markPrice")),
                      dec(prem.get("lastFundingRate")), dec(oi.get("openInterest")), volume_24h=dec(t.get("quoteVolume")))
