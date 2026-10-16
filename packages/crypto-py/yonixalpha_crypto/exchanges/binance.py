"""Binance spot public market data (no key): exchangeInfo, klines,
bookTicker, ping. The same REST shape serves binance.com (`binance`) and
Binance.US (`binance_us`), but they are separate exchanges: each has its
own profile, listings, fees and reachability. Precision comes from the
PRICE_FILTER (tickSize), LOT_SIZE (stepSize, minQty) and NOTIONAL /
MIN_NOTIONAL (minNotional) filters. Kline rows are [open ms, open, high,
low, close, volume, close ms, ...]."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from yonixalpha_crypto.candles import Candle, ms
from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook, dec, split_symbol
from yonixalpha_crypto.publichttp import ExchangeUnavailable, PublicHttp

INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}


def instrument_from(exchange: str, s: dict) -> InstrumentSpec:
    """One symbol of /api/v3/exchangeInfo (also used by the signed account
    adapter, private.adapters)."""
    f = {x.get("filterType"): x for x in s.get("filters") or []}
    notional = (f.get("NOTIONAL") or {}).get("minNotional") or (f.get("MIN_NOTIONAL") or {}).get("minNotional") or "0"
    return InstrumentSpec(exchange, f"{s['baseAsset']}/{s['quoteAsset']}", s["symbol"], s["baseAsset"],
                          s["quoteAsset"], s.get("status", ""), s.get("status") == "TRADING",
                          dec((f.get("PRICE_FILTER") or {}).get("tickSize") or "0"),
                          dec((f.get("LOT_SIZE") or {}).get("stepSize") or "0"),
                          dec((f.get("LOT_SIZE") or {}).get("minQty") or "0"), dec(notional))


class BinanceSpot:
    def __init__(self, exchange: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        if exchange not in ("binance", "binance_us"):
            raise ValueError(exchange)
        self.exchange = exchange
        self.http = PublicHttp(exchange, base_url, transport=transport)

    async def ping(self) -> None:
        await self.http.get("/api/v3/ping")

    async def instrument(self, symbol: str) -> InstrumentSpec:
        base, quote = split_symbol(symbol)
        body = await self.http.get("/api/v3/exchangeInfo", {"symbol": base + quote})
        syms = (body.get("symbols") or []) if isinstance(body, dict) else []
        if not syms:
            raise ExchangeUnavailable(self.exchange, 200, f"{symbol} is not listed")
        return instrument_from(self.exchange, syms[0])

    async def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval not in INTERVALS:
            raise ValueError(interval)
        base, quote = split_symbol(symbol)
        rows = await self.http.get("/api/v3/klines", {"symbol": base + quote, "interval": interval,
                                                      "limit": min(int(limit), 1000)})
        out = [Candle(ms(r[0]), dec(r[1]), dec(r[2]), dec(r[3]), dec(r[4]), dec(r[5]), interval) for r in rows or []]
        return sorted(out, key=lambda c: c.open_time)

    async def top_of_book(self, symbol: str) -> TopOfBook:
        base, quote = split_symbol(symbol)
        b = await self.http.get("/api/v3/ticker/bookTicker", {"symbol": base + quote})
        if not b or not b.get("bidPrice") or not b.get("askPrice"):
            raise ExchangeUnavailable(self.exchange, 200, f"no bid/ask for {symbol}")
        return TopOfBook(self.exchange, f"{base}/{quote}", dec(b["bidPrice"]), dec(b["askPrice"]),
                         datetime.now(timezone.utc), dec(b["bidQty"]) if b.get("bidQty") else None,
                         dec(b["askQty"]) if b.get("askQty") else None)

    async def aclose(self) -> None:
        await self.http.aclose()
