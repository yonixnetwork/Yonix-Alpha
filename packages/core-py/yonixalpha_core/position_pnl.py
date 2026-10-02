"""Real-time PnL of one position (master §59-60), for every positions table.

A position always shows PROFIT or LOSS with its percentage when the PnL can
be calculated; never a bare OPEN. When it cannot (no price mark yet), the
outcome is PNL_UNAVAILABLE with the reason, never a 0.

Fields (amounts in the account's quote coin: SOL, BNB or ETH):
  entry            entry price
  current          latest mark (open) or exit price (closed)
  quantity         remaining tokens (open) / initial tokens (closed)
  value            remaining quantity at the latest mark (open)
  unrealized       value - the remaining share of the entry cost (open)
  realized         proceeds of the sold part - the sold share of the entry
                   cost (open, after a partial take-profit); realized_pnl
                   (closed)
  fees             fees paid so far (entry, exits)
  net              realized + unrealized; % of the entry cost
  peak             best price excursion since entry (highest price for a
                   LONG, lowest for a SHORT), in % of the entry price
  drawdown         current price against that peak, in % (0 or negative)

The entry cost includes the entry fees, so net is after them. The mark
basis differs by engine and is stated: EVM positions are marked at the
executable sell quote (exit fees and token taxes included); Solana paper
positions at the curve / pool price (exit costs not deducted); the price is
STALE when the mark is older than max_age_seconds.

tone: positive (green), negative (red), neutral; the UI pairs it with an icon.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.db.models import PaperPosition
from yonixalpha_core.safety import store

HUNDRED = Decimal(100)


def _s(v: Decimal | None, places: str | None = None) -> str | None:
    if v is None:
        return None
    if places:
        return f"{v.quantize(Decimal(places)):f}"
    return f"{v.normalize():f}" if v != 0 else "0"


def _pct(num: Decimal | None, den: Decimal | None) -> Decimal | None:
    return num / den * HUNDRED if num is not None and den else None


def outcome(net: Decimal | None) -> tuple[str, str]:
    if net is None:
        return "PNL_UNAVAILABLE", "neutral"
    if net > 0:
        return "PROFIT", "positive"
    if net < 0:
        return "LOSS", "negative"
    return "BREAKEVEN", "neutral"


def basis(p: PaperPosition) -> str:
    if (p.engine or "").startswith("evm_"):  # evm_bsc, evm_copy_bsc, ...
        return "executable sell quote (exit fees and token taxes included)"
    if store.venue_kind(p) == "futures":
        return "futures mark price (exit costs not deducted)"
    return "latest curve / pool price (exit costs not deducted)"


def view(p: PaperPosition, now: datetime, max_age_seconds: int = 120) -> dict[str, Any]:
    initial = p.initial_quantity or p.quantity
    remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    cost = p.entry_cost_quote
    is_open = p.status == "open"
    short = p.side == "SHORT"
    reason = None

    if is_open:
        current = p.last_price
        age = (now - p.last_marked_at).total_seconds() if p.last_marked_at else None
        price_status = "UNAVAILABLE" if current is None else ("STALE" if age is None or age > max_age_seconds else "LIVE")
        cost_remaining = cost * remaining / initial if cost is not None and initial else None
        cost_sold = cost - cost_remaining if cost is not None and cost_remaining is not None else None
        value = store.marked_value(p, current) if current is not None and remaining is not None else None
        unrealized = value - cost_remaining if value is not None and cost_remaining is not None else None
        realized = (p.proceeds_quote or Decimal(0)) - cost_sold if cost_sold is not None else None
        net = unrealized + realized if unrealized is not None and realized is not None else None
        if current is None:
            reason = "no price mark yet"
        elif cost is None:
            reason = "entry cost not recorded"
    else:
        current, age, price_status = p.exit_price, None, "CLOSED"
        value, unrealized = None, None
        realized = p.realized_pnl
        net = p.realized_pnl
        if net is None:
            reason = "realized PnL not recorded"

    peak_px = p.lowest_price if short else p.highest_price
    peak = None
    if peak_px is not None and p.entry_price:
        peak = (p.entry_price - peak_px) / p.entry_price * HUNDRED if short else (peak_px / p.entry_price - 1) * HUNDRED
    drawdown = None
    if peak_px and current is not None:
        drawdown = (peak_px - current) / peak_px * HUNDRED if short else (current / peak_px - 1) * HUNDRED
        drawdown = min(drawdown, Decimal(0))
    label, tone = outcome(net)
    net_pct = _pct(net, cost)
    return {
        "outcome": label, "tone": tone, "reason": reason,
        "net": _s(net), "net_pct": _s(net_pct, "0.01"),
        "entry": _s(p.entry_price), "current": _s(current), "price_status": price_status,
        "price_age_seconds": round(age, 1) if age is not None else None,
        "quantity": _s(remaining if is_open else initial), "entry_cost": _s(cost), "value": _s(value),
        "unrealized": _s(unrealized), "unrealized_pct": _s(_pct(unrealized, cost * remaining / initial
                                                                 if is_open and cost is not None and initial else None), "0.01"),
        "realized": _s(realized), "fees": _s(p.fees_paid_quote),
        "peak_pct": _s(peak, "0.01"), "drawdown_pct": _s(drawdown, "0.01"),
        "basis": basis(p),
    }
