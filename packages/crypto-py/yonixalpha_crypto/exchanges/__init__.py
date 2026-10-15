"""Market-data adapters: one per exchange, all public and read-only."""

from __future__ import annotations

import httpx

from yonixalpha_crypto.exchanges.base import MarketData
from yonixalpha_crypto.exchanges.binance import BinanceSpot
from yonixalpha_crypto.exchanges.bybit import BybitSpot
from yonixalpha_crypto.exchanges.profiles import EXCHANGES, PROFILES, profile


def market_data(exchange: str, base_url: str | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> MarketData:
    url = profile(exchange, base_url).base_url
    if exchange == "bybit":
        return BybitSpot(url, transport)
    return BinanceSpot(exchange, url, transport)


__all__ = ["EXCHANGES", "PROFILES", "market_data", "profile"]
