"""One place that builds the venue adapters from settings, each with its own
client-side rate budget (well under the venues' published public limits)."""

from typing import Any

import httpx

from yonixalpha_core.solana.market_data import RateBudget
from yonixalpha_core.venues.binance_public import BinanceFuturesPublic
from yonixalpha_core.venues.bybit import BybitClient
from yonixalpha_core.venues.hyperliquid import HyperliquidInfo

VENUE_ENGINE = {"binance": "binance_futures", "bybit": "bybit_futures", "hyperliquid": "hyperliquid_perps"}
ENGINE_VENUE = {v: k for k, v in VENUE_ENGINE.items()}


def build_venues(client: httpx.AsyncClient, settings: Any) -> dict[str, Any]:
    return {
        "binance": BinanceFuturesPublic(client, RateBudget(120)),
        "bybit": BybitClient(client, RateBudget(120), getattr(settings, "BYBIT_API_KEY", None),
                             getattr(settings, "BYBIT_API_SECRET", None), bool(getattr(settings, "BYBIT_TESTNET", False))),
        "hyperliquid": HyperliquidInfo(client, RateBudget(120), getattr(settings, "HYPERLIQUID_ACCOUNT_ADDRESS", None),
                                       bool(getattr(settings, "HYPERLIQUID_TESTNET", False))),
    }
