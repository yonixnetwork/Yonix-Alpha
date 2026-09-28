"""Trade report: measured latency and price execution of recent LIVE orders.
Read-only; prints no secrets.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.trade_report [--last 20] [--side BUY|SELL] [--json]

Orders placed before execution diagnostics existed are analysed from what
they recorded (stage timestamps, fill, the last log lines); the decision
price then falls back to the position's planned entry price and the
decision time to the assessment's evaluation time. What cannot be measured
is printed as "—", never estimated.
"""

import argparse
import asyncio
import json
import statistics
from collections import Counter
from typing import Any

from sqlalchemy import select

from yonixalpha_core import execution_analysis as xa
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, RiskAssessment, TradingCandidate
from yonixalpha_core.live_trading import failure_code_of

METRICS = ("decision_eval_ms", "queue_wait_ms", "quote_latency_ms", "build_ms", "guard_and_recheck_ms", "simulation_ms",
           "submission_latency_ms", "submit_to_confirm_ms", "decision_to_submit_ms", "decision_to_confirm_ms",
           "rpc_latency_before_submit_ms", "slots_to_land")


async def analyse(session, order: ExecutionOrder, wallet: str | None = None) -> dict[str, Any]:
    position = await session.get(PaperPosition, order.position_id) if order.position_id else None
    cand = await session.get(TradingCandidate, position.candidate_id) if position is not None and position.candidate_id else None
    diag = dict(order.diagnostics or {})
    if not diag.get("decision"):
        # Before diagnostics: reconstruct only what was recorded.
        decision: dict[str, Any] = {"reconstructed": True}
        if order.assessment_id:
            a = await session.get(RiskAssessment, order.assessment_id)
            if a is not None and order.side == "BUY":
                decision["decision_at"] = a.evaluated_at.isoformat()
                decision["approval_at"] = a.evaluated_at.isoformat()
        if order.side == "BUY" and position is not None and (position.plan or {}).get("entry_price"):
            decision["price_sol"] = str(position.plan["entry_price"])
        diag["decision"] = decision
    result = dict(order.result or {})
    if order.status == "CONFIRMED" and not result.get("trade_event") and position is not None:
        if wallet:
            try:
                result["trade_event"] = xa.own_trade_event(result.get("logs") or [], wallet, order.mint)
            except Exception:  # noqa: BLE001
                result["trade_event"] = None
    shadow = type("O", (), {})()
    for k in ("created_at", "status", "side", "amount", "amount_kind", "priority_fee_sol", "limits"):
        setattr(shadow, k, getattr(order, k))
    shadow.result, shadow.diagnostics = result, diag
    out = xa.analyze(shadow, position, cand)
    return {"order_id": str(order.id), "side": order.side, "mint": order.mint, "status": order.status,
            "reason": order.reason, "created_at": order.created_at.isoformat(), "signature": order.signature,
            "failure_code": None if order.status == "CONFIRMED" else failure_code_of(order.side, order.status, order.error,
                                                                                    order.signature, order.result),
            "error": order.error, "symbol": position.symbol if position else None,
            "position_status": position.status if position else None,
            "realized_pnl_sol": str(position.realized_pnl) if position is not None and position.realized_pnl is not None else None,
            "timing": out.get("timing"), "price": out.get("price"), "decision": out.get("decision"),
            "priority_fee_sol": str(order.priority_fee_sol),
            "compute_unit_limit": next((st.get("compute_unit_limit") for st in reversed((order.result or {}).get("stages") or [])
                                        if st.get("stage") == "TRANSACTION_BUILT"), None)}


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def summary(rows: list[dict]) -> dict[str, Any]:
    agg: dict[str, Any] = {}
    for m in METRICS:
        vals = [r["timing"][m] for r in rows if r.get("timing") and isinstance(r["timing"].get(m), (int, float))]
        if vals:
            agg[m] = {"n": len(vals), "avg": round(statistics.mean(vals)), "median": round(statistics.median(vals)),
                      "worst": max(vals)}
    agg["classifications"] = dict(Counter((r.get("price") or {}).get("classification") for r in rows
                                          if r["status"] == "CONFIRMED" and r["side"] == "BUY"))
    agg["failures"] = dict(Counter(r["failure_code"] for r in rows if r.get("failure_code")))
    # Does a higher priority fee (or tighter CU limit) land faster? Measured per setting.
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] == "CONFIRMED" and r.get("timing"):
            groups.setdefault(f"fee {r.get('priority_fee_sol')} SOL / CU limit {r.get('compute_unit_limit') or '—'}", []).append(r["timing"])
    agg["by_priority_setting"] = {
        k: {"n": len(v),
            "avg_submit_to_confirm_ms": round(statistics.mean(x)) if (x := [t["submit_to_confirm_ms"] for t in v
                                                                           if isinstance(t.get("submit_to_confirm_ms"), int)]) else None,
            "avg_slots_to_land": round(statistics.mean(y), 1) if (y := [t["slots_to_land"] for t in v
                                                                        if isinstance(t.get("slots_to_land"), int)]) else None}
        for k, v in groups.items()}
    return agg


