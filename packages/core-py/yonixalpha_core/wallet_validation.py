"""Wallet validation (master upgrade §26) and the discovery lifecycle (§25).

A wallet found by the profile rebuild is a CANDIDATE. It is checked against
operator-configurable minimums and limits over the closed trades of its
FIFO ledger (wallet_pnl). Every check is reported with its value, the
requirement and pass / fail, never a bare verdict:

  history (not enough to judge: INSUFFICIENT_DATA)
    min_trades, min_closed, min_active_days, min_active_weeks,
    min_unique_tokens, min_history_days (coverage: first to last trade)
  quality (judged: NOT_VALIDATED when one fails)
    min_profitable_periods       days with positive realized PnL
    min_profitable_period_share  consistency: share of active days that were
                                 profitable (one lucky day is not enough)
    max_drawdown_pct             drawdown of cumulative realized PnL as a
                                 percentage of the capital it put in
    min_profit_factor            gross wins / gross losses
    max_single_trade_share       best trade's share of gross gains
    min_median_return_pct        median closed-trade return

VALIDATED means "passed these rules on the history this system has", not
"good" and not a ranking. Discovery never copies a wallet: a VALIDATED
wallet is PAPER_FOLLOWED (its recent buys replayed with
copy_outcomes.evaluate) and the operator decides whether to add it as a
copy target.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from yonixalpha_core.wallet_pnl import ClosedTrade, max_drawdown

SETTINGS_KEY = "wallet_validation"
CANDIDATE, INSUFFICIENT, NOT_VALIDATED, VALIDATED = "CANDIDATE", "INSUFFICIENT_DATA", "NOT_VALIDATED", "VALIDATED"


@dataclass(frozen=True)
class ValidationConfig:
    min_trades: int = 20
    min_closed: int = 10
    min_active_days: int = 3
    min_active_weeks: int = 1
    min_unique_tokens: int = 5
    min_history_days: float = 3.0
    min_profitable_periods: int = 2
    min_profitable_period_share: float = 0.5
    max_drawdown_pct: float = 50.0
    min_profit_factor: float = 1.2
    max_single_trade_share: float = 0.5
    min_median_return_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


HISTORY_CHECKS = ("min_trades", "min_closed", "min_active_days", "min_active_weeks", "min_unique_tokens",
                  "min_history_days")


def parse_config(data: dict | None) -> tuple[ValidationConfig, list[str]]:
    """Operator values over the defaults; unknown keys and bad values are errors."""
    base, errors, values = ValidationConfig(), [], {}
    types = {f.name: f.type for f in fields(ValidationConfig)}
    for k, v in (data or {}).items():
        if k not in types:
            errors.append(f"unknown setting {k}")
            continue
        try:
            num = int(v) if types[k] in ("int", int) else float(Decimal(str(v)))
        except (ValueError, TypeError, InvalidOperation):
            errors.append(f"{k}: not a number")
            continue
        if num < 0:
            errors.append(f"{k}: must be >= 0")
            continue
        if k in ("min_profitable_period_share", "max_single_trade_share") and num > 1:
            errors.append(f"{k}: a share between 0 and 1")
            continue
        values[k] = num
    return ValidationConfig(**{**asdict(base), **values}), errors


def _check(name: str, value: Any, required: Any, ok: bool | None, unit: str = "") -> dict[str, Any]:
    return {"check": name, "value": value, "required": required, "pass": ok, "unit": unit}


def validate(closed: list[ClosedTrade], *, trades: int, unique_tokens: int, trade_times: list[datetime],
             cfg: ValidationConfig) -> dict[str, Any]:
    """§26 checks over one wallet's FIFO closed trades and trade times."""
    days = sorted({t.date() for t in trade_times})
    weeks = {tuple(d.isocalendar())[:2] for d in days}
    history_days = (max(trade_times) - min(trade_times)).total_seconds() / 86400 if trade_times else 0.0
    by_day: dict[Any, Decimal] = defaultdict(Decimal)
    for c in closed:
        by_day[c.closed_at.date()] += c.pnl
    profitable = sum(1 for v in by_day.values() if v > 0)
    share = profitable / len(by_day) if by_day else None
    gross_win = sum((c.pnl for c in closed if c.pnl > 0), Decimal(0))
    gross_loss = -sum((c.pnl for c in closed if c.pnl <= 0), Decimal(0))
    pf = float(gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    best = max((c.pnl for c in closed), default=None)
    best_share = float(best / gross_win) if best is not None and best > 0 and gross_win > 0 else None
    cost = sum((c.cost for c in closed), Decimal(0))
    dd = max_drawdown(closed)
    dd_pct = float(dd / cost * 100) if dd is not None and cost > 0 else None
    rois = [c.roi for c in closed if c.roi is not None]
    med = statistics.median(rois) * 100 if rois else None

    history = [
        _check("min_trades", trades, cfg.min_trades, trades >= cfg.min_trades),
        _check("min_closed", len(closed), cfg.min_closed, len(closed) >= cfg.min_closed),
        _check("min_active_days", len(days), cfg.min_active_days, len(days) >= cfg.min_active_days),
        _check("min_active_weeks", len(weeks), cfg.min_active_weeks, len(weeks) >= cfg.min_active_weeks),
        _check("min_unique_tokens", unique_tokens, cfg.min_unique_tokens, unique_tokens >= cfg.min_unique_tokens),
        _check("min_history_days", round(history_days, 2), cfg.min_history_days, history_days >= cfg.min_history_days, "days"),
    ]
    quality = [
        _check("min_profitable_periods", profitable, cfg.min_profitable_periods,
               profitable >= cfg.min_profitable_periods, "days"),
        _check("min_profitable_period_share", round(share, 4) if share is not None else None,
               cfg.min_profitable_period_share, None if share is None else share >= cfg.min_profitable_period_share),
        _check("max_drawdown_pct", round(dd_pct, 2) if dd_pct is not None else None, cfg.max_drawdown_pct,
               None if dd_pct is None else dd_pct <= cfg.max_drawdown_pct, "%"),
        _check("min_profit_factor", None if pf is None else ("inf" if pf == float("inf") else round(pf, 3)),
               cfg.min_profit_factor, None if pf is None else pf >= cfg.min_profit_factor),
        _check("max_single_trade_share", round(best_share, 4) if best_share is not None else None,
               cfg.max_single_trade_share, None if best_share is None else best_share <= cfg.max_single_trade_share),
        _check("min_median_return_pct", round(med, 2) if med is not None else None, cfg.min_median_return_pct,
               None if med is None else med >= cfg.min_median_return_pct, "%"),
    ]
    missing = [c["check"] for c in history if not c["pass"]]
    unknown = [c["check"] for c in quality if c["pass"] is None]
    failed = [c["check"] for c in quality if c["pass"] is False]
    if missing:
        status, reason = INSUFFICIENT, "not enough history to judge: " + ", ".join(missing)
    elif failed:
        status, reason = NOT_VALIDATED, "failed: " + ", ".join(failed)
    elif unknown:
        status, reason = INSUFFICIENT, "cannot be measured yet: " + ", ".join(unknown)
    else:
        status, reason = VALIDATED, "passed every configured check on the history observed"
    return {"status": status, "reason": reason, "checks": history + quality, "config": cfg.to_dict(),
            "active_days": len(days), "profitable_days": profitable}


def discovery_status(validation: dict[str, Any], paper_follow: dict[str, Any] | None) -> dict[str, Any]:
    """§25 lifecycle. Never COPYING: adding a copy target is the operator's decision."""
    v = validation.get("status")
    if v == VALIDATED:
        stage = "PAPER_FOLLOWED" if paper_follow and paper_follow.get("evaluated") else "VALIDATED"
    elif v == NOT_VALIDATED:
        stage = "REJECTED"
    else:
        stage = "COLLECTING_HISTORY"
    return {"stage": stage, "validation": v, "note": "candidates are never copied automatically; "
                                                     "add a copy target yourself after reviewing the paper results"}
