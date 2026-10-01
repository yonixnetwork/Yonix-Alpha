"""Copy position link and paper copy outcomes (master upgrade §32, §35).

Link (§32): what one copied position is tied to, built from rows that are
already stored (copy_events, copy_positions, paper_positions), so it also
covers positions opened before this module existed:

  source_wallet / source_transaction   the target and its trade (EVM: the
                                       transaction hash; the Solana stream
                                       carries no signature, so None)
  source_position                      target tokens bought / still held
  our_position, copy_mode, copy_ratio  our position, the target's mode, our
                                       tokens per target token
  target_entry / our_entry             per-token prices (native per whole token)
  target_exit / our_exit               the target's average sell price after
                                       our entry; our exit price
  copy_latency                         the stages of copy_trading.latency
  price_displacement_pct               our entry against the target's
  slippage                             None for paper: the paper fill IS the
                                       executable quote at decision time;
                                       slippage exists for live fills only
  pnl                                  realized (closed) or unrealized (open)

Outcomes (§35): every target BUY (copied, skipped or notify-only) is
evaluated once its horizon has passed, from trade prices recorded on the
same token (EVM: evm_trades; Solana: the pump stream, kept 3 hours):

  simulated entry   the first traded price at or after we SAW the target's
                    trade (what we could have got at the earliest)
  simulated exit    the target's first sell after that within the horizon
                    (a mirror exit), else the last traded price at the horizon
  result            WOULD_HAVE_WON (> 0 %) or WOULD_HAVE_LOST (<= 0 %), with
                    the best and worst move inside the horizon
  class             COPIED, MISSED (operational: too late, limits, cash,
                    switches), BLOCKED_BY_SAFETY (safety, gate, liquidity,
                    chase guard, risk plan), FILTERED_BY_SETTINGS (the
                    operator's own target filters), NOT_COPYABLE (token or
                    venue unknown / not tradable), NOTIFY_ONLY

Prices are other wallets' trades: before our fees, price impact and gas. A
token with no trade after we saw the target's trade is NO_PRICE_DATA, never
a 0 % result. This is training data and a measure of what each filter cost
or saved; it never changes a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

HORIZON = timedelta(minutes=60)
TOKEN_DECIMALS = {"solana": 6, "bsc": 18, "robinhood": 18}  # pump.fun mints: 6; EVM launchpad tokens: 18
QUOTE_DECIMALS = {"solana": 9, "bsc": 18, "robinhood": 18}

MISSED = {"TOO_LATE", "MAX_OPEN_FOR_TARGET", "INSUFFICIENT_PAPER_CASH", "DUPLICATE_POSITION", "KILL_SWITCH",
          "TRADING_CONTROL_OFF"}
SETTINGS = {"LAUNCHPAD_FILTERED", "TARGET_BUY_TOO_SMALL", "ALREADY_COPIED", "SELL_ONLY_TARGET"}
NOT_COPYABLE = {"TOKEN_NOT_DISCOVERED", "VENUE_NOT_TRADABLE"}
CLASSES = ("COPIED", "MISSED", "BLOCKED_BY_SAFETY", "FILTERED_BY_SETTINGS", "NOT_COPYABLE", "NOTIFY_ONLY")
BASIS = "other wallets' trade prices; before our fees, price impact and gas"


def skip_class(decision: str, reason: str | None) -> str:
    if decision == "COPIED":
        return "COPIED"
    if decision == "NOTIFIED":
        return "NOTIFY_ONLY"
    if decision == "FAILED":
        return "MISSED"
    code = (reason or "").split(":", 1)[0].strip()
    if code in MISSED:
        return "MISSED"
    if code in SETTINGS:
        return "FILTERED_BY_SETTINGS"
    if code in NOT_COPYABLE:
        return "NOT_COPYABLE"
    return "BLOCKED_BY_SAFETY"  # gate, safety, liquidity, chase guard, risk-plan blockers


def unit_price(chain: str, quote_raw: Decimal | int, token_raw: Decimal | int) -> Decimal | None:
    """Native quote per whole token from raw amounts."""
    q, t = Decimal(quote_raw), Decimal(token_raw)
    if q <= 0 or t <= 0:
        return None
    return (q / Decimal(10) ** QUOTE_DECIMALS[chain]) / (t / Decimal(10) ** TOKEN_DECIMALS[chain])


def whole_tokens(chain: str, token_raw: Decimal | int) -> Decimal:
    return Decimal(token_raw) / Decimal(10) ** TOKEN_DECIMALS[chain]


def source_transaction(chain: str, source_event_id: str) -> str | None:
    """EVM event ids are chain:tx_hash:log_index; Solana stream ids are hashes (no signature)."""
    parts = source_event_id.split(":")
    return parts[1] if chain != "solana" and len(parts) == 3 and parts[1].startswith("0x") else None


@dataclass(frozen=True)
class Point:
    at: datetime
    price: Decimal


def _pct(a: Decimal, b: Decimal) -> float:
    return round(float((b / a - 1) * 100), 4)


def evaluate(seen_at: datetime, path: Iterable[Point], target_exit: Point | None = None,
             horizon: timedelta = HORIZON) -> dict[str, Any]:
    """Paper outcome of taking the target's trade when we saw it (see module doc)."""
    end = seen_at + horizon
    pts = sorted((p for p in path if seen_at <= p.at <= end and p.price > 0), key=lambda p: p.at)
    if not pts:
        return {"status": "NO_PRICE_DATA", "reason": "no trade of this token between seeing the target's trade "
                                                      "and the horizon", "horizon_min": horizon.total_seconds() / 60}
    entry = pts[0]
    horizon_px = pts[-1]
    mirror = target_exit if target_exit is not None and entry.at <= target_exit.at <= end and target_exit.price > 0 \
        else None
    exit_ = mirror or horizon_px
    result = _pct(entry.price, exit_.price)
    return {
        "status": "EVALUATED", "label": "WOULD_HAVE_WON" if result > 0 else "WOULD_HAVE_LOST",
        "simulated_entry": str(entry.price), "simulated_entry_at": entry.at.isoformat(),
        "simulated_exit": str(exit_.price), "simulated_exit_at": exit_.at.isoformat(),
        "exit_by": "TARGET_SELL" if mirror else "HORIZON", "result_pct": result,
        "horizon_return_pct": _pct(entry.price, horizon_px.price),
        "max_return_pct": _pct(entry.price, max(p.price for p in pts)),
        "min_return_pct": _pct(entry.price, min(p.price for p in pts)),
        "trades_in_window": len(pts), "horizon_min": horizon.total_seconds() / 60, "basis": BASIS,
    }


