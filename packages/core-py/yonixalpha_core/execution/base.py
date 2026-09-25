"""Execution providers for LIVE derivatives / FX trading.

Every provider implements the same contract, so the order worker, exit
logic and reconciliation are written once:

- instrument(symbol)            -> step size, tick size, minimums
- prepare(symbol, leverage)     -> leverage (+ one-way position mode check)
- market_order(...)             -> submit with OUR client id (idempotency)
- order_status(symbol, cid)     -> the exchange's view: status, filled qty,
                                   average price, fee (never assumed)
- set_stop(...) / cancel_stop / cancel_protection / open_protection
                                -> an exchange-side stop that closes the
                                   whole position even if YonixAlpha is down;
                                   a moved stop is placed before the old
                                   one is cancelled, so there is no gap
- position(symbol), balance(), fills_since(symbol, ms)
                                -> reconciliation inputs

Providers never decide anything. They raise ExecutionError on any failure
and return the exchange's answer otherwise; callers treat anything that is
not a confirmed fill as not filled.
"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from yonixalpha_core.venues import common as venue_common

FILLED = "FILLED"
PARTIAL = "PARTIALLY_FILLED"
OPEN = "NEW"
CANCELED = "CANCELED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
UNKNOWN = "UNKNOWN"
TERMINAL = {FILLED, CANCELED, REJECTED, EXPIRED}


class ExecutionError(venue_common.VenueError):
    """Transport failure, exchange error code or unexpected response.
    A VenueError, so venue health counts it and callers that already stop
    on venue failures stop on these too."""


class NotConfigured(ExecutionError, venue_common.NotConfigured):
    """Credentials or endpoint for this provider are not set."""


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    qty_step: Decimal
    tick_size: Decimal
    min_qty: Decimal
    min_notional: Decimal = Decimal(0)
    price_sig_figs: int | None = None  # Hyperliquid: 5 significant figures

    def round_qty(self, qty: Decimal) -> Decimal:
        return (qty / self.qty_step).to_integral_value(ROUND_DOWN) * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        p = price
        if self.price_sig_figs:
            p = Decimal(f"{p:.{self.price_sig_figs}g}")
        return (p / self.tick_size).to_integral_value() * self.tick_size

    def check(self, qty: Decimal, price: Decimal) -> str | None:
        if qty < self.min_qty or qty <= 0:
            return f"quantity {qty} below the venue minimum {self.min_qty}"
        if self.min_notional and qty * price < self.min_notional:
            return f"notional {qty * price} below the venue minimum {self.min_notional}"
        return None


@dataclass
class OrderState:
    client_id: str
    status: str  # FILLED | PARTIALLY_FILLED | NEW | CANCELED | REJECTED | EXPIRED | UNKNOWN
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee: Decimal = Decimal(0)  # in the quote currency when the venue reports it that way
    exchange_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"client_id": self.client_id, "status": self.status, "filled_qty": str(self.filled_qty),
                "avg_price": str(self.avg_price) if self.avg_price is not None else None, "fee": str(self.fee),
                "exchange_id": self.exchange_id, "error": self.error}


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    size: Decimal  # signed: + long, - short
    entry_price: Decimal | None
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    order_id: str
    client_id: str | None
    side: str  # BUY | SELL
    qty: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal | None
    time_ms: int


class ExecutionProvider(Protocol):
    venue: str
    quote_currency: str

    @property
    def configured(self) -> bool: ...
    async def instrument(self, symbol: str) -> InstrumentRules: ...
    async def prepare(self, symbol: str, leverage: int) -> None: ...
    async def market_order(self, symbol: str, side: str, qty: Decimal, reduce_only: bool, client_id: str,
                           ref_price: Decimal | None = None) -> OrderState: ...
    async def order_status(self, symbol: str, client_id: str) -> OrderState: ...
    async def set_stop(self, symbol: str, position_side: str, stop_price: Decimal, qty: Decimal, client_id: str) -> str: ...
    async def cancel_stop(self, symbol: str, stop_id: str) -> None: ...
    async def cancel_protection(self, symbol: str) -> None: ...
    async def open_protection(self, symbol: str) -> list[dict[str, Any]]: ...
    async def position(self, symbol: str) -> PositionInfo | None: ...
    async def balance(self) -> Decimal: ...
    async def fills_since(self, symbol: str, start_ms: int) -> list[Fill]: ...


def close_side(position_side: str) -> str:
    return "SELL" if position_side == "LONG" else "BUY"


def open_side(position_side: str) -> str:
    return "BUY" if position_side == "LONG" else "SELL"


def dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return default
