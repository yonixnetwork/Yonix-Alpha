"""Performance figures computed from stored closed DEMO positions only.
A figure that needs more trades than exist is reported as unavailable (None
with a reason), never estimated."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

MIN_SAMPLE = 20  # closed trades before win rate / profit factor / expectancy are shown


def summarize(pnls: list[Decimal], fees: list[Decimal], starting_balance: Decimal | None = None) -> dict[str, Any]:
    """`pnls`: realized PnL per closed trade, oldest first, net of fees."""
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls, Decimal(0))
    equity, peak, max_dd = Decimal(0), Decimal(0), Decimal(0)
    curve = []
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append(str(equity))
    enough = n >= MIN_SAMPLE
    why = None if enough else f"needs {MIN_SAMPLE} closed demo trades (have {n})"
    gross_loss = -sum(losses, Decimal(0))
    return {
        "closed_trades": n,
        "realized_pnl_quote": str(total),
        "fees_quote": str(sum(fees, Decimal(0))),
        "win_rate": str(round(Decimal(len(wins)) / n, 4)) if enough else None,
        "profit_factor": (str(round(sum(wins, Decimal(0)) / gross_loss, 4)) if gross_loss > 0 else None) if enough else None,
        "expectancy_quote": str(round(total / n, 8)) if enough else None,
        "max_drawdown_quote": str(max_dd),
        "max_drawdown_pct": (str(round(max_dd / starting_balance * 100, 4))
                             if starting_balance and starting_balance > 0 else None),
        "equity_curve": curve[-500:],
        "unavailable_reason": why,
        "sample_label": "DEMO (simulated fills on public market data)",
    }
