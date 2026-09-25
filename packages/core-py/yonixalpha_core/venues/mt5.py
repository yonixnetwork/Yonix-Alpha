"""MT5 market data through services/mt5-bridge (the terminal's own rates
and depth of market), so FX strategies are assessed on the same broker
feed they would execute on.

- klines: GET /rates/{symbol}?interval=&limit= (copy_rates_from_pos);
- book:   GET /book/{symbol} (market_book_get), volumes converted to base
  units by the bridge. Many brokers publish no depth of market for FX; then
  the bridge answers 404 and there is NO book — the gate cannot size or
  cost the trade and returns NO_TRADE. Depth is never invented from a
  bid/ask quote.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.safety.liquidity import OrderBookModel, book_from_levels
from yonixalpha_core.venues.common import Candle, NotConfigured, VenueError, dec, ms_to_dt, tracked

INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


class MT5Market:
    venue = "mt5"

    def __init__(self, client: httpx.AsyncClient, base_url: str | None, token: Any = None,
                 commission_bps: Decimal = Decimal(0)):
        self.client = client
        self.base = (base_url or "").rstrip("/")
        self._token = token
        # Commission per side in bps of notional; spread is in the book itself.
        self.taker_fee_bps = commission_bps

    def _secret(self) -> str:
        t = self._token
        return "" if t is None else (t.get_secret_value() if hasattr(t, "get_secret_value") else str(t))

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not (self.base and self._secret()):
            raise NotConfigured("MT5_BRIDGE_URL / MT5_BRIDGE_TOKEN not set")

        async def go():
            try:
                resp = await self.client.get(f"{self.base}{path}", params=params, timeout=10.0,
                                             headers={"Authorization": f"Bearer {self._secret()}"})
            except httpx.HTTPError as exc:
                raise VenueError(f"mt5-bridge {path}: transport {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise VenueError(f"mt5-bridge {path}: HTTP {resp.status_code} {resp.text[:160]}")
            return resp.json()
        return await tracked("mt5", go())

    async def klines(self, symbol: str, interval: str, limit: int = 200, now: datetime | None = None) -> list[Candle]:
        if interval not in INTERVALS:
            raise VenueError(f"unsupported interval {interval}")
        rows = await self._get(f"/rates/{symbol}", {"interval": interval, "limit": limit})
        now = now or datetime.now(timezone.utc)
        out = [Candle(ms_to_dt(r["open_time_ms"]), Decimal(str(r["open"])), Decimal(str(r["high"])), Decimal(str(r["low"])),
                      Decimal(str(r["close"])), Decimal(str(r.get("volume", 0))), ms_to_dt(r["close_time_ms"]) <= now)
               for r in rows or []]
        if not out:
            raise VenueError("mt5 rates: empty")
        return out

    async def book(self, symbol: str, limit: int = 100) -> OrderBookModel:
        body = await self._get(f"/book/{symbol}")
        try:
            return book_from_levels(body["bids"][:limit], body["asks"][:limit], self.taker_fee_bps)
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(f"mt5 book: {exc}") from exc

    async def mid(self, symbol: str) -> Decimal:
        body = await self._get(f"/tick/{symbol}")
        bid, ask = dec(body.get("bid")), dec(body.get("ask"))
        if not bid or not ask:
            raise VenueError(f"mt5 tick: no bid/ask for {symbol}")
        return (bid + ask) / 2
