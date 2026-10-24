"""Forward test from the command line (yonixalpha_core.forward_test).

    $C exec -T api python -m yonixalpha_core.tools.forward_test status [--json]
    $C exec -T api python -m yonixalpha_core.tools.forward_test start --label "after deploy abc1234"
    $C exec -T api python -m yonixalpha_core.tools.forward_test history
    $C exec -T api python -m yonixalpha_core.tools.forward_test record-deployment --revision abc1234   (deploy.sh)

`status` is read-only. `start` writes only the forward-test record and an
audit-log entry: it opens a new period now (the previous one is ended and
kept) with the deployed revision, the configuration fingerprint, the data
sources' health and the frozen rules. `record-deployment` writes only the
deployment record. Nothing here trades or changes a mode or a setting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from yonixalpha_core import forward_test as ft
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis


def _n(v: Any, d: int = 5) -> str:
    return "-" if v is None else f"{v:+.{d}f}"


def render(r: dict[str, Any]) -> str:
    if r.get("state") == "NOT STARTED":
        return f"FORWARD TEST: NOT STARTED. {r['message']}"
    p = r["period"]
    dep = p.get("deployment") or {}
    out = [f"FORWARD TEST {p['id']} ({r['state']}): {p['label']}",
           f"  started {p['started_at'][:16]} UTC by {p.get('started_by') or '-'}; elapsed {r['elapsed_days']} days; "
           f"deployed revision {dep.get('revision', 'not recorded')} (deployed {str(dep.get('at', '-'))[:16]})",
           f"  data sources at the start: {(p.get('data_sources') or {}).get('overall', 'not recorded')}",
           f"  baseline: closed trades {r['baseline_window'][0][:16]} .. {r['baseline_window'][1][:16]}",
           f"  rules (frozen at the start): {json.dumps({k: v for k, v in r['rules'].items() if k != 'unit'})}",
           f"  configuration: {r['configuration_note']}"]
    for engine, s in r["strategies"].items():
        for mode, m in s["modes"].items():
            j = m["judgement"]
            ci, dci = j["expectancy_ci_sol"], j["difference_ci_sol"]
            out.append(f"\n  {engine} {mode}: {j['verdict']}")
            out.append(f"    forward: {j['closed_trades']} closed, {j['distinct_tokens']} tokens, net {_n(j['net_sol'])} SOL, "
                       f"per trade {_n(j['expectancy_sol'])} (95 % interval {_n(ci[0]) if ci else '-'} .. "
                       f"{_n(ci[1]) if ci else '-'}); best trade share of net {j['best_trade_share_of_net'] or '-'}")
            out.append(f"    baseline: {j['baseline_trades']} closed, per trade {_n(j['baseline_expectancy_sol'])}; "
                       f"difference interval {_n(dci[0]) if dci else '-'} .. {_n(dci[1]) if dci else '-'}")
            out.append(f"    edge: {j['edge']}")
            for g in j["evidence_gaps"]:
                out.append(f"    missing evidence: {g}")
            f, roll = m.get("forward") or {}, m.get("rolling_7d") or {}
            if f:
                out.append(f"    forward win {f.get('win_rate_pct')}%  PF {f.get('profit_factor')}  fees {_n(f.get('fees_sol'))}  "
                           f"max drawdown {_n(f.get('max_drawdown_sol'))}  rolling 7 d net {_n(roll.get('net_pnl_sol'))}")
            for day, d in (m.get("daily") or {}).items():
                out.append(f"      {day}  closed {d['trades']}  W/L {d['wins']}/{d['losses']}  net {_n(d['net_pnl_sol'])}  "
                           f"fees {_n(d['fees_sol'])}")
        for o in s["open_positions"]:
            out.append(f"    OPEN (not booked): {o['mode']} {o['count']} {o['status']}, cost {_n(o['cost_sol'])} SOL")
    out.append("\n  ledger decisions in the period (signals accepted, refused, watched), with the measured outcome:")
    for g in r["signals"][:40]:
        out.append(f"    {g['engine']:<17} {g['stage']:<11} {g['decision']:<22} {'traded' if g['traded'] else '-':<6} "
                   f"{g['reason'][:40]:<40} n {g['count']:<6} measured {g['measured']:<5} positive {g['positive']:<5} "
                   f"median exec {g['median_executable_return_pct']}%")
    out += [f"\n  note: {n}" for n in r["notes"]]
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="forward test")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    st.add_argument("--period", help="an earlier period id (see history)")
    s2 = sub.add_parser("start")
    s2.add_argument("--label", required=True)
    sub.add_parser("history")
    rd = sub.add_parser("record-deployment")
    rd.add_argument("--revision", required=True)
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, pool_size=1, max_overflow=0)
    redis = make_redis(settings) if args.cmd == "start" else None
    try:
        async with make_session_factory(engine)() as s:
            if args.cmd == "record-deployment":
                entry = await ft.record_deployment(s, args.revision)
                await s.commit()
                print(f"deployment recorded: {entry['revision']} at {entry['at'][:19]} UTC")
            elif args.cmd == "start":
                p = await ft.start(s, redis, label=args.label, user="cli")
                await s.commit()
                print(f"forward test {p['id']} started at {p['started_at'][:19]} UTC on revision "
                      f"{(p.get('deployment') or {}).get('revision', 'not recorded')}; data sources "
                      f"{(p.get('data_sources') or {}).get('overall')}. Status: tools.forward_test status")
            elif args.cmd == "history":
                for h in await ft.history(s):
                    print(f"{h['id']}  {h['state']:<7} {h['started_at'][:16]} .. {(h.get('ended_at') or 'now')[:16]}  "
                          f"{(h.get('deployment') or {}).get('revision', '-')}  {h['label']}")
            else:
                period = None
                if args.period:
                    period = next((h for h in ((await ft._get(s, ft.KEY)) or {}).get("history") or []
                                   if h.get("id") == args.period), None)
                    if period is None:
                        print(f"no ended period {args.period}; see: tools.forward_test history")
                        return 1
                r = await ft.report(s, None, period=period)
                print(json.dumps(r, indent=1, default=str) if args.json else render(r))
    finally:
        if redis is not None:
            await redis.aclose()
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