def wallet_public_key(settings) -> str | None:
    """The live wallet's PUBLIC key (only it is used, to find our own trade
    events and accounts); None when no wallet is configured."""
    wallet = getattr(settings, "WALLET_PUBLIC_KEY", None)
    if wallet:
        return wallet
    from yonixalpha_core.solana.wallet import load_wallet
    try:
        w = load_wallet(settings)
        return w.pubkey if w else None
    except Exception:  # noqa: BLE001
        return None


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--side", choices=("BUY", "SELL"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    settings = get_settings()
    wallet = wallet_public_key(settings)
    engine = make_engine(settings)
    async with make_session_factory(engine)() as s:
        q = select(ExecutionOrder).where(ExecutionOrder.mode == "LIVE").order_by(ExecutionOrder.created_at.desc())
        if a.side:
            q = q.where(ExecutionOrder.side == a.side)
        orders = (await s.execute(q.limit(a.last))).scalars().all()
        rows = [await analyse(s, o, wallet) for o in orders]
    await engine.dispose()
    agg = summary(rows)
    if a.json:
        print(json.dumps({"orders": rows, "summary": agg}, default=str, indent=1))
        return 0
    for r in rows:
        t, p = r.get("timing") or {}, r.get("price") or {}
        print(f"=== {r['created_at'][:19]} {r['side']:4} {r['symbol'] or r['mint'][:8]:12} {r['status']:9} {r['reason']}"
              f"{'  ' + r['failure_code'] if r['failure_code'] else ''}")
        print(f"    decision→submit {_fmt(t.get('decision_to_submit_ms'))} ms | decision→confirm {_fmt(t.get('decision_to_confirm_ms'))} ms"
              f" | eval {_fmt(t.get('decision_eval_ms'))} | queue {_fmt(t.get('queue_wait_ms'))} | quote {_fmt(t.get('quote_latency_ms'))}"
              f" | build {_fmt(t.get('build_ms'))} | guard+recheck {_fmt(t.get('guard_and_recheck_ms'))}"
              f" | simulate {_fmt(t.get('simulation_ms'))} | submit {_fmt(t.get('submission_latency_ms'))}"
              f" | confirm {_fmt(t.get('submit_to_confirm_ms'))} | slots {_fmt(t.get('slots_to_land'))}")
        if p:
            c = p.get("components_pct") or {}
            print(f"    price: decision {_fmt(p.get('decision_price_sol'))} → build spot {_fmt(p.get('spot_at_build_sol'))}"
                  f" → before our trade {_fmt(p.get('spot_before_trade_sol'))} → trade {_fmt(p.get('trade_price_sol'))}"
                  f" → all-in {_fmt(p.get('all_in_price_sol'))}")
            print(f"    moves %: decision→build {_fmt(c.get('decision_to_build_pct'))} | build→landing {_fmt(c.get('build_to_landing_pct'))}"
                  f" | impact {_fmt(c.get('price_impact_pct'))} | fees {_fmt(c.get('fees_pct'))}"
                  f" | total vs decision {_fmt(c.get('total_vs_decision_pct'))}")
            if p.get("classification"):
                print(f"    cause: {p['classification']}  {'; '.join(p.get('evidence') or [])}")
        if r.get("error") and r["status"] != "CONFIRMED":
            print(f"    error: {r['error'][:200]}")
    print("\nSUMMARY (ms unless noted; n = orders that recorded the stage)")
    for m in METRICS:
        v = agg.get(m)
        if v:
            print(f"  {m:30} n={v['n']:<3} avg {v['avg']:<7} median {v['median']:<7} worst {v['worst']}")
    print(f"  price causes (confirmed buys): {agg['classifications'] or '—'}")
    print(f"  failures: {agg['failures'] or '—'}")
    print("  by priority setting (confirmed orders; compare only with enough samples):")
    for k, v in agg["by_priority_setting"].items():
        print(f"    {k:48} n={v['n']:<3} submit→confirm {v['avg_submit_to_confirm_ms']} ms, slots to land {v['avg_slots_to_land']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

