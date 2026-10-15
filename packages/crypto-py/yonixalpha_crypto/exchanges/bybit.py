"""Bybit V5 public spot market data (no key): instruments-info, kline,
tickers, time. Spot quantity precision is lotSizeFilter.basePrecision and
the minimum order is lotSizeFilter.minOrderAmt (quote); Bybit marks the spot
minOrderQty as deprecated. Kline rows are [start ms, open, high, low, close,
volume, turnover]; they are re-sorted here whatever order Bybit returns."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from yonixalpha_crypto.candles import Candle, ms
from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook, dec, split_symbol
from yonixalpha_crypto.publichttp import ExchangeUnavailable, PublicHttp

INTERVALS = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240",
             "1d": "D"}


class BybitSpot:
    exchange = "bybit"

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.http = PublicHttp("bybit", base_url, transport=transport)

    async def _get(self, path: str, params: dict) -> dict:
        body = await self.http.get(path, params)
        if not isinstance(body, dict) or body.get("retCode") != 0:
            raise ExchangeUnavailable("bybit", 200, f"retCode {body.get('retCode') if isinstance(body, dict) else '?'}: "
                                      f"{(body or {}).get('retMsg') if isinstance(body, dict) else body}")
        return body

    async def ping(self) -> None:
        await self._get("/v5/market/time", {})

    async def instrument(self, symbol: str) -> InstrumentSpec:
        base, quote = split_symbol(symbol)
        body = await self._get("/v5/market/instruments-info", {"category": "spot", "symbol": base + quote})
        items = (body.get("result") or {}).get("list") or []
        if not items:
            raise ExchangeUnavailable("bybit", 200, f"{symbol} is not listed on Bybit spot")
        it = items[0]
        lot, pf = it.get("lotSizeFilter") or {}, it.get("priceFilter") or {}
        step = dec(lot.get("basePrecision") or "0")
        return InstrumentSpec("bybit", f"{it['baseCoin']}/{it['quoteCoin']}", it["symbol"], it["baseCoin"],
                              it["quoteCoin"], it.get("status", ""), it.get("status") == "Trading",
                              dec(pf.get("tickSize") or "0"), step,
                              dec(lot.get("minOrderQty") or step), dec(lot.get("minOrderAmt") or "0"))

    async def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        base, quote = split_symbol(symbol)
        body = await self._get("/v5/market/kline", {"category": "spot", "symbol": base + quote,
                                                    "interval": INTERVALS[interval], "limit": min(int(limit), 1000)})
        rows = (body.get("result") or {}).get("list") or []
        out = [Candle(ms(r[0]), dec(r[1]), dec(r[2]), dec(r[3]), dec(r[4]), dec(r[5]), interval) for r in rows]
        return sorted(out, key=lambda c: c.open_time)

    async def top_of_book(self, symbol: str) -> TopOfBook:
        base, quote = split_symbol(symbol)
        body = await self._get("/v5/market/tickers", {"category": "spot", "symbol": base + quote})
        items = (body.get("result") or {}).get("list") or []
        if not items or not items[0].get("bid1Price") or not items[0].get("ask1Price"):
            raise ExchangeUnavailable("bybit", 200, f"no bid/ask for {symbol}")
        at = ms(body["time"]) if body.get("time") else datetime.now(timezone.utc)
        it = items[0]
        return TopOfBook("bybit", f"{base}/{quote}", dec(it["bid1Price"]), dec(it["ask1Price"]), at,
                         dec(it["bid1Size"]) if it.get("bid1Size") else None,
                         dec(it["ask1Size"]) if it.get("ask1Size") else None)

    async def aclose(self) -> None:
        await self.http.aclose()

