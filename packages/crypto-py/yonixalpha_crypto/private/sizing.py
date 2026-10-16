"""Risk-based position size for an exchange account (spot, LONG).

The size is what loses at most the risk budget if the protective stop is
hit, after costs:
    budget        = min(equity * risk_pct / 100, max_loss_quote)
    entry_eff     = entry * (1 + slippage)
    exit_eff      = stop_limit * (1 - slippage)      (worst fill of the stop)
    loss_per_unit = entry_eff - exit_eff + taker fee on both legs
    qty           = budget / loss_per_unit, then capped by the maximum
                    position, the free quote balance (with the buy fee) and
                    rounded DOWN to the exchange step.
After rounding the planned loss is recomputed and must still be within the
budget. Anything that cannot be computed reliably is a rejection with a
reason, never a guess: unknown precision, a non-positive or non-finite
input, a stop not below the entry, a stop closer than two ticks, or a size
below the exchange minimum. A wider stop only ever gives a SMALLER size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from yonixalpha_crypto.exchanges.base import InstrumentSpec

BPS = Decimal(10000)
Q = Decimal("0.00000001")


@dataclass(frozen=True)
class SizingRules:
    risk_pct: Decimal  # % of equity risked per trade
    max_loss_quote: Decimal  # absolute cap on the planned loss
    max_position_quote: Decimal
    taker_fee_bps: Decimal
    slippage_bps: Decimal
    stop_limit_gap_bps: Decimal  # the stop-limit's limit price sits this far below the trigger


@dataclass
class SizedEntry:
    approved: bool
    qty: Decimal = Decimal(0)
    entry_eff: Decimal = Decimal(0)
    stop: Decimal = Decimal(0)
    stop_limit: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    budget: Decimal = Decimal(0)
    planned_loss: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)


def _ok(*vals: Decimal) -> bool:
    return all(isinstance(v, Decimal) and v.is_finite() for v in vals)


def size_long(rules: SizingRules, spec: InstrumentSpec, *, equity_quote: Decimal, free_quote: Decimal,
              entry_price: Decimal, stop_price: Decimal) -> SizedEntry:
    r = rules
    if not _ok(r.risk_pct, r.max_loss_quote, r.max_position_quote, r.taker_fee_bps, r.slippage_bps,
               r.stop_limit_gap_bps, equity_quote, free_quote, entry_price, stop_price):
        return SizedEntry(False, reasons=["an input is missing or not a finite number"])
    if spec.tick_size <= 0 or spec.qty_step <= 0:
        return SizedEntry(False, reasons=[f"{spec.symbol}: precision unknown (tick {spec.tick_size}, step {spec.qty_step})"])
    if not spec.tradable:
        return SizedEntry(False, reasons=[f"{spec.symbol} is not trading (status {spec.status})"])
    if equity_quote <= 0 or free_quote <= 0:
        return SizedEntry(False, reasons=["no equity or no free quote balance"])
    if r.risk_pct <= 0 or r.max_loss_quote <= 0 or r.max_position_quote <= 0:
        return SizedEntry(False, reasons=["risk limits must be positive"])
    if r.taker_fee_bps < 0 or r.slippage_bps < 0 or r.stop_limit_gap_bps < 0:
        return SizedEntry(False, reasons=["fees, slippage and the stop gap cannot be negative"])
    if entry_price <= 0 or stop_price <= 0:
        return SizedEntry(False, reasons=["entry and stop must be positive"])
    stop = spec.round_price(stop_price, up=False)
    if entry_price - stop < 2 * spec.tick_size:
        return SizedEntry(False, reasons=[f"stop {stop} is not at least two ticks below the entry {entry_price}"])
    stop_limit = spec.round_price(stop * (1 - r.stop_limit_gap_bps / BPS), up=False)
    if stop_limit <= 0:
        return SizedEntry(False, reasons=["the stop-limit price would be zero"])

    fee, slip = r.taker_fee_bps / BPS, r.slippage_bps / BPS
    entry_eff = entry_price * (1 + slip)
    exit_eff = stop_limit * (1 - slip)
    loss_per_unit = (entry_eff - exit_eff) + entry_eff * fee + exit_eff * fee
    if loss_per_unit <= 0:
        return SizedEntry(False, reasons=["loss per unit could not be computed"])
    budget = min(equity_quote * r.risk_pct / 100, r.max_loss_quote)
    qty = budget / loss_per_unit
    reasons = []
    cap_position = r.max_position_quote / entry_eff
    cap_cash = free_quote / (entry_eff * (1 + fee))
    if qty > cap_position:
        qty, _ = cap_position, reasons.append(f"capped by the maximum position {r.max_position_quote}")
    if qty > cap_cash:
        qty, _ = cap_cash, reasons.append(f"capped by the free balance {free_quote}")
    qty = (qty / spec.qty_step).to_integral_value(rounding=ROUND_DOWN) * spec.qty_step
    notional = qty * entry_eff
    if qty <= 0 or qty < spec.min_qty:
        return SizedEntry(False, budget=budget, reasons=reasons + [f"size {qty} below the minimum quantity {spec.min_qty}"])
    if notional < spec.min_notional:
        return SizedEntry(False, budget=budget,
                          reasons=reasons + [f"order value {notional:.4f} below the minimum {spec.min_notional}"])
    planned = (qty * loss_per_unit).quantize(Q)
    if planned > budget:  # cannot happen after rounding down; kept as a hard check
        return SizedEntry(False, budget=budget, reasons=[f"planned loss {planned} above the budget {budget}"])
    return SizedEntry(True, qty, entry_eff.quantize(Q), stop, stop_limit, notional.quantize(Q), budget.quantize(Q),
                      planned, reasons)
