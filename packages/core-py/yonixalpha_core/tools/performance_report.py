"""Performance report: what the Solana strategies earned, per strategy and
period, PAPER and LIVE apart, closed trades only. Read-only; prints no
secrets.

    $C exec -T api python -m yonixalpha_core.tools.performance_report [--section NAME ...] [--engine E]
                                                                       [--mode PAPER|LIVE] [--last 20] [--json]

Sections (default: positions day week all):
  positions    the latest positions one by one: token, mint, strategy, signal time, entry / exit time and
               price, size, gross, fees, net, entry slippage estimate, hold, exit reason; open positions
               show a marked value, labelled, never booked
  day          the last 24 hours
  week         the last 7 complete UTC days: one line per day, then the 7 days together
  all          all-time
  rejected     every ledger decision of the last --hours (accepted, refused, watched / skipped) with its
               first reason and the outcome the ledger measured afterwards
  predictions  Signal Center experiments, and shadow-model scores split into PROSPECTIVE (made before the
               outcome) and RETROSPECTIVE (counted, never evaluated)

For day / week / all, each strategy and mode: closed and open positions, gross and net PnL, fees (each cost
once), win / loss / breakeven, average and median win and loss, largest win and loss, profit factor (or why
it is undefined), expectancy after costs, return on the paper account's starting capital, maximum drawdown,
time in positions, trade frequency, the groups (token age, stage, market activity, entry quality, exit
reason) and the data coverage. A group with fewer than 20 trades is marked (small).

Live trade history and the wallet reconciliation, with refundable token-account rent kept apart from real
costs: tools.pnl_reconcile. Scanner and pipeline health: tools.momentum_pipeline and GET /api/system/scanner-health.
The forward test: tools.forward_test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from yonixalpha_core import forward_test, signal_center, solana_performance as sp
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import MlExperiment
from yonixalpha_core.ml import prediction_audit

SECTIONS = ("positions", "day", "week", "all", "rejected", "predictions")
DEFAULT = ("positions", "day", "week", "all")
ENGINES = forward_test.ENGINES
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _n(v: Any, d: int = 4) -> str:
    return "-" if v is None else f"{v:+.{d}f}" if isinstance(v, (int, float)) else str(v)


def _pc(v: Any) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def metrics_line(m: dict | None) -> str:
    if not m or not m.get("trades"):
        return "no closed trades"
    pf = m.get("profit_factor")
    return (f"closed {m['trades']}{' (small)' if m.get('anecdotal') else ''}  W/L/BE {m['wins']}/{m['losses']}/{m['breakeven']}"
            f"  win {m['win_rate_pct']}%  net {_n(m['net_pnl_sol'], 5)}  gross {_n(m['gross_pnl_sol'], 5)}"
            f"  fees {_n(m['fees_sol'], 5)}  exp/trade {_n(m['expectancy_sol'], 5)}"
            f"  PF {pf if pf is not None else '-'}  DD {_n(m.get('max_drawdown_sol'), 5)}")


def detail_lines(m: dict) -> list[str]:
    out = [f"      avg / median win {_n(m['avg_win_sol'], 5)} / {_n(m['median_win_sol'], 5)} SOL "
           f"({_pc(m['avg_win_pct'])} / {_pc(m['median_win_pct'])})   avg / median loss {_n(m['avg_loss_sol'], 5)} / "
           f"{_n(m['median_loss_sol'], 5)} SOL ({_pc(m['avg_loss_pct'])} / {_pc(m['median_loss_pct'])})",
           f"      largest win {_n(m['largest_win_sol'], 5)}  largest loss {_n(m['largest_loss_sol'], 5)}  "
           f"median hold {m['median_hold_s']} s  avg open at once {m.get('avg_open_positions', '-')}  "
           f"trades/day {m.get('trades_per_day', '-')}  return on starting capital "
           f"{_pc(m.get('return_on_starting_capital_pct'))}"]
    if m.get("profit_factor_note"):
        out.append(f"      profit factor {m['profit_factor_note']}")
    if m.get("return_note"):
        out.append(f"      return: {m['return_note']}")
    return out


def window_block(title: str, r: dict, *, groups: bool = True, daily: bool = False) -> list[str]:
    out = [f"\n{title}  [{r['since'][:16]} .. {r['until'][:16]} UTC]"]
    for mode in ("PAPER", "LIVE"):
        m = r["trades_by_mode"].get(mode)
        opened = [o for o in r["open_positions"] if o["mode"] == mode]
        failed = (r.get("failed_entries") or {}).get(mode)
        if not m and not opened and not failed:
            continue
        out.append(f"  {mode}: {metrics_line((m or {}).get('overall'))}")
        if m and m.get("overall") and m["overall"].get("trades"):
            out += detail_lines(m["overall"])
        for o in opened:
            out.append(f"      OPEN (not booked): {o['count']} {o['status']} position(s), cost {_n(o['cost_sol'], 5)} SOL, "
                       f"oldest entry {(o['oldest_entry'] or '-')[:16]}")
        if failed:
            out.append(f"      entries that never filled (failed, no cost, not trades, not open): {failed}")
        if m and daily:
            for day, d in m["by_day"].items():
                out.append(f"    {day}  {metrics_line(d)}")
        if m and groups:
            for dim, label in (("by_exit_reason", "exit reason"), ("by_stage", "stage"), ("by_age", "token age"),
                               ("by_regime", "market activity"), ("by_entry_quality", "entry quality")):
                for k, g in (m.get(dim) or {}).items():
                    out.append(f"    {label:<15} {k:<16} {metrics_line(g)}")
        cov = r["coverage"].get(mode)
        if cov:
            out.append(f"    coverage: {cov['closed']} closed; no ledger record {cov['no_ledger_record']}, fees not recorded "
                       f"{cov['fees_not_recorded']}, age unknown {cov['age_unknown']}, activity unknown "
                       f"{cov['activity_unknown']}, entry quality unknown {cov['entry_quality_unknown']}; "
                       f"QUIET = {cov['quiet_launches_per_hour']} launches/h, BUSY = {cov['busy_launches_per_hour']}")
    if len(out) == 1:
        out.append("  no closed or open positions")
    return out


def positions_block(rows: list[dict]) -> list[str]:
    out = ["\nLATEST POSITIONS (newest first; SOL; booked PnL only for closed positions)"]
    if not rows:
        return out + ["  none"]
    for p in rows:
        out.append(f"  {p['entry_at'][:19]}  {p['mode']:<5} {p['engine']:<17} {(p['symbol'] or '')[:10]:<10} {p['mint']}")
        out.append(f"      strategy {p['strategy'] or '-'}  signal {(p['signal_at'] or '-')[:19]}  status {p['status']}"
                   f"  size {_n(p['size_sol'], 4)}  entry {p['entry_price']} (decision {p['decision_price']}, slippage "
                   f"estimate {_pc(p['entry_slippage_estimate_pct'])})")
        if p["status"] == "closed":
            out.append(f"      exit {(p['exit_at'] or '-')[:19]} @ {p['exit_price']}  {p['exit_reason']}  hold {p['hold_s']} s  "
                       f"gross {_n(p['gross_pnl_sol'], 5)}  fees {_n(p['fees_sol'], 5)}  NET {_n(p['net_pnl_sol'], 5)} "
                       f"({_pc(p['net_pnl_pct'])})")
        else:
            out.append(f"      OPEN: marked {_n(p['unrealized_marked_sol'], 5)} SOL at {(p['marked_at'] or '-')[:19]} "
                       "(unrealized, not booked)")
    out.append("  slippage estimate = fill price vs the price the decision saw (impact, drift, AMM fee); it is already "
               "inside gross / net and never subtracted again")
    return out


async def build(s, sections: tuple[str, ...], engine: str | None, mode: str | None, last: int, hours: int,
                now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"at": now.isoformat()}
    engines = (engine,) if engine else ENGINES
    if "positions" in sections:
        out["positions"] = await sp.positions(s, engine=engine, mode=mode, limit=last)
    if "day" in sections:
        out["day"] = {e: await sp.report(s, since=now - timedelta(hours=24), until=now, engine=e, outcomes=False)
                      for e in engines}
    if "week" in sections:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out["week"] = {e: await sp.report(s, since=end - timedelta(days=7), until=end, engine=e, outcomes=False)
                       for e in engines}
    if "all" in sections:
        out["all"] = {e: await sp.report(s, since=EPOCH, until=now, engine=e, outcomes=False) for e in engines}
    if "rejected" in sections:
        out["rejected"] = await forward_test.signals(s, now - timedelta(hours=hours), now, engines)
    if "predictions" in sections:
        exps = (await s.execute(select(MlExperiment).where(MlExperiment.status.in_(("ACCEPTED", "RETIRED")))
                                .order_by(MlExperiment.created_at.desc()).limit(20))).scalars().all()
        out["experiments"] = [await signal_center.report(s, e.id) for e in exps]
        out["shadow_predictions"] = await prediction_audit.audit(s, now - timedelta(hours=hours), now)
    return out


def render(r: dict[str, Any], hours: int) -> str:
    out = [f"PERFORMANCE REPORT at {r['at'][:16]} UTC: closed trades only, PAPER and LIVE apart; open positions are never booked"]
    if "positions" in r:
        out += positions_block(r["positions"])
    for key, title, daily in (("day", "LAST 24 HOURS", False), ("week", "LAST 7 COMPLETE DAYS", True), ("all", "ALL TIME", False)):
        for e, rep in (r.get(key) or {}).items():
            out += window_block(f"{title}: {e}", rep, daily=daily)
    if "rejected" in r:
        out.append(f"\nLEDGER DECISIONS, last {hours} h (count; outcome measured / positive; median executable return "
                   "at T+5m and median peak)")
        for g in r["rejected"]:
            out.append(f"  {g['engine']:<17} {g['stage']:<11} {g['decision']:<22} {'traded' if g['traded'] else 'not traded':<10} "
                       f"{g['reason'][:45]:<45} n {g['count']:<6} measured {g['measured']:<5} positive {g['positive']:<5} "
                       f"exec {_pc(g['median_executable_return_pct'])} peak {_pc(g['median_peak_pct'])}")
        if not r["rejected"]:
            out.append("  none recorded")
        unmeasured = sorted({g["engine"] for g in r["rejected"]} - {g["engine"] for g in r["rejected"] if g["measured"]})
        for e in unmeasured:
            out.append(f"  {e}: no decision has a measured outcome. The executable return is simulated on the bonding "
                       "curve only; " + ("migrated tokens trade on a PumpSwap pool, which the ledger does not simulate"
                                         if e == "solana_migration" else "none was measurable in this window")
                       + ", so these decisions cannot be judged from this table (DATA GAP)")
    if "experiments" in r:
        out.append("\nSIGNAL CENTER EXPERIMENTS (predictions recorded before their outcome)")
        for x in r["experiments"]:
            e = x["experiment"]
            out.append(f"  {e['status']:<9} {e['idea'][:60]:<60} signals {x['signals']} evaluated {x['evaluated']} "
                       f"verdict {x['verdict']['state']}: {x['verdict']['reason']}")
        if not r["experiments"]:
            out.append("  no accepted experiment")
        a = r["shadow_predictions"]
        out.append(f"\nSHADOW MODEL SCORES, last {hours} h: {a['rows_scored']} ledger rows scored")
        for name, c in a["models"].items():
            out.append(f"  {name:<40} scored {c['scored']:<6} PROSPECTIVE {c['PROSPECTIVE']:<6} RETROSPECTIVE "
                       f"{c['RETROSPECTIVE']:<6} prospective with outcome {c['prospective_with_outcome']}")
        rm = a["return_model_prospective"]
        out.append(f"  return model, prospective only: favoured n={rm['favoured']['n']} mean "
                   f"{_pc(rm['favoured']['mean_executable_return_pct'])}, not favoured n={rm['not_favoured']['n']} mean "
                   f"{_pc(rm['not_favoured']['mean_executable_return_pct'])}, all {_pc(rm['all']['mean_executable_return_pct'])} "
                   f"({rm['note']})")
    out.append("\nNot evidence of profitability by itself: read sample sizes, the result without the best trade "
               "(tools.forward_test) and costs. Nothing was written.")
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--section", action="append", choices=SECTIONS, help="repeatable; default positions day week all")
    ap.add_argument("--engine", choices=ENGINES)
    ap.add_argument("--mode", choices=("PAPER", "LIVE"), help="positions section only; the windows always show both apart")
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--hours", type=int, default=24, help="rejected / predictions window")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
    try:
        async with make_session_factory(engine)() as s:
            r = await build(s, tuple(args.section or DEFAULT), args.engine, args.mode, args.last, args.hours,
                            datetime.now(timezone.utc))
    finally:
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else render(r, args.hours))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
