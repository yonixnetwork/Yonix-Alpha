"""Paper vs LIVE parity per signal (Solana).

The system routes each approved signal to ONE target (PAPER or LIVE), so
true pairs of the same signal through both paths do not exist. For every
entry this reports what was measured on its own path, and the other path's
counterfactual where it can be measured, with a named reason for each
difference:

  every entry   signal time, decision (planned) price, target, entry fill,
                displacement from the decision price, fees as a share of
                the size, size, stop, take profits, exit reason, realized PnL
  PAPER entry   what LIVE would have paid: the stream price at the decision
                + the measured median LIVE decision-to-confirm latency
                (estimated, from the held trade stream; UNKNOWN when expired)
  LIVE entry    what PAPER would have filled at: the decision price (paper
                fills at the decision, plus the measured live drift when
                charge_measured_live_drift applies); the measured cause of
                the gap comes from the order's own price analysis

Plus, for the period: entries the gate approved but LIVE refused (wallet,
readiness, route), with their reasons. "As of now" values are labelled as
such; nothing is invented for a past moment.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, RiskAssessment, TradeTimelineEvent
from yonixalpha_core.solana import pump_stream

SOLANA_ENGINES = ("solana_fresh", "solana_migration", "solana_momentum")


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else round((a / b - 1) * 100, 3)


def _reasons(row: dict[str, Any]) -> list[str]:
    """Named causes of the difference between this entry and its counterfactual."""
    why: list[str] = []
    d = row.get("entry_displacement_pct")
    if d is not None and abs(d) >= 2:
        why.append(f"entry {d:+.1f}% vs the decision price "
                   + ("(latency: the market moved before the fill)" if row["mode"] == "LIVE" else "(paper fill impact / drift)"))
    fees = row.get("fees_pct_of_size")
    if fees is not None and fees >= 2:
        why.append(f"fixed and program fees {fees:.1f}% of the size")
    cls = row.get("live_price_classification")
    if cls and cls not in ("WITHIN_EXPECTED", "UNKNOWN"):
        why.append(f"LIVE price analysis: {cls}")
    est = row.get("est_live_displacement_pct")
    if est is not None and abs(est) >= 2:
        why.append(f"a LIVE order would have landed about {est:+.1f}% away from the paper fill (measured latency)")
    if row.get("exit_failures"):
        why.append(f"{row['exit_failures']} failed exit attempt(s) before the final exit")
    return why


async def _price_after(redis, mint: str, at: datetime, seconds: float) -> tuple[float | None, float | None, str | None]:
    """(price at `at`, price at `at`+seconds) from the held stream, raw
    lamports per raw unit; or the reason it is unknown."""
    trades = sorted(await pump_stream.load_trades(redis, mint), key=lambda t: t.at) if redis is not None else []
    if not trades:
        return None, None, "stream trades for this token expired (kept 3 h)"
    before = [t for t in trades if t.at <= at]
    later = [t for t in trades if t.at <= at + timedelta(seconds=seconds)]
    if not before:
        return None, None, "no stream trade at or before the decision"
    p0 = before[-1].virtual_sol / before[-1].virtual_token if before[-1].virtual_token else None
    p1 = later[-1].virtual_sol / later[-1].virtual_token if later and later[-1].virtual_token else None
    return p0, p1, None


async def per_signal(session: AsyncSession, redis, since: datetime, latency_s: float, latency_source: str,
                     limit: int = 200) -> dict[str, Any]:
    positions = (await session.execute(
        select(PaperPosition).where(PaperPosition.entry_at >= since, PaperPosition.engine.in_(SOLANA_ENGINES),
                                    PaperPosition.execution_mode.in_(("PAPER", "LIVE")),
                                    PaperPosition.status != "pending_entry")
        .order_by(PaperPosition.entry_at.desc()).limit(limit))).scalars().all()
    a_ids = [p.assessment_id for p in positions if p.assessment_id]
    plans = {aid: a for aid, a in (await session.execute(select(RiskAssessment.id, RiskAssessment.assessment)
                                                         .where(RiskAssessment.id.in_(a_ids)))).all()} if a_ids else {}
    orders = {}
    if positions:
        for o in (await session.execute(select(ExecutionOrder).where(
                ExecutionOrder.position_id.in_([p.id for p in positions]), ExecutionOrder.side == "BUY"))).scalars().all():
            orders.setdefault(o.position_id, o)
    rows: list[dict[str, Any]] = []
    for p in positions:
        a = plans.get(p.assessment_id) or {}
        plan = a.get("plan") or {}
        decision_price = _f(plan.get("entry_price"))
        size = _f((plan.get("position_size") or {}).get("value"))
        cost = _f(p.entry_cost_quote)
        entry = _f(p.entry_price)
        row: dict[str, Any] = {
            "position_id": str(p.id), "mint": p.asset_id, "symbol": p.symbol, "engine": p.engine, "mode": p.execution_mode,
            "status": p.status, "signal_at": a.get("evaluated_at") or (p.entry_at.isoformat() if p.entry_at else None),
            "entry_at": p.entry_at.isoformat() if p.entry_at else None,
            "decision_price": decision_price, "entry_price_all_in": entry,
            "entry_displacement_pct": _pct(entry, decision_price), "size_quote": size, "entry_cost_quote": cost,
            "fees_paid_quote": _f(p.fees_paid_quote),
            "fees_pct_of_size": round(_f(p.fees_paid_quote) / cost * 100, 3) if cost and p.fees_paid_quote is not None else None,
            "stop_loss": _f(p.stop_loss), "take_profit": p.take_profit, "exit_reason": p.exit_reason,
            "exit_price": _f(p.exit_price), "realized_pnl_pct": round(_f(p.realized_pnl_pct) * 100, 3) if p.realized_pnl_pct is not None else None,
            "exit_failures": int(getattr(p, "exit_failures", 0) or 0),
        }
        if p.execution_mode == "LIVE":
            o = orders.get(p.id)
            diag = (o.diagnostics or {}) if o is not None else {}
            row["decision_to_confirm_ms"] = (diag.get("timing") or {}).get("decision_to_confirm_ms")
            row["live_price_classification"] = (diag.get("price") or {}).get("classification")
            row["live_price_components_pct"] = (diag.get("price") or {}).get("components_pct")
            row["paper_counterfactual"] = {"fill_price": decision_price,
                                           "note": "paper fills at the decision price (plus measured live drift when enabled)",
                                           "live_minus_paper_pct": _pct(entry, decision_price)}
        else:
            at = p.entry_at
            if at is not None:
                p0, p1, unknown = await _price_after(redis, p.asset_id, at, latency_s)
                row["est_live_displacement_pct"] = _pct(p1, p0)
                row["live_counterfactual"] = {"latency_seconds": latency_s, "latency_source": latency_source,
                                              "estimated": True, "unknown": unknown}
        row["difference_reasons"] = _reasons(row)
        rows.append(row)

    refused = (await session.execute(select(TradeTimelineEvent.occurred_at, TradeTimelineEvent.detail)
                                     .where(TradeTimelineEvent.event_type == "live_entry_refused",
                                            TradeTimelineEvent.occurred_at >= since)
                                     .order_by(TradeTimelineEvent.occurred_at.desc()).limit(100))).all()
    refusal_counts: dict[str, int] = {}
    for _, detail in refused:
        r = str((detail or {}).get("reason") or "unknown")
        key = r.split(":")[0][:80]
        refusal_counts[key] = refusal_counts.get(key, 0) + 1

    def agg(mode: str) -> dict[str, Any]:
        sub = [r for r in rows if r["mode"] == mode]
        pnl = [r["realized_pnl_pct"] for r in sub if r["realized_pnl_pct"] is not None and r["status"] == "closed"]
        disp = [r["entry_displacement_pct"] for r in sub if r["entry_displacement_pct"] is not None]
        fees = [r["fees_pct_of_size"] for r in sub if r["fees_pct_of_size"] is not None]
        size = [r["size_quote"] for r in sub if r["size_quote"] is not None]
        return {"entries": len(sub), "closed": len(pnl),
                "win_rate": round(sum(1 for x in pnl if x > 0) / len(pnl), 4) if pnl else None,
                "median_pnl_pct": round(statistics.median(pnl), 3) if pnl else None,
                "median_entry_displacement_pct": round(statistics.median(disp), 3) if disp else None,
                "median_fees_pct_of_size": round(statistics.median(fees), 3) if fees else None,
                "median_size_quote": round(statistics.median(size), 6) if size else None}

    est = [r["est_live_displacement_pct"] for r in rows if r.get("est_live_displacement_pct") is not None]
    return {"since": since.isoformat(), "latency_seconds": latency_s, "latency_source": latency_source,
            "paper": agg("PAPER"), "live": agg("LIVE"),
            "paper_estimated_live_displacement_median_pct": round(statistics.median(est), 3) if est else None,
            "live_refused_after_gate_approval": {"count": len(refused), "by_reason": refusal_counts},
            "note": "each signal went to one target: the other path is a counterfactual, estimated where stated",
            "rows": rows}


def size_effect(fees_sol: float, size_sol: float) -> float | None:
    """Fixed fees as a percent of a trade's size (why tiny LIVE trades lose to fees)."""
    return None if not size_sol else round(fees_sol / size_sol * 100, 3)


def _d(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
