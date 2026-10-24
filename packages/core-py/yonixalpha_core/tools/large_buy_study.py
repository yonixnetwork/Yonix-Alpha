"""Large-buy study (read-only): after a large buy, do new buyers arrive and do
earlier holders sell into it, compared with ordinary moments? And would
buying right after one have paid after costs? (yonixalpha_core.large_trades)

    $C exec -T api python -m yonixalpha_core.tools.large_buy_study [--days 14] [--json]

Uses COMPLETE event / control pairs of one definition version. Split by
time: the first 60 % of events (exploration) and the last 40 % (hold-out,
unseen when the question was framed). For each horizon and measure: the mean
after a large buy, the mean at control moments, the difference with a 95 %
bootstrap interval, and the number of pairs.

A finding counts only if it has the same sign in both periods and its hold-out
interval excludes 0. "After costs" subtracts COST_PCT, an ESTIMATE of a round
trip on the curve (two 1.25 % Pump.fun fees plus the fixed network costs of a
0.05 SOL trade), not a measured fill. Large buyers that are the token's
creator are reported apart; other wallet relations are unknown. Nothing is
written; nothing here is a trading rule.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from yonixalpha_core import large_trades as lt
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import LargeTradeEvent
from yonixalpha_core.forward_test import bootstrap_diff_ci, bootstrap_mean_ci

MEASURES = ("return_pct", "net_flow_sol", "new_buyers", "prior_holder_sells", "prior_holder_sell_sol")
COST_PCT = 3.0  # estimate: 2 x 1.25 % curve fee + ~0.5 % fixed costs on a 0.05 SOL round trip
MIN_PAIRS = 30


def _v(row: LargeTradeEvent | None, horizon: str, measure: str) -> float | None:
    if row is None or not row.post:
        return None
    x = (row.post.get(horizon) or {}).get(measure)
    return None if x is None else float(x)


def compare(pairs: list[tuple[LargeTradeEvent, LargeTradeEvent | None]], horizon: str, measure: str) -> dict[str, Any]:
    ev = [v for e, _ in pairs if (v := _v(e, horizon, measure)) is not None]
    ct = [v for _, c in pairs if (v := _v(c, horizon, measure)) is not None]
    diff = bootstrap_diff_ci(ev, ct) if ev and ct else None
    return {"n_event": len(ev), "n_control": len(ct), "event_mean": round(statistics.fmean(ev), 4) if ev else None,
            "control_mean": round(statistics.fmean(ct), 4) if ct else None,
            "difference_ci": [round(diff[0], 4), round(diff[1], 4)] if diff else None}


def finding(explore: dict, hold: dict) -> str:
    if (hold["n_event"] < MIN_PAIRS) or not hold["difference_ci"] or not explore["difference_ci"]:
        return "INSUFFICIENT DATA"
    lo, hi = hold["difference_ci"]
    e_sign = (explore["event_mean"] or 0) - (explore["control_mean"] or 0)
    if lo > 0 and e_sign > 0:
        return "HIGHER AFTER LARGE BUYS (confirmed on the hold-out)"
    if hi < 0 and e_sign < 0:
        return "LOWER AFTER LARGE BUYS (confirmed on the hold-out)"
    return "NOT CONFIRMED"


async def build(session, days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(LargeTradeEvent).where(
        LargeTradeEvent.at >= now - timedelta(days=days), LargeTradeEvent.definition_version == lt.DEFINITION_VERSION)
        .order_by(LargeTradeEvent.at).limit(100000))).scalars().all()
    events = [r for r in rows if r.kind == "EVENT"]
    controls = {r.pair_id: r for r in rows if r.kind == "CONTROL" and r.status == "COMPLETE"}
    status: dict[str, int] = {}
    for r in rows:
        status[f"{r.kind} {r.status}"] = status.get(f"{r.kind} {r.status}", 0) + 1
    done = [(e, controls.get(e.id)) for e in events if e.status == "COMPLETE"]
    cut = int(len(done) * 0.6)
    explore, hold = done[:cut], done[cut:]
    tables = {}
    for h in lt.HORIZONS:
        for m in MEASURES:
            a, b = compare(explore, h, m), compare(hold, h, m)
            tables[f"{h} {m}"] = {"exploration": a, "hold_out": b, "finding": finding(a, b)}
    rets = {h: [v - COST_PCT for e, _ in hold if (v := _v(e, h, "return_pct")) is not None] for h in lt.HORIZONS}
    after_cost = {h: {"n": len(v), "mean_pct": round(statistics.fmean(v), 3) if v else None,
                      "ci": [round(x, 3) for x in ci] if (ci := bootstrap_mean_ci(v)) else None}
                  for h, v in rets.items()}
    sold = [x for e, _ in done if (x := _v(e, "5m", "large_buyer_sold_share")) is not None]
    creator = sum(1 for e in events if (e.pre or {}).get("large_buyer_is_creator"))
    return {"since": (now - timedelta(days=days)).isoformat(), "definition": lt.DEFINITION_VERSION, "rows_by_status": status,
            "complete_pairs": len(done), "with_control": sum(1 for _, c in done if c is not None),
            "exploration_pairs": len(explore), "hold_out_pairs": len(hold),
            "hold_out_start": hold[0][0].at.isoformat() if hold else None, "comparisons": tables,
            "buy_after_large_buy_net_of_cost_estimate": after_cost, "cost_estimate_pct": COST_PCT,
            "large_buyer_sold_within_5m": {"n": len(sold), "share_sold_any": round(sum(1 for x in sold if x > 0) / len(sold), 3)
                                           if sold else None,
                                           "median_share_sold": round(statistics.median(sold), 3) if sold else None},
            "large_buyer_is_creator": creator, "events": len(events)}


def render(r: dict[str, Any]) -> str:
    out = [f"LARGE-BUY STUDY since {r['since'][:16]} ({r['definition']}): {r['events']} events; complete pairs "
           f"{r['complete_pairs']} ({r['with_control']} with a control); exploration {r['exploration_pairs']}, hold-out "
           f"{r['hold_out_pairs']} from {(r['hold_out_start'] or '-')[:16]}",
           f"rows: {r['rows_by_status']}",
           f"large buyer is the token's creator in {r['large_buyer_is_creator']} event(s); other relations unknown",
           "\nEVENT vs CONTROL (mean after a large buy, mean at ordinary moments, 95 % interval of the difference)"]
    for k, t in r["comparisons"].items():
        e, h = t["exploration"], t["hold_out"]
        out.append(f"  {k:<28} explore {e['event_mean']} vs {e['control_mean']} (n {e['n_event']}/{e['n_control']})  "
                   f"hold-out {h['event_mean']} vs {h['control_mean']} (n {h['n_event']}/{h['n_control']}) "
                   f"diff {h['difference_ci']}  -> {t['finding']}")
    out.append(f"\nBUYING RIGHT AFTER A LARGE BUY, hold-out, return minus a {r['cost_estimate_pct']} % cost ESTIMATE")
    for h, a in r["buy_after_large_buy_net_of_cost_estimate"].items():
        out.append(f"  {h:<4} n {a['n']:<5} mean {a['mean_pct']} %  95 % interval {a['ci']}")
    s = r["large_buyer_sold_within_5m"]
    out.append(f"\nlarge buyer sold within 5 min: in {s['share_sold_any']} of {s['n']} events; median share sold {s['median_share_sold']}")
    out.append(f"\nA finding needs at least {MIN_PAIRS} hold-out pairs, the same sign in both periods and a hold-out interval "
               "that excludes 0. This is research, not a trading rule. Nothing was written.")
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="large-buy study, read-only")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
    try:
        async with make_session_factory(engine)() as s:
            r = await build(s, args.days)
    finally:
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