def link(*, chain: str, wallet: str, mode: str, entry_event: Any | None, sells: list[Any], position: Any,
         target_tokens_held: Decimal | int | None) -> dict[str, Any]:
    """§32 link fields of one copy position (entry_event / sells: copy_events rows)."""
    t_bought = whole_tokens(chain, entry_event.target_token_amount) if entry_event is not None else None
    t_entry = unit_price(chain, entry_event.target_quote_amount, entry_event.target_token_amount) \
        if entry_event is not None else None
    sold_tok = sum((Decimal(e.target_token_amount) for e in sells), Decimal(0))
    sold_q = sum((Decimal(e.target_quote_amount) for e in sells), Decimal(0))
    t_exit = unit_price(chain, sold_q, sold_tok) if sells else None
    init = Decimal(position.initial_quantity or position.quantity)
    rem = Decimal(position.remaining_quantity if position.remaining_quantity is not None else position.quantity)
    proceeds = Decimal(position.proceeds_quote or 0)
    if position.exit_price is not None and position.status == "closed":
        our_exit = Decimal(position.exit_price)
    elif proceeds > 0 and init > rem:
        our_exit = proceeds / (init - rem)
    else:
        our_exit = None
    our_entry = Decimal(position.entry_price)
    if position.status == "closed":
        pnl, pnl_kind = position.realized_pnl, "realized"
    else:  # marked value of what is left + what partial exits returned - what the entry cost
        pnl = Decimal(position.last_price or position.entry_price) * rem + proceeds - Decimal(position.entry_cost_quote or 0)
        pnl_kind = "open: marked value + partial exits - cost"
    return {
        "source_wallet": wallet,
        "source_transaction": source_transaction(chain, entry_event.source_event_id) if entry_event is not None else None,
        "source_event_id": entry_event.source_event_id if entry_event is not None else None,
        "source_position": {"bought": t_bought, "still_held": whole_tokens(chain, target_tokens_held)
                            if target_tokens_held is not None else None},
        "our_position": position.id, "copy_mode": mode,
        "copy_ratio": (init / t_bought) if t_bought else None,
        "target_entry": t_entry, "our_entry": our_entry,
        "target_exit": t_exit, "target_sold_fraction": (sold_tok / Decimal(entry_event.target_token_amount))
        if sells and entry_event is not None and Decimal(entry_event.target_token_amount) > 0 else None,
        "our_exit": our_exit,
        "copy_latency": entry_event.latency_ms if entry_event is not None else None,
        "price_displacement_pct": _pct(t_entry, our_entry) if t_entry else None,
        "slippage": None, "slippage_note": "paper fill is the executable quote at decision time; measured on live fills only",
        "pnl": pnl, "pnl_kind": pnl_kind,
    }
