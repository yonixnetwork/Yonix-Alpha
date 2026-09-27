"""Where did every Pump.fun token stop? Read-only, from the live database.

    python -m yonixalpha_core.tools.execution_funnel --hours 24
    python -m yonixalpha_core.tools.execution_funnel --mint <MINT>     # one token, evaluation by evaluation
    python -m yonixalpha_core.tools.execution_funnel --code STOP_INSIDE_COSTS   # the numbers behind a blocking code
    python -m yonixalpha_core.tools.execution_funnel --json

On the server:
    $C run --rm decision-engine python -m yonixalpha_core.tools.execution_funnel --hours 24
"""

import argparse
import asyncio
import json
import sys

from redis.asyncio import from_url

from yonixalpha_core import execution_funnel
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory


def _print_funnel(f: dict) -> None:
    print(f"EXECUTION FUNNEL since {f['since']}")
    print("\nMODES:", json.dumps({k: v for k, v in f["modes"].items() if k not in ("wallet", "live_worker")}, default=str))
    w = f["modes"].get("wallet")
    if w:
        print(f"WALLET: {w.get('sol')} SOL at {w.get('at')}")
    print("\nOBSERVED (fresh observation outcomes):", f["observed"] or "none")
    print("CANDIDATES (engine -> state counts):", json.dumps(f["candidates"]) if f["candidates"] else "none")
    print("\nSTAGES per engine:")
    for engine, s in f["stages"].items():
        print(f"  {engine:17} assessed {s['tokens']:5} tokens ({s['assessments']} evaluations) | BUY signal {s['signal_tokens']:4} "
              f"| EXECUTABLE {s['executable_tokens']:4} (paper {s['executable_paper']}, live {s['executable_live']})")
    print("\nDECISIONS:")
    for engine, d in f["decisions"].items():
        print(f"  {engine:17} " + ", ".join(f"{k} {v['tokens']}t/{v['assessments']}e" for k, v in sorted(d.items())))
    print("\nBLOCKED WITH A BUY SIGNAL (code -> tokens):")
    for b in f["blocked_with_buy_signal"][:20]:
        print(f"  {b['engine']:17} {b['code']:32} {b['decision']:24} {b['tokens']:5} tokens  e.g. {str(b['example'])[:110]}")
    if f["approval_drivers"]:
        print("\nHIGH FINDINGS BEHIND 'NEEDS APPROVAL' (AUTO -> NO_TRADE):")
        for d in f["approval_drivers"][:15]:
            print(f"  {d['engine']:17} {d['code']:32} action {d['action']:24} {d['tokens']:5} tokens")
    if f["near_misses"]:
        print("\nNEAR MISSES (BUY signal, exactly one blocking code):")
        for n in f["near_misses"]:
            print(f"  {n['engine']:17} {n['code']:32} {n['tokens']:5} tokens")
    print("\nALL BLOCKING CODES (any signal):")
    for b in f["blocked_all"][:20]:
        print(f"  {b['engine']:17} {b['code']:32} {b['decision']:24} {b['tokens']:5} tokens")
    if f.get("data_errors"):
        print("\nDATA ERRORS behind *_UNAVAILABLE / *_STALE (addresses masked):")
        for e in f["data_errors"]:
            print(f"  {e['engine']:17} {e['tokens']:5} tokens  {e['error']}")
    print("\nPOSITIONS OPENED:", json.dumps(f["positions"]) if f["positions"] else "none")
    print("LIVE ORDERS:", json.dumps(f["orders"]) if f["orders"] else "none")
    for e in f["order_errors"]:
        print(f"  order error ({e['side']}, {e['n']}x): {e['error']}")
    if f["order_latency_median_seconds"]:
        print("ORDER LATENCY (median s):", json.dumps(f["order_latency_median_seconds"], default=str))
    if f["execution_failures"]:
        print("\nEXECUTION FAILURES:")
        for e in f["execution_failures"]:
            print(f"  {e['event_type']:22} {e['n']:4}x  {e['reason']}")
    print("\nDIAGNOSIS:")
    for n in f["diagnosis"] or ["no activity in this window"]:
        print(f"  - {n}")


def _print_trace(t: dict) -> None:
    print(f"TOKEN {t['mint']}")
    for o in t["observation"]:
        print(f"  observation: {o['outcome']} trend={o['trend']} at {o['decided_at']} — {(o['reasons'] or [''])[-1]}")
    for c in t["candidates"]:
        print(f"  candidate {c['engine']}: state {c['state']} (created {c['created_at']})")
        for h in (c["state_history"] or [])[-8:]:
            print(f"      {h.get('at')} -> {h.get('state')}: {str(h.get('reason') or '')[:120]}")
    for a in t["assessments"]:
        codes = ", ".join(b["code"] for b in (a["blocking"] or [])) or "-"
        print(f"  {a['evaluated_at']} {a['engine']:16} signal={'BUY' if a['buy_signal'] else 'no ':3} "
              f"{a['decision']:24} target={a['execution_target']:5} size={a['size']} blocking: {codes}")
    for p in t["positions"]:
        print(f"  POSITION {p['execution_mode']} {p['status']} entry {p['entry_at']} @ {p['entry_price']} "
              f"lifecycle={p['lifecycle']} route={p['execution_route']} exit={p['exit_reason']} pnl={p['realized_pnl']}")
    for o in t["orders"]:
        print(f"  ORDER {o['side']} {o['reason']} {o['status']} sig={o['signature']} err={o['error']}")
    for e in t.get("position_events", []):
        d = e["detail"] or {}
        why = "; ".join(d.get("reasons") or []) or d.get("reason") or d.get("error") or ""
        print(f"  EVENT {e['occurred_at']} {e['event_type']}: {str(why)[:200]}")


async def main(hours: float, mint: str | None, as_json: bool, code: str | None = None) -> int:
    settings = get_settings()
    engine = make_engine(settings)
    redis = from_url(settings.redis_url, decode_responses=True)
    try:
        async with make_session_factory(engine)() as session:
            if code:
                rows = await execution_funnel.code_examples(session, code, execution_funnel.since_hours(hours))
                print(json.dumps(rows, default=str, indent=2) if rows else f"no assessment with {code} in the last {hours} h")
            elif mint:
                data = await execution_funnel.token_trace(session, mint)
                print(json.dumps(data, default=str, indent=2)) if as_json else _print_trace(data)
            else:
                data = await execution_funnel.funnel(session, execution_funnel.since_hours(hours), redis, settings)
                print(json.dumps(data, default=str, indent=2)) if as_json else _print_funnel(data)
    finally:
        await redis.aclose()
        await engine.dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--mint")
    ap.add_argument("--code", help="show the numbers behind a blocking code, e.g. STOP_INSIDE_COSTS")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.hours, a.mint, a.json, a.code)))
