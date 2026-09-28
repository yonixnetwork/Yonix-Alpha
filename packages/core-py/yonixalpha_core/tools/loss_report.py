"""Loss report: why recent losing trades were entered, and why volatility
blocked decisions. Read-only; prints no secrets.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.loss_report [--mode LIVE|PAPER] [--last 20] [--hours 72] [--json]

For each losing closed position: PnL, MFE/MAE (from the position's marks),
exit reason, hold time, and the entry decision's own evidence — flow
features, the exit-intelligence check at entry, volatility and its source,
data errors, every warning finding — plus the LOSS_ANALYSIS classification
(the same rules opportunities.classify_loss applies to new trades).
Then: decisions blocked because volatility was unavailable, grouped by the
recorded reason, over the same window.
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from yonixalpha_core import opportunities
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, RiskAssessment

FEATURES = ("unique_buyers", "trade_count", "buy_sell_volume_ratio", "window_volume", "volatility", "liquidity_quote",
            "top10_share", "creator_launches_24h", "age_seconds", "round_trip_share", "sync_buy_cluster")


def _pct(a, b) -> str | None:
    try:
        return str(((Decimal(a) / Decimal(b) - 1) * 100).quantize(Decimal("0.01"))) if a is not None and b else None
    except Exception:  # noqa: BLE001
        return None


async def losses(session, mode: str, since: datetime, last: int) -> list[dict[str, Any]]:
    rows = (await session.execute(select(PaperPosition).where(
        PaperPosition.execution_mode == mode, PaperPosition.status == "closed", PaperPosition.realized_pnl < 0,
        PaperPosition.exit_at >= since).order_by(PaperPosition.exit_at.desc()).limit(last))).scalars().all()
    out = []
    for p in rows:
        a = await session.get(RiskAssessment, p.assessment_id) if p.assessment_id else None
        doc = (a.assessment if a else None) or {}
        ev = doc.get("inputs_snapshot") or {}
        feats = ev.get("features") or {}
        findings = doc.get("findings") or []
        warnings = [f"{f.get('code')}: {str(f.get('message'))[:140]}" for f in findings
                    if f.get("level") in ("MODERATE", "HIGH") or f.get("action") not in ("EXECUTE", None)]
        buy = (await session.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == p.id, ExecutionOrder.side == "BUY")
                                     .order_by(ExecutionOrder.created_at))).scalars().first()
        diag = (buy.diagnostics or {}) if buy is not None else {}
        snapshot = {"data_errors": ev.get("errors") or [], "volatility_confidence": ev.get("volatility_confidence"),
                    "deterioration_indicators": (ev.get("entry_quality") or {}).get("indicators"),
                    "entry_exit_check": (ev.get("entry_exit_check") or {}).get("action"),
                    "signal_qualified": doc.get("qualified"), "signal_strength": None,
                    "price_age_seconds": (diag.get("decision") or {}).get("price_age_seconds")}
        result = {"pnl_sol": str(p.realized_pnl), "pnl_pct": str(p.realized_pnl_pct), "exit_reason": p.exit_reason,
                  "mfe_pct": _pct(p.highest_price, p.entry_price), "mae_pct": _pct(p.lowest_price, p.entry_price)}
        la = opportunities.classify_loss(snapshot, result, diag, Decimal(p.max_loss_quote) if p.max_loss_quote else None)
        out.append({
            "symbol": p.symbol, "mint": p.asset_id, "engine": p.engine, "entry_at": p.entry_at.isoformat(),
            "hold_seconds": round((p.exit_at - p.entry_at).total_seconds()) if p.exit_at and p.entry_at else None,
            **result, "planned_entry": str((p.plan or {}).get("entry_price")), "fill_entry": str(p.entry_price),
            "entry_decision": {"decision": a.decision if a else None, "status": a.status_label if a else None,
                               "overall_risk": a.overall_risk if a else None},
            "features": {k: feats.get(k) for k in FEATURES},
            "entry_exit_check": ev.get("entry_exit_check"), "volatility_source": ev.get("volatility_source"),
            "data_errors": [str(e)[:160] for e in (ev.get("errors") or [])][:5],
            "warnings_at_entry": warnings[:10],
            "execution": {"decision_to_confirm_ms": (diag.get("timing") or {}).get("decision_to_confirm_ms"),
                          "price_cause": (diag.get("price") or {}).get("classification")},
            "loss_analysis": la,
        })
    return out


async def volatility_blocks(session, since: datetime) -> dict[str, Any]:
    rows = (await session.execute(select(RiskAssessment.assessment).where(RiskAssessment.evaluated_at >= since)
                                  .order_by(RiskAssessment.evaluated_at.desc()).limit(5000))).scalars().all()
    blocked = Counter()
    low = 0
    for doc in rows:
        codes = {f.get("code") for f in (doc or {}).get("findings") or []}
        src = str(((doc or {}).get("inputs_snapshot") or {}).get("volatility_source") or "no source recorded")
        if "AUTO_SL_NO_VOLATILITY" in codes:
            blocked[src.split(";")[0][:120]] += 1
        if "VOLATILITY_LOW_CONFIDENCE" in codes:
            low += 1
    return {"assessments_checked": len(rows), "blocked_no_volatility": sum(blocked.values()),
            "by_reason": dict(blocked.most_common(10)), "low_confidence_needs_approval": low}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="LIVE", choices=("LIVE", "PAPER"))
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    engine = make_engine(get_settings())
    async with make_session_factory(engine)() as s:
        rows = await losses(s, a.mode, since, a.last)
        vol = await volatility_blocks(s, since)
    await engine.dispose()
    if a.json:
        print(json.dumps({"losses": rows, "volatility": vol}, default=str, indent=1))
        return 0
    for r in rows:
        la = r["loss_analysis"]
        print(f"=== {r['entry_at'][:19]} {r['symbol']:12} {r['engine']}  PnL {r['pnl_sol']} SOL ({r['pnl_pct']})  "
              f"exit {r['exit_reason']} after {r['hold_seconds']}s  MFE {r['mfe_pct']}% / MAE {r['mae_pct']}%")
        print(f"    class {la['classification']}  flags {la['flags']}  {'; '.join(la['evidence'])}")
        print(f"    entry: {r['entry_decision']}  planned {r['planned_entry']} → filled {r['fill_entry']}  exec {r['execution']}")
        print(f"    features: {json.dumps(r['features'], default=str)}")
        print(f"    exit check at entry: {(r['entry_exit_check'] or {}).get('action')} {(r['entry_exit_check'] or {}).get('reasons')}")
        print(f"    volatility: {r['volatility_source']}  data errors: {r['data_errors']}")
        for w in r["warnings_at_entry"]:
            print(f"      warning at entry: {w}")
    print(f"\n{len(rows)} losing {a.mode} trade(s) in the last {a.hours} h; classes: "
          f"{dict(Counter(r['loss_analysis']['classification'] for r in rows)) or '—'}")
    print(f"VOLATILITY: {vol['blocked_no_volatility']} of {vol['assessments_checked']} assessments blocked by AUTO_SL_NO_VOLATILITY; "
          f"{vol['low_confidence_needs_approval']} low-confidence (approval)")
    for reason, n in vol["by_reason"].items():
        print(f"    {n:5}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
