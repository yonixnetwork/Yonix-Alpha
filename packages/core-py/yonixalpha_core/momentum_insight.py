"""Momentum strategy panel (dashboard): what the strategy decided and how
its closed trades did, from recorded data only.

  decisions   momentum decisions in the window by stage and outcome:
              GATE episodes (traded or not) and WATCH rows (evaluated while
              the strategy was OFF, never traded)
  results     closed momentum positions, PAPER and LIVE apart: trades, win
              rate, net, profit factor, drawdown, fees, net without the best
              trade and without the best day
  by_lifecycle  the token's curve state at entry: CURVE (< 70 % sold),
              NEAR_MIGRATION (>= 70 %), and whether it migrated while held
  by_entry_type CONTINUATION (rising over 1 m and 5 m) or PULLBACK (up over
              5 m, last minute down), from the features at the decision
  recent      the latest decisions with their v2 state and reasons

A group appears only when it has trades; nothing is filled in or sampled.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import OpportunityOutcome, RiskAssessment
from yonixalpha_core.strategies import momentum_v2
from yonixalpha_core.strategy_comparison import MIN_SAMPLE
from yonixalpha_core.tools.strategy_recovery import TRADES_SQL, metrics

ENGINE = "solana_momentum"
NEAR = Decimal("0.70")
RECENT = 15


def _d(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except Exception:  # noqa: BLE001 - a stored non-number is shown as unknown
        return None


def lifecycle_of(r: Any) -> str:
    snap = r.snap or {}
    cp = _d(snap.get("curve_progress"))
    base = "UNKNOWN" if cp is None else ("NEAR_MIGRATION" if cp >= NEAR else "CURVE")
    return base + (" -> migrated while held" if r.lifecycle == "MIGRATED" else "")


def entry_type_of(r: Any) -> str:
    snap = r.snap or {}
    v2 = snap.get("momentum_v2") or {}
    if v2.get("entry_type"):
        return v2["entry_type"]
    return momentum_v2.entry_type(momentum_v2.features_from_intel(snap.get("intel"), snap))


def summary(rows: list[Any]) -> dict[str, Any]:
    m = metrics(rows)
    pnls = [float(r.pnl or 0) for r in rows]
    by_day: dict[str, float] = defaultdict(float)
    for r, p in zip(rows, pnls):
        by_day[r.exit_at.astimezone(timezone.utc).strftime("%Y-%m-%d") if r.exit_at else "?"] += p
    best_day = max(by_day, key=by_day.get) if by_day else None
    fees_share = (m["fees"] / (m["avg_size"] * m["n"])) if m["avg_size"] and m["n"] else None
    return {"trades": m["n"], "wins": m["wins"], "win_rate": m["win_rate"], "net_sol": m["net"],
            "profit_factor": None if m["pf"] == float("inf") else m["pf"], "max_drawdown_sol": m["max_dd"],
            "fees_sol": m["fees"], "fees_share_of_size": fees_share, "avg_size_sol": m["avg_size"],
            "net_without_best_trade_sol": (sum(pnls) - max(pnls)) if pnls else None,
            "net_without_best_day_sol": (sum(pnls) - by_day[best_day]) if best_day else None, "best_day": best_day,
            "sample": "ok" if m["n"] >= MIN_SAMPLE else "too small to judge"}


def _grouped(rows: list[Any], key) -> list[dict[str, Any]]:
    g: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        g[(r.mode, key(r))].append(r)
    return [{"mode": mode, "group": name, **summary(v)} for (mode, name), v in sorted(g.items())]


async def insight(session: AsyncSession, since: datetime | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    window_start = since or now - timedelta(hours=24)
    rows = [r for r in (await session.execute(text(TRADES_SQL))).all() if r.engine == ENGINE]
    if since is not None:
        rows = [r for r in rows if r.exit_at is not None and r.exit_at >= since]
    by_mode: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_mode[r.mode].append(r)

    o = OpportunityOutcome
    decisions = [{"stage": s, "decision": d, "count": n} for s, d, n in (await session.execute(
        select(o.stage, o.decision, func.count()).where(o.engine == ENGINE, o.decided_at >= window_start)
        .group_by(o.stage, o.decision).order_by(o.stage, o.decision))).all()]
    gate = {d: n for d, n in (await session.execute(
        select(RiskAssessment.decision, func.count()).where(RiskAssessment.engine == ENGINE,
                                                            RiskAssessment.evaluated_at >= window_start)
        .group_by(RiskAssessment.decision))).all()}
    recent_rows = (await session.execute(select(o).where(o.engine == ENGINE).order_by(o.decided_at.desc()).limit(RECENT))).scalars()
    recent = []
    for x in recent_rows:
        v2 = (x.snapshot or {}).get("momentum_v2") or {}
        recent.append({"decided_at": x.decided_at.isoformat(), "symbol": x.symbol, "mint": x.mint, "stage": x.stage,
                       "decision": x.decision, "traded": x.traded, "state": v2.get("state"), "score": v2.get("score"),
                       "entry_type": v2.get("entry_type"), "version": v2.get("version"),
                       "reasons": (v2.get("reasons") or list(x.reasons or []))[:3],
                       "checks": [{"name": c.get("name"), "status": c.get("status"), "detail": c.get("detail")}
                                  for c in (v2.get("checks") or [])]})
    return {
        "window_start": window_start.isoformat(), "since": since.isoformat() if since else None,
        "decisions": decisions, "gate_decisions": gate,
        "results": [{"mode": mode, **summary(v)} for mode, v in sorted(by_mode.items())],
        "by_lifecycle": _grouped(rows, lifecycle_of), "by_entry_type": _grouped(rows, entry_type_of),
        "recent": recent, "min_sample": MIN_SAMPLE,
        "notes": ["Closed trades only, net of every fee charged; PAPER and LIVE are separate.",
                  "Decisions count the window (default the last 24 h); results count all history unless a window is chosen.",
                  "WATCH rows were evaluated while the strategy was OFF: no position was opened.",
                  f"Groups under {MIN_SAMPLE} trades are too small to judge."],
    }
