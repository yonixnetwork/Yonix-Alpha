"""Per-exchange profiles. Each exchange is configured on its own: the same
symbol on two exchanges is two different instruments with different fees,
precision and trading rules.

Fees are the PUBLIC base tier, not this account's verified rate (no account
is connected). Sources, checked 2026-10-15:
- Bybit spot non-VIP: 0.10% maker / 0.10% taker (Bybit help center, "Spot
  Trading: Fees Explained", updated 2026-09-10). Varies by VIP level and region.
- Binance spot Regular: 0.10% / 0.10% (third-party fee summaries; Binance's
  own page was not reachable here). Varies by VIP and BNB fee payment.
- Binance.US spot: 0% maker / 0.02% taker on all pairs (Binance.US blog,
  April 2026). Binance.US is a separate exchange from binance.com.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExchangeProfile:
    id: str
    label: str
    base_url: str
    market_type: str
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    fee_source: str
    note: str = ""


FEE_ASSUMED = "ASSUMED public base tier (not account-verified)"

PROFILES: dict[str, ExchangeProfile] = {
    "bybit": ExchangeProfile(
        "bybit", "Bybit (spot)", "https://api.bybit.com", "spot", Decimal("10"), Decimal("10"),
        f"{FEE_ASSUMED}: Bybit help center, non-VIP spot 0.10% / 0.10%",
        "Bybit refuses some locations (HTTP 403); this server got 403 on 2026-10-15"),
    "binance": ExchangeProfile(
        "binance", "Binance (binance.com spot)", "https://api.binance.com", "spot", Decimal("10"), Decimal("10"),
        f"{FEE_ASSUMED}: Regular tier 0.10% / 0.10% (third-party summaries)",
        "binance.com refuses some locations (HTTP 451); this server got 451 on 2026-10-15"),
    "binance_us": ExchangeProfile(
        "binance_us", "Binance.US (spot)", "https://api.binance.us", "spot", Decimal("0"), Decimal("2"),
        f"{FEE_ASSUMED}: Binance.US blog (April 2026) 0% maker / 0.02% taker",
        "a separate exchange from binance.com, with its own listings"),
    # Public market data only (no account, no orders): Binance's official
    # market-data host. Prices are binance.com spot prices, so the fee
    # assumption is binance.com's. Health-checked from the server like the
    # others; NOT selectable for strategies (registry.SPOT_EXCHANGES) until the
    # operator has checked that its terms allow the intended use from this
    # server's location (docs/MARKET_DATA_AND_451.md).
    "binance_data": ExchangeProfile(
        "binance_data", "Binance public market data (data-api.binance.vision)", "https://data-api.binance.vision",
        "spot", Decimal("10"), Decimal("10"),
        f"{FEE_ASSUMED}: binance.com Regular tier 0.10% / 0.10% (prices are binance.com spot prices)",
        "market data only: no account or order endpoint exists on this host; reachability is checked from "
        "the server before it is shown as healthy"),
}

EXCHANGES = tuple(PROFILES)
# Hosts that serve public market data only: never an account environment,
# so no key can be sent there (private.environments has no such host).
PUBLIC_DATA_ONLY = frozenset({"binance_data"})


def profile(exchange: str, base_url: str | None = None) -> ExchangeProfile:
    p = PROFILES[exchange]
    if base_url:
        from dataclasses import replace

        p = replace(p, base_url=base_url.rstrip("/"))
    return p
