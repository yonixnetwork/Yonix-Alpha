"""Risk validation for the Crypto Trading engine, separate from the Memecoin
risk engine (which is SOL-denominated and is not used here). All amounts are
in the instrument's quote currency (USDT for BTC/USDT, ETH/USDT, SOL/USDT).

A strategy signal does not authorise a trade: size_entry() approves only
when every limit holds and the data is fresh; otherwise it returns the
reasons and nothing is filled."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook

BPS = Decimal(10000)

# key -> (default, lo, hi). Upper bounds are sanity bounds for a demo account.
RISK_RULES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "risk_per_trade_quote": (Decimal("5"), Decimal("0.01"), Decimal("1000")),
    "max_position_quote": (Decimal("100"), Decimal("1"), Decimal("100000")),
    "max_open_positions": (Decimal("1"), Decimal("1"), Decimal("10")),
    "max_daily_loss_quote": (Decimal("15"), Decimal("0.01"), Decimal("10000")),
    "max_total_exposure_quote": (Decimal("200"), Decimal("1"), Decimal("100000")),
    "max_spread_bps": (Decimal("20"), Decimal("0.1"), Decimal("500")),
    "slippage_bps": (Decimal("5"), Decimal("0"), Decimal("500")),
    "max_book_age_seconds": (Decimal("15"), Decimal("1"), Decimal("300")),
}


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_quote: Decimal
    max_position_quote: Decimal
    max_open_positions: int
    max_daily_loss_quote: Decimal
    max_total_exposure_quote: Decimal
    max_spread_bps: Decimal
    slippage_bps: Decimal
    max_book_age_seconds: Decimal

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RiskLimits":
        d = d or {}
        vals = {k: Decimal(str(d.get(k, rule[0]))) for k, rule in RISK_RULES.items()}
        vals["max_open_positions"] = int(vals["max_open_positions"])
        return cls(**vals)


def validate_risk(d: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    errors, out = [], {}
    for k, v in (d or {}).items():
        if k not in RISK_RULES:
            errors.append(f"risk.{k}: unknown setting")
            continue
        _, lo, hi = RISK_RULES[k]
        try:
            x = Decimal(str(v))
        except Exception:  # noqa: BLE001
            errors.append(f"risk.{k}: not a number")
            continue
        if not x.is_finite() or x < lo or x > hi:
            errors.append(f"risk.{k}: must be between {lo} and {hi}")
            continue
        if k == "max_open_positions" and x != x.to_integral_value():
            errors.append("risk.max_open_positions: must be a whole number")
            continue
        out[k] = str(x)
    return out, errors


@dataclass(frozen=True)
class Exposure:
    open_positions: int
    open_exposure_quote: Decimal
    realized_today_quote: Decimal  # negative = loss today (UTC)
    cash_quote: Decimal


@dataclass
class RiskDecision:
    approved: bool
    qty: Decimal = Decimal(0)
    expected_price: Decimal = Decimal(0)
    quote_amount: Decimal = Decimal(0)
    planned_loss_quote: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)


def size_entry(limits: RiskLimits, exposure: Exposure, book: TopOfBook, spec: InstrumentSpec, stop_price: Decimal,
               taker_fee_bps: Decimal, now: datetime, *, blocked: str | None = None) -> RiskDecision:
    """LONG entry (spot). Size so that a fill at ask + slippage, an exit at
    the stop - slippage, and taker fees on both legs lose at most
    risk_per_trade_quote; then cap by position, exposure and cash."""
    reasons: list[str] = []
    if blocked:
        reasons.append(blocked)
    if not spec.tradable:
        reasons.append(f"{spec.symbol} is not trading on {spec.exchange} (status {spec.status})")
    age = (now - book.at).total_seconds()
    if age > float(limits.max_book_age_seconds):
        reasons.append(f"order book is {age:.0f}s old (max {limits.max_book_age_seconds}s)")
    if book.bid <= 0 or book.ask <= 0 or book.ask < book.bid:
        reasons.append("no valid bid/ask")
    elif book.spread_bps > limits.max_spread_bps:
        reasons.append(f"spread {book.spread_bps:.2f} bps above max {limits.max_spread_bps}")
    if exposure.open_positions >= limits.max_open_positions:
        reasons.append(f"{exposure.open_positions} open position(s), max {limits.max_open_positions}")
    if -exposure.realized_today_quote >= limits.max_daily_loss_quote:
        reasons.append(f"daily loss {-exposure.realized_today_quote} reached max {limits.max_daily_loss_quote}")
    if reasons:
        return RiskDecision(False, reasons=reasons)

    slip, fee = limits.slippage_bps / BPS, taker_fee_bps / BPS
    entry = book.ask * (1 + slip)
    exit_at_stop = stop_price * (1 - slip)
    if stop_price <= 0 or exit_at_stop >= entry:
        return RiskDecision(False, reasons=["stop is not below the expected entry"])
    loss_per_unit = (entry - exit_at_stop) + entry * fee + exit_at_stop * fee
    qty = limits.risk_per_trade_quote / loss_per_unit
    room = min(limits.max_position_quote, limits.max_total_exposure_quote - exposure.open_exposure_quote,
               exposure.cash_quote / (1 + fee))
    if room <= 0:
        return RiskDecision(False, reasons=["no room: exposure or cash limit reached"])
    qty = min(qty, room / entry)
    qty = spec.round_qty(qty) if spec.qty_step > 0 else qty
    notional = qty * entry
    if qty <= 0 or qty < spec.min_qty:
        return RiskDecision(False, reasons=[f"size {qty} below the minimum quantity {spec.min_qty}"])
    if notional < spec.min_notional:
        return RiskDecision(False, reasons=[f"order value {notional:.4f} below the minimum {spec.min_notional}"])
    return RiskDecision(True, qty, entry, notional, (qty * loss_per_unit).quantize(Decimal("0.00000001")), [])
