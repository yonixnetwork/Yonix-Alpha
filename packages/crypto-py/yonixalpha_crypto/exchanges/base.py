"""Exchange-neutral market data types. Symbols are written BASE/QUOTE
(e.g. BTC/USDT) inside the engine; each adapter maps them to its own form."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Protocol

from yonixalpha_crypto.candles import Candle


@dataclass(frozen=True)
class InstrumentSpec:
    exchange: str
    symbol: str  # BASE/QUOTE
    venue_symbol: str
    base: str
    quote: str
    status: str  # the exchange's own status word
    tradable: bool
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal  # in the quote currency

    def round_qty(self, qty: Decimal) -> Decimal:
        """Always down: a demo order never buys more than it can afford."""
        return (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step

    def round_price(self, price: Decimal, up: bool) -> Decimal:
        """Buys round up and sells round down: rounding never improves a fill."""
        return (price / self.tick_size).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * self.tick_size


@dataclass(frozen=True)
class TopOfBook:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    at: datetime  # exchange time when given, otherwise receipt time
    bid_qty: Decimal | None = None  # displayed size at the best bid / ask
    ask_qty: Decimal | None = None

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 10000 if mid > 0 else Decimal(0)


class MarketData(Protocol):
    exchange: str

    async def instrument(self, symbol: str) -> InstrumentSpec: ...

    async def candles(self, symbol: str, interval: str, limit: int) -> list[Candle]: ...

    async def top_of_book(self, symbol: str) -> TopOfBook: ...

    async def ping(self) -> None: ...

    async def aclose(self) -> None: ...


def split_symbol(symbol: str) -> tuple[str, str]:
    base, sep, quote = symbol.upper().partition("/")
    if not sep or not base.isalnum() or not quote.isalnum():
        raise ValueError(f"symbol must be BASE/QUOTE, got {symbol!r}")
    return base, quote


def dec(v) -> Decimal:
    return Decimal(str(v))
