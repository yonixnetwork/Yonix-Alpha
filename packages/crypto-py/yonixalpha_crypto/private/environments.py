"""The host of each exchange account environment. Fixed in code: no
environment variable or database value can point a key somewhere else,
and a failed environment never falls back to another one.

Sources (official documentation, checked 2026-10-16):
- Bybit demo trading: https://bybit-exchange.github.io/docs/v5/demo
  (keys are made on the mainnet account in Demo Trading; not every
  endpoint exists there).
- Bybit testnet: a separate site with its own keys (testnet.bybit.com).
- Binance spot Demo Mode: https://developers.binance.com/en/docs/products/spot/demo-mode/general-info
- Binance spot Testnet: https://developers.binance.com/en/docs/products/spot/testnet/general-info
  (/api only: /sapi endpoints do not exist there).
Binance.US is a different company with its own accounts and is not listed:
it needs the operator's approval first.
"""

from __future__ import annotations

from dataclasses import dataclass

LIVE = "LIVE"
DEMO = "DEMO"
TESTNET = "TESTNET"
ENVIRONMENTS = (DEMO, TESTNET, LIVE)
PRACTICE = frozenset({DEMO, TESTNET})  # exchange-run practice money, no real funds


@dataclass(frozen=True)
class AccountEnvironment:
    exchange: str
    environment: str
    base_url: str
    label: str
    note: str


_ALL = (
    AccountEnvironment("bybit", DEMO, "https://api-demo.bybit.com", "Bybit Demo Trading",
                       "Demo key made on the mainnet account (Demo Trading). Some endpoints are missing."),
    AccountEnvironment("bybit", TESTNET, "https://api-testnet.bybit.com", "Bybit Testnet",
                       "Separate testnet account and key."),
    AccountEnvironment("bybit", LIVE, "https://api.bybit.com", "Bybit LIVE", "Real funds."),
    AccountEnvironment("binance", DEMO, "https://demo-api.binance.com", "Binance Spot Demo Mode",
                       "Demo key from Binance Demo Trading; mirrors live features."),
    AccountEnvironment("binance", TESTNET, "https://testnet.binance.vision", "Binance Spot Testnet",
                       "Testnet key; /api only, balances reset monthly."),
    AccountEnvironment("binance", LIVE, "https://api.binance.com", "Binance LIVE", "Real funds."),
)
ACCOUNT_ENVIRONMENTS: dict[tuple[str, str], AccountEnvironment] = {(e.exchange, e.environment): e for e in _ALL}


class UnknownEnvironment(ValueError):
    pass


def get(exchange: str, environment: str) -> AccountEnvironment:
    try:
        return ACCOUNT_ENVIRONMENTS[(exchange, environment)]
    except KeyError:
        raise UnknownEnvironment(f"{exchange} / {environment} is not a supported account environment") from None
