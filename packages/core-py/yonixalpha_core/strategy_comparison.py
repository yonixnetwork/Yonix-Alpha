"""Strategy comparison (memecoin refocus, 2026-10-20): every closed Solana
position, PAPER and LIVE apart, grouped by strategy (engine), by token
category at entry and by both, with the figures that decide whether a
strategy works rather than a single combined score:

  trades, win rate, net SOL (after every fee charged), average win / loss,
  profit factor, maximum drawdown of the group's realized curve, fees,
  net WITHOUT the single best trade (a group that is positive only because
  of one trade has no demonstrated edge), best and worst token, median hold.

Read-only. The trade rows and the category rule are the ones of
tools.strategy_recovery, so the dashboard and the forensic reports agree.
Groups under MIN_SAMPLE trades are marked as too small to judge."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.tools.strategy_recovery import TRADES_SQL, category, metrics

MIN_SAMPLE = 20


def _num(v: float | None, digits: int = 6) -> str | None:
    if v is None:
        return None
    if v == float("inf"):
        return "inf"
    return f"{v:.{digits}f}"


def _group_row(key: str, mode: str, rows: list[Any]) -> dict[str, Any]:
    m = metrics(rows)
    pnls = [float(r.pnl or 0) for r in rows]
    best = max(range(len(rows)), key=lambda i: pnls[i])
    worst = min(range(len(rows)), key=lambda i: pnls[i])
    return {
        "key": key, "mode": mode, "trades": m["n"], "wins": m["wins"],
        "win_rate": _num(m["win_rate"], 4), "net_sol": _num(m["net"]), "avg_sol": _num(m["avg"]),
        "avg_win_sol": _num(m["avg_win"]), "avg_loss_sol": _num(m["avg_loss"]),
        "profit_factor": _num(m["pf"], 3), "max_drawdown_sol": _num(m["max_dd"]), "fees_sol": _num(m["fees"]),
        "avg_size_sol": _num(m["avg_size"]), "median_hold_seconds": m["median_hold_s"],
        "net_without_best_sol": _num(sum(pnls) - pnls[best]),
        "best": {"symbol": rows[best].symbol, "pnl_sol": _num(pnls[best]), "position_id": str(rows[best].id)},
        "worst": {"symbol": rows[worst].symbol, "pnl_sol": _num(pnls[worst]), "position_id": str(rows[worst].id)},
        "sample": "ok" if m["n"] >= MIN_SAMPLE else "too small to judge",
    }


def _short_category(r: Any) -> str:
    return category(r).split(" (")[0]


async def compare(session: AsyncSession, since: datetime | None = None) -> dict[str, Any]:
    rows = list((await session.execute(text(TRADES_SQL))).all())
    if since is not None:
        rows = [r for r in rows if r.exit_at is not None and r.exit_at >= since]
    groups: dict[str, dict[tuple[str, str], list[Any]]] = {"strategy": defaultdict(list), "category": defaultdict(list),
                                                             "strategy_x_category": defaultdict(list)}
    for r in rows:
        groups["strategy"][(r.mode, r.engine)].append(r)
        groups["category"][(r.mode, _short_category(r))].append(r)
        groups["strategy_x_category"][(r.mode, f"{r.engine} | {_short_category(r)}")].append(r)
    out: dict[str, Any] = {"since": since.isoformat() if since else None, "trades": len(rows), "min_sample": MIN_SAMPLE}
    for name, g in groups.items():
        out[name] = [_group_row(key, mode, v) for (mode, key), v in sorted(g.items())]
    out["notes"] = [
        "Realized results only, net of every fee charged; PAPER and LIVE are separate rows.",
        "net_without_best_sol: the group's net without its single best trade. A group positive only because of one "
        "trade has no demonstrated edge.",
        f"Groups with fewer than {MIN_SAMPLE} trades are too small to judge.",
        "Token category = the token's state at the decision (FRESH / NEAR_MIGRATION / MIGRATED / MOMENTUM).",
    ]
    return out
