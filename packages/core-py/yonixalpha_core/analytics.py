"""Performance statistics over CLOSED paper positions (spec §48).

Everything is computed from realized PnL in the position's own quote
currency; positions in different currencies (SOL, USDT, USDC) are never
summed together — callers group by account first. Open positions are not
trades yet and are never counted here.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)


@dataclass
class ClosedTrade:
    pnl: Decimal
    pnl_pct: Decimal | None
    fees: Decimal
    entry_at: datetime
    exit_at: datetime


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def performance(trades: list[ClosedTrade], starting_balance: Decimal | None = None) -> dict[str, Any]:
    """Win/loss/breakeven counts and rates, profit factor, expectancy,
    average win/loss, largest win/loss, max drawdown of the realized equity
    curve (absolute, and relative to its peak including the starting
    balance when given), fees and average holding time."""
    trades = sorted(trades, key=lambda t: t.exit_at)
    n = len(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    breakeven = n - len(wins) - len(losses)
    gross_profit = sum(wins, ZERO)
    gross_loss = sum(losses, ZERO)
    total = gross_profit + gross_loss
    equity = starting_balance or ZERO
    peak = equity
    max_dd = ZERO
    max_dd_pct: Decimal | None = None
    curve = []
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            if peak > 0:
                max_dd_pct = dd / peak
        curve.append({"at": t.exit_at.isoformat(), "cumulative_pnl": str(equity - (starting_balance or ZERO))})
    durations = [(t.exit_at - t.entry_at).total_seconds() for t in trades]
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": (len(wins) / n) if n else None,
        "loss_rate": (len(losses) / n) if n else None,
        "total_pnl": str(total),
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        # No losing trade -> undefined, not infinite: reported as null.
        "profit_factor": _s(gross_profit / -gross_loss) if gross_loss < 0 else None,
        "expectancy": _s(total / n) if n else None,
        "avg_win": _s(gross_profit / len(wins)) if wins else None,
        "avg_loss": _s(gross_loss / len(losses)) if losses else None,
        "largest_win": _s(max(wins)) if wins else None,
        "largest_loss": _s(min(losses)) if losses else None,
        "max_drawdown": str(max_dd),
        "max_drawdown_pct": _s(max_dd_pct),
        "fees": str(sum((t.fees for t in trades), ZERO)),
        "avg_duration_seconds": (sum(durations) / n) if n else None,
        "equity_curve": curve,
    }
