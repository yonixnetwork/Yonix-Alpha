"""Builds the LIVE execution providers from settings. A provider without
credentials is still built (so health and readiness can say NOT
CONFIGURED); every signed call on it raises NotConfigured."""

from typing import Any

import httpx

from yonixalpha_core.execution.binance import BinanceProvider
from yonixalpha_core.execution.bybit import BybitProvider
from yonixalpha_core.execution.hyperliquid import HyperliquidProvider
from yonixalpha_core.execution.mt5_bridge import MT5BridgeProvider

# venue -> ExecutionOrder.provider / PaperPosition.execution_provider value
PROVIDER_NAME = {"binance": "binance_futures", "bybit": "bybit_linear", "hyperliquid": "hyperliquid_perps",
                 "mt5": "mt5_bridge"}
FUTURES_PROVIDERS = tuple(PROVIDER_NAME.values())


def _secret(v: Any) -> str | None:
    if v is None:
        return None
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)


def build_providers(client: httpx.AsyncClient, settings: Any) -> dict[str, Any]:
    g = lambda name, default=None: getattr(settings, name, default)  # noqa: E731
    return {
        "binance": BinanceProvider(client, g("BINANCE_API_KEY"), _secret(g("BINANCE_API_SECRET")), bool(g("BINANCE_TESTNET", True))),
        "bybit": BybitProvider(client, g("BYBIT_API_KEY"), _secret(g("BYBIT_API_SECRET")), bool(g("BYBIT_TESTNET", False))),
        "hyperliquid": HyperliquidProvider(client, g("HYPERLIQUID_ACCOUNT_ADDRESS"), g("HYPERLIQUID_API_WALLET_PRIVATE_KEY"),
                                           bool(g("HYPERLIQUID_TESTNET", False))),
        "mt5": MT5BridgeProvider(client, g("MT5_BRIDGE_URL"), g("MT5_BRIDGE_TOKEN")),
    }
