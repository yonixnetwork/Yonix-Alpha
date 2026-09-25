"""Shapes shared by the venue adapters."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.solana.market_data import RateBudget

# Published base-tier taker fees, used only to price paper fills. They are
# configured assumptions — an account's actual rate depends on its VIP tier
# and discounts — and can be overridden per strategy (config.taker_fee_bps).
DEFAULT_TAKER_FEE_BPS = {
    "binance": Decimal("5"),  # USDⓈ-M futures, regular user taker 0.0500%
    "bybit": Decimal("5.5"),  # derivatives, non-VIP taker 0.055%
    "hyperliquid": Decimal("4.5"),  # perps, base tier taker 0.045%
}


class VenueError(Exception):
    """Transport failure, non-2xx, venue error code, or unexpected shape."""


class NotConfigured(VenueError):
    """The call needs credentials that are not set in the environment."""


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool


@dataclass(frozen=True)
class Ticker:
    symbol: str
    observed_at: datetime
    last_price: Decimal | None
    mark_price: Decimal | None
    funding_rate: Decimal | None
    open_interest: Decimal | None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume_24h: Decimal | None = None


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def ms_to_dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


async def budgeted(budget: RateBudget, label: str) -> None:
    if not await budget.acquire():
        raise VenueError(f"{label}: client-side rate budget exhausted")


# Per-process record of each venue's last successful and failed call, for
# connection monitoring: services put it in their heartbeat, the API turns
# it into CONNECTED / DEGRADED / OFFLINE. Error text never includes
# credentials (requests are signed in headers, not echoed back).
VENUE_HEALTH: dict[str, dict[str, Any]] = {}


def _health(venue: str) -> dict[str, Any]:
    return VENUE_HEALTH.setdefault(venue, {"last_ok_at": None, "last_error_at": None, "last_error": None,
                                           "consecutive_failures": 0, "calls": 0})


async def tracked(venue: str, coro) -> Any:
    h = _health(venue)
    h["calls"] += 1
    try:
        out = await coro
    except NotConfigured:
        raise
    except VenueError as exc:
        h["last_error_at"] = datetime.now(timezone.utc).isoformat()
        h["last_error"] = str(exc)[:200]
        h["consecutive_failures"] += 1
        raise
    h["last_ok_at"] = datetime.now(timezone.utc).isoformat()
    h["consecutive_failures"] = 0
    return out


def record_call(venue: str, ok: bool, error: str | None = None) -> None:
    """For clients that report failures as values instead of raising
    (e.g. the Jupiter quote client)."""
    h = _health(venue)
    h["calls"] += 1
    if ok:
        h["last_ok_at"] = datetime.now(timezone.utc).isoformat()
        h["consecutive_failures"] = 0
    else:
        h["last_error_at"] = datetime.now(timezone.utc).isoformat()
        h["last_error"] = (error or "error")[:200]
        h["consecutive_failures"] += 1


def venue_health_snapshot() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in VENUE_HEALTH.items()}


def raise_for(resp: httpx.Response, label: str) -> Any:
    if resp.status_code != 200:
        raise VenueError(f"{label}: HTTP {resp.status_code} {resp.text[:160]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise VenueError(f"{label}: non-JSON response") from exc
