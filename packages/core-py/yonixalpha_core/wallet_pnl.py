"""Wallet profit and loss from a trade ledger (master upgrade §19-27).

Cost basis: FIFO. Every buy opens a lot; every sell consumes the oldest
lots of the same token first. A closed trade is one token's matched lots
(a wallet usually trades a memecoin as one position), so wins and losses
are counted per token, not per sell.

What is NOT profit here, by construction:
- a sell of tokens with no recorded buy (received by transfer, airdrop, or
  bought before the retained history): its proceeds are counted in
  `unknown_basis_sells`, never as a gain;
- open holdings: not valued (no executable price is attached to a
  profile), so there is no unrealized PnL — the reason says so.

Amounts are in the chain's native quote units exactly as the launchpad
event reports them (BNB on BSC, ETH on Robinhood Chain). Launchpad fees
reported separately by the event are listed as `fees_reported` and NOT
subtracted: whether an event's amount already includes its fee differs by
launchpad and is NOT VERIFIED per launchpad. Gas is not included (a trade
event does not carry the trader's gas).

Nothing here is a ranking: the numbers describe what the wallet did.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

MIN_CLOSED = 5  # fewer closed trades: the numbers are shown but marked INSUFFICIENT_DATA
WINDOWS = {"24H": timedelta(hours=24), "7D": timedelta(days=7), "14D": timedelta(days=14), "30D": timedelta(days=30),
           "90D": timedelta(days=90), "180D": timedelta(days=180)}


@dataclass
class TradeIn:
    token: str
    at: datetime
    is_buy: bool
    tokens: Decimal
    quote: Decimal  # native units
    fee: Decimal = Decimal(0)


@dataclass
class ClosedTrade:
    token: str
    cost: Decimal
    proceeds: Decimal
    opened_at: datetime
    closed_at: datetime
    qty: Decimal

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def roi(self) -> float | None:
        return float(self.pnl / self.cost) if self.cost > 0 else None

    @property
    def hold_s(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()


@dataclass
class Ledger:
    closed: list[ClosedTrade]
    open_tokens: int
    unknown_basis_sells: int
    unknown_basis_proceeds: Decimal
    fees_reported: Decimal
    first_at: datetime | None
    last_at: datetime | None


def fifo(trades: Iterable[TradeIn]) -> Ledger:
    lots: dict[str, deque] = defaultdict(deque)  # token -> [qty, unit_cost, at]
    per: dict[str, dict[str, Any]] = {}
    unknown_n, unknown_q, fees = 0, Decimal(0), Decimal(0)
    rows = sorted(trades, key=lambda t: t.at)
    for t in rows:
        fees += t.fee or 0
        if t.tokens <= 0:
            continue
        if t.is_buy:
            lots[t.token].append([t.tokens, t.quote / t.tokens, t.at])
            continue
        left, price = t.tokens, t.quote / t.tokens
        q = lots[t.token]
        while left > 0 and q:
            lot = q[0]
            take = min(left, lot[0])
            agg = per.setdefault(t.token, {"cost": Decimal(0), "proceeds": Decimal(0), "qty": Decimal(0),
                                           "opened": lot[2], "closed": t.at})
            agg["cost"] += take * lot[1]
            agg["proceeds"] += take * price
            agg["qty"] += take
            agg["opened"] = min(agg["opened"], lot[2])
            agg["closed"] = max(agg["closed"], t.at)
            lot[0] -= take
            left -= take
            if lot[0] <= 0:
                q.popleft()
        if left > 0:  # more sold than this ledger ever saw bought
            unknown_n += 1
            unknown_q += left * price
    closed = [ClosedTrade(tok, a["cost"], a["proceeds"], a["opened"], a["closed"], a["qty"]) for tok, a in per.items()]
    open_tokens = sum(1 for q in lots.values() if q)
    return Ledger(sorted(closed, key=lambda c: c.closed_at), open_tokens, unknown_n, unknown_q, fees,
                  rows[0].at if rows else None, rows[-1].at if rows else None)


def _d(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(Decimal("0.000000001")))


def _med(vals: list) -> Any:
    return statistics.median(vals) if vals else None


def _mean(vals: list) -> Any:
    return sum(vals) / len(vals) if vals else None


def max_drawdown(closed: list[ClosedTrade]) -> Decimal | None:
    """Largest peak-to-trough fall of cumulative realized PnL, in exit order."""
    if not closed:
        return None
    peak = cum = dd = Decimal(0)
    for c in sorted(closed, key=lambda c: c.closed_at):
        cum += c.pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def outliers(closed: list[ClosedTrade]) -> dict[str, Any]:
    """Is the result carried by one lucky trade?"""
    if not closed:
        return {"status": "INSUFFICIENT_DATA"}
    pnls = sorted((c.pnl for c in closed), reverse=True)
    total = sum(pnls, Decimal(0))
    gross_win = sum((p for p in pnls if p > 0), Decimal(0))
    without_best = total - pnls[0]
    without_top3 = total - sum(pnls[:3], Decimal(0))
    share = float(pnls[0] / gross_win) if gross_win > 0 and pnls[0] > 0 else None
    if total > 0 and without_best <= 0:
        dep = "HIGH: profitable only because of its best trade"
    elif total > 0 and without_top3 <= 0:
        dep = "MEDIUM: profitable only because of its top 3 trades"
    elif total > 0:
        dep = "LOW: still profitable without its top 3 trades"
    else:
        dep = "NOT PROFITABLE"
    return {"total_pnl": _d(total), "without_best": _d(without_best), "without_top3": _d(without_top3),
            "best_trade_share_of_gains": round(share, 4) if share is not None else None, "dependence": dep}


def stats(closed: list[ClosedTrade], min_closed: int = MIN_CLOSED) -> dict[str, Any]:
    wins = [c for c in closed if c.pnl > 0]
    losses = [c for c in closed if c.pnl <= 0]
    n = len(closed)
    gw = sum((c.pnl for c in wins), Decimal(0))
    gl = -sum((c.pnl for c in losses), Decimal(0))
    win_roi = [c.roi for c in wins if c.roi is not None]
    loss_roi = [c.roi for c in losses if c.roi is not None]
    reasons = []
    if n == 0:
        reasons.append("no closed trades in this window")
    elif n < min_closed:
        reasons.append(f"only {n} closed trades (at least {min_closed} are needed)")
    out = {
        "status": "OK" if n >= min_closed else "INSUFFICIENT_DATA", "reasons": reasons,
        "closed_trades": n, "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "usually_earns": {"avg": _d(_mean([c.pnl for c in wins])), "median": _d(_med([c.pnl for c in wins])),
                          "avg_pct": _pct(_mean(win_roi)), "median_pct": _pct(_med(win_roi)),
                          "largest": _d(max((c.pnl for c in wins), default=None))} if wins else None,
        "usually_loses": {"avg": _d(_mean([c.pnl for c in losses])), "median": _d(_med([c.pnl for c in losses])),
                          "avg_pct": _pct(_mean(loss_roi)), "median_pct": _pct(_med(loss_roi)),
                          "largest": _d(min((c.pnl for c in losses), default=None))} if losses else None,
        "realized_pnl": _d(gw - gl) if n else None,
        "roi": _pct(float((gw - gl) / sum((c.cost for c in closed), Decimal(0))))
        if n and sum((c.cost for c in closed), Decimal(0)) > 0 else None,
        "profit_factor": round(float(gw / gl), 3) if gl > 0 else None,
        "profit_factor_note": None if gl > 0 else ("no losing trades" if wins else None),
        "max_drawdown": _d(max_drawdown(closed)),
        "avg_hold_s": round(_mean([c.hold_s for c in closed]), 1) if n else None,
        "median_hold_s": round(_med([c.hold_s for c in closed]), 1) if n else None,
        "best_trade": _d(max((c.pnl for c in closed), default=None)),
        "worst_trade": _d(min((c.pnl for c in closed), default=None)),
        "outliers": outliers(closed),
    }
    return out


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100, 2)


def profile(trades: list[TradeIn], now: datetime, history_days: float, min_closed: int = MIN_CLOSED) -> dict[str, Any]:
    """The full P/L profile: all retained history plus fixed windows. A
    window longer than the retained history is INSUFFICIENT_DATA with the
    reason, never a number computed from part of it."""
    ledger = fifo(trades)
    windows = {}
    for name, span in WINDOWS.items():
        if span > timedelta(days=history_days) + timedelta(hours=1):
            windows[name] = {"status": "INSUFFICIENT_DATA", "closed_trades": None,
                             "reasons": [f"history retained here covers {history_days:g} days"]}
            continue
        windows[name] = stats([c for c in ledger.closed if c.closed_at >= now - span], min_closed)
    notes = [f"cost basis: FIFO over trades observed by this system in the last {history_days:g} days",
             "launchpad fees reported by events are listed, not subtracted (fee inclusion NOT VERIFIED per launchpad)",
             "gas is not included"]
    if ledger.unknown_basis_sells:
        notes.append(f"{ledger.unknown_basis_sells} sells had no recorded buy (transfer, airdrop or older buy): "
                     "their proceeds are not counted as profit")
    if ledger.open_tokens:
        notes.append(f"{ledger.open_tokens} tokens still held: unrealized PnL not computed (no executable price)")
    return {"all": stats(ledger.closed, min_closed), "windows": windows,
            "open_tokens": ledger.open_tokens, "unknown_basis_sells": ledger.unknown_basis_sells,
            "unknown_basis_proceeds": _d(ledger.unknown_basis_proceeds), "fees_reported": _d(ledger.fees_reported),
            "unrealized_pnl": None, "notes": notes, "cost_basis": "FIFO"}
