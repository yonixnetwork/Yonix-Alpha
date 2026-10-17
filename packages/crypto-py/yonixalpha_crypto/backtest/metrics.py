"""Performance metrics, defined once and used for backtests and paper /
demo results alike. All amounts are in the quote currency.

Definitions:
- net_profit = sum of trade P&L after fees, slippage and funding;
  net_return_pct = net_profit / starting equity;
- gross_profit = sum of winning trades, gross_loss = sum of losing trades
  (a trade at exactly 0 counts as a loss: it paid costs);
- win_rate = winners / completed trades; expectancy = net_profit / trades,
  also in R (P&L / the risk planned at entry);
- profit_factor = gross_profit / |gross_loss| (None without losses);
- max_drawdown = largest peak-to-trough fall of the bar-close equity curve;
- Sharpe / Sortino: daily equity returns, annualised with sqrt(365) (crypto
  trades every day), risk-free rate 0; None with fewer than 30 days;
- exposure = share of bars with a position; capital utilisation = average
  entry notional / starting equity;
- realised P&L comes from closed trades; a position open at the end of the
  data is closed at the last close and counted separately (end_of_data).
A result with fewer than MIN_TRADES completed trades is labelled
"insufficient evidence" and is never ranked as a win.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from yonixalpha_crypto.backtest.engine import Result

MIN_TRADES = 30
MIN_DAYS = 30


def _daily(equity: list[float], times: list[str]) -> list[tuple[str, float]]:
    last: dict[str, float] = {}
    for t, e in zip(times, equity):
        last[t[:10]] = e
    return sorted(last.items())


def _period_returns(equity: list[float], times: list[str], key) -> dict[str, float]:
    ends: dict[str, float] = {}
    starts: dict[str, float] = {}
    prev = equity[0] if equity else 0.0
    for t, e in zip(times, equity):
        k = key(t)
        if k not in starts:
            starts[k] = prev
        ends[k] = e
        prev = e
    return {k: round((ends[k] / starts[k] - 1) * 100, 4) if starts[k] else 0.0 for k in ends}


def _week(t: str) -> str:
    from datetime import date
    y, m, d = int(t[:4]), int(t[5:7]), int(t[8:10])
    iso = date(y, m, d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def compute(res: Result, starting_equity: float, bars_per_day: float = 24.0) -> dict[str, Any]:
    tr = res.trades
    n = len(tr)
    pnls = [t.pnl for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net = sum(pnls)
    rs = [t.r_multiple for t in tr if t.r_multiple is not None]
    peak, mdd, mdd_pct = -math.inf, 0.0, 0.0
    for e in res.equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, peak - e)
            mdd_pct = max(mdd_pct, (peak - e) / peak * 100)
    daily = _daily(res.equity, res.times)
    rets = [(b / a - 1) for (_, a), (_, b) in zip(daily, daily[1:]) if a > 0]
    sharpe = sortino = None
    why_ratio = None
    if len(rets) >= MIN_DAYS:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
        dn = [min(0.0, x) for x in rets]
        dd = math.sqrt(sum(x * x for x in dn) / len(dn)) if dn else 0.0
        sharpe = round(mu / sd * math.sqrt(365), 3) if sd > 0 else None
        sortino = round(mu / dd * math.sqrt(365), 3) if dd > 0 else None
    else:
        why_ratio = f"needs {MIN_DAYS} days of equity (have {len(rets)})"
    streak_w = streak_l = cur_w = cur_l = 0
    for p in pnls:
        cur_w, cur_l = (cur_w + 1, 0) if p > 0 else (0, cur_l + 1)
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)
    holds = [t.exit_i - t.entry_i for t in tr if t.exit_i is not None]
    bars = max(1, res.end_i - res.start_i)
    insufficient = n < MIN_TRADES
    exit_reasons: dict[str, int] = defaultdict(int)
    for t in tr:
        exit_reasons[t.exit_reason or "?"] += 1
    return {
        "trades": n, "completed_trades": sum(1 for t in tr if not t.end_of_data),
        "end_of_data_trades": sum(1 for t in tr if t.end_of_data),
        "net_profit": round(net, 6), "net_return_pct": round(net / starting_equity * 100, 4) if starting_equity else None,
        "gross_profit": round(sum(wins), 6), "gross_loss": round(sum(losses), 6),
        "realized_pnl": round(sum(t.pnl for t in tr if not t.end_of_data), 6),
        "unrealized_pnl_closed_at_end": round(sum(t.pnl for t in tr if t.end_of_data), 6),
        "win_rate": round(len(wins) / n, 4) if n else None, "loss_rate": round(len(losses) / n, 4) if n else None,
        "avg_win": round(sum(wins) / len(wins), 6) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else None,
        "expectancy": round(net / n, 6) if n else None,
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
        "profit_factor": round(sum(wins) / -sum(losses), 4) if losses and sum(losses) < 0 else None,
        "max_drawdown": round(mdd, 6), "max_drawdown_pct": round(mdd_pct, 4),
        "sharpe": sharpe, "sortino": sortino, "ratio_unavailable_reason": why_ratio,
        "avg_holding_bars": round(sum(holds) / len(holds), 2) if holds else None,
        "avg_holding_hours": round(sum(holds) / len(holds) * 24 / bars_per_day, 2) if holds else None,
        "fees": round(sum(t.fees for t in tr), 6), "slippage": round(sum(t.slippage for t in tr), 6),
        "funding": round(sum(t.funding for t in tr), 6),
        "exposure_pct": round(res.exposure_bars / bars * 100, 2),
        "capital_utilization_pct": round(res.notional_sum / max(1, n) / starting_equity * 100, 2) if n else 0.0,
        "partial_fills": sum(1 for t in tr if t.partial_fill),
        "max_consecutive_wins": streak_w, "max_consecutive_losses": streak_l,
        "monthly_returns_pct": _period_returns(res.equity, res.times, lambda t: t[:7]),
        "weekly_returns_pct": _period_returns(res.equity, res.times, _week),
        "exit_reasons": dict(exit_reasons), "skipped_signals": dict(res.skipped),
        "long_short": {"long": {"trades": n, "net_profit": round(net, 6)}, "short": None},
        "period": {"from": res.times[0] if res.times else None, "to": res.times[-1] if res.times else None,
                   "bars": bars},
        "sample_label": "insufficient evidence" if insufficient else "sample sufficient",
        "insufficient_evidence": insufficient,
    }


def sample_curve(equity: list[float], times: list[str], points: int = 300) -> dict[str, list]:
    """A downsampled equity curve and its drawdown, for charts."""
    if not equity:
        return {"t": [], "equity": [], "drawdown_pct": []}
    step = max(1, len(equity) // points)
    idx = list(range(0, len(equity), step))
    if idx[-1] != len(equity) - 1:
        idx.append(len(equity) - 1)
    peak, dd = -math.inf, []
    for e in equity:
        peak = max(peak, e)
        dd.append((peak - e) / peak * 100 if peak > 0 else 0.0)
    return {"t": [times[i] for i in idx], "equity": [round(equity[i], 4) for i in idx],
            "drawdown_pct": [round(dd[i], 4) for i in idx]}
