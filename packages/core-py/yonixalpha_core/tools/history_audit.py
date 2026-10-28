"""Full-history audit (read-only): every stored Solana position and ledger
decision from the first record on, PAPER and LIVE apart. Prints no secrets;
nothing is written. Runs in one READ ONLY transaction with a statement timeout.

    $C exec -T api python -m yonixalpha_core.tools.history_audit [--section NAME ...] [--engine E]
        [--since 2026-09-25] [--until 2026-10-27] [--json]

Times are UTC (the server and database run in UTC); a day is 00:00-24:00 UTC,
an hour is the UTC hour of the exit.

Sections (default: all)
  range        earliest / latest row and approximate row count of every table the audit reads, and the data
               retention setting: what can and cannot be traced back
  positions    per strategy and mode: closed / open / failed / other positions; then the full metrics of the
               closed ones (solana_performance: gross, net, fees once, win rate, average / median win and loss,
               profit factor, expectancy, return on the paper account's starting capital, maximum drawdown,
               largest win and loss, hold) and the groups by exit reason, token age, stage, entry quality,
               market activity
  daily        net per UTC day, per strategy and mode, all history
  hourly       net per UTC hour of the exit, all history
  weekly       net per ISO week (Monday 00:00 UTC), per strategy and mode
  versions     per strategy version (paper_positions.strategy) and model version
  concentration
               are the losses a few tokens or many small ones? top losing / winning tokens and their share,
               how many trades make up 80 % of the losses, losses by size band
  ledger       every ledger decision per strategy: decision x traded, unresolved (still TRACKING), outcome
               measured, and the measured executable return (0.05 SOL reference trade after fees, impact,
               latency and fixed costs) of traded vs not traded decisions: missed opportunities
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core import forward_test, solana_performance as sp
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory

SECTIONS = ("range", "positions", "daily", "hourly", "weekly", "versions", "concentration", "ledger")
ENGINES = forward_test.ENGINES
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# (table, time column) read by the audit, and what each holds
TABLES = (
    ("paper_positions", "entry_at", "positions, PAPER and LIVE (kept)"),
    ("opportunity_outcomes", "decided_at", "ledger: every decision with its snapshot and outcome (kept)"),
    ("risk_assessments", "evaluated_at", "every gate evaluation (pruned after the retention days)"),
    ("trading_candidates", "created_at", "candidates and their state history (pruned)"),
    ("trade_timeline_events", "occurred_at", "per-position events, partial exits (pruned)"),
    ("execution_orders", "created_at", "LIVE orders"),
    ("entry_signals", "decided_at", "shadow entry-strategy signals"),
    ("model_versions", "created_at", "every trained model version (kept)"),
    ("system_events", "created_at", "service warnings / errors (info and warning pruned)"),
    ("market_samples", "at", "SOL/USD and stream activity every 5 min (from 2026-10-25)"),
    ("large_trade_events", "at", "large-buy observations (from 2026-10-24)"),
)

POS_FILTER = ("pp.engine = ANY(:engines) AND (CAST(:since AS timestamptz) IS NULL OR pp.entry_at >= :since) "
              "AND (CAST(:until AS timestamptz) IS NULL OR pp.entry_at < :until)")
CLOSED = POS_FILTER + " AND pp.status = 'closed' AND pp.exit_at IS NOT NULL"

STATUS_SQL = f"""
SELECT pp.engine, coalesce(pp.execution_mode, 'PAPER') AS mode, pp.status, count(*) AS n,
       min(pp.entry_at) AS first_entry, max(pp.entry_at) AS last_entry
FROM paper_positions pp WHERE {POS_FILTER} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

BUCKET_SQL = """
SELECT pp.engine, coalesce(pp.execution_mode, 'PAPER') AS mode, {bucket} AS k, count(*) AS n,
       count(*) FILTER (WHERE pp.realized_pnl > 0) AS wins, sum(coalesce(pp.realized_pnl, 0)) AS net,
       sum(coalesce(pp.fees_paid_quote, 0)) AS fees
FROM paper_positions pp WHERE {where} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

BUCKETS = {
    "daily": "to_char(pp.exit_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
    "hourly": "to_char(pp.exit_at AT TIME ZONE 'UTC', 'HH24')",
    "weekly": "to_char(date_trunc('week', pp.exit_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
    "versions": "coalesce(pp.strategy, '-') || ' | model ' || coalesce(pp.model_version, '-')",
}

TOKENS_SQL = f"""
SELECT pp.engine, coalesce(pp.execution_mode, 'PAPER') AS mode, pp.asset_id AS mint, max(pp.symbol) AS symbol,
       count(*) AS n, sum(coalesce(pp.realized_pnl, 0)) AS net
FROM paper_positions pp WHERE {CLOSED} GROUP BY 1, 2, 3
"""

TRADES_SQL = f"""
SELECT pp.engine, coalesce(pp.execution_mode, 'PAPER') AS mode, coalesce(pp.realized_pnl, 0) AS pnl,
       pp.realized_pnl_pct AS pnl_pct
FROM paper_positions pp WHERE {CLOSED}
"""

LEDGER_SQL = """
SELECT engine, stage, decision, traded, count(*) AS n,
       count(*) FILTER (WHERE status = 'TRACKING') AS unresolved,
       count(*) FILTER (WHERE executable_return_pct IS NOT NULL) AS measured,
       count(*) FILTER (WHERE executable_return_pct > 0) AS positive,
       avg(executable_return_pct) AS mean_exec, percentile_cont(0.5) WITHIN GROUP (ORDER BY executable_return_pct) AS med_exec,
       min(executable_return_pct) AS min_exec, max(executable_return_pct) AS max_exec,
       min(decided_at) AS first_at, max(decided_at) AS last_at
FROM opportunity_outcomes
WHERE engine = ANY(:engines) AND (CAST(:since AS timestamptz) IS NULL OR decided_at >= :since)
  AND (CAST(:until AS timestamptz) IS NULL OR decided_at < :until)
GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 5 DESC
"""


def _f(v: Any, d: int = 6) -> float | None:
    return None if v is None else round(float(v), d)


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


async def table_range(s) -> list[dict[str, Any]]:
    out = []
    for table, col, what in TABLES:
        try:
            async with s.begin_nested():
                r = (await s.execute(text(f"SELECT min({col}) AS lo, max({col}) AS hi FROM {table}"))).one()
                est = (await s.execute(text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"), {"t": table})).scalar()
            out.append({"table": table, "what": what, "earliest": _iso(r.lo), "latest": _iso(r.hi),
                        "approx_rows": int(est) if est is not None and est >= 0 else None})
        except Exception as exc:  # noqa: BLE001 - a missing table is reported, never fatal
            out.append({"table": table, "what": what, "error": f"{type(exc).__name__}"})
    ret = (await s.execute(text("SELECT value FROM platform_settings WHERE key = 'data_retention'"))).scalar_one_or_none()
    return out + [{"table": "data_retention setting", "what": json.dumps(ret) if ret else "not set (retention off)"}]


async def buckets(s, kind: str, p: dict) -> list[dict[str, Any]]:
    sql = BUCKET_SQL.format(bucket=BUCKETS[kind], where=CLOSED)
    return [{"engine": r.engine, "mode": r.mode, "key": r.k, "trades": int(r.n), "wins": int(r.wins),
             "net_sol": _f(r.net), "fees_sol": _f(r.fees)} for r in (await s.execute(text(sql), p)).all()]


def concentration(tokens: list, trades: list) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted({(t.engine, t.mode) for t in tokens}):
        toks = [t for t in tokens if (t.engine, t.mode) == key]
        pnl = sorted(float(t.pnl) for t in trades if (t.engine, t.mode) == key)
        losses = [x for x in pnl if x < 0]
        gross_loss = -sum(losses)
        acc, k80 = 0.0, 0
        for x in losses:  # largest losses first (sorted ascending)
            acc += -x
            k80 += 1
            if gross_loss and acc >= 0.8 * gross_loss:
                break
        worst = sorted(toks, key=lambda t: float(t.net))[:5]
        best = sorted(toks, key=lambda t: -float(t.net))[:5]
        # realized_pnl_pct is stored as a fraction of the entry cost (-0.34 = -34 %)
        pcts = [100 * float(t.pnl_pct) for t in trades if (t.engine, t.mode) == key and t.pnl_pct is not None and t.pnl < 0]
        net = sum(pnl)
        out[f"{key[0]} {key[1]}"] = {
            "trades": len(pnl), "tokens": len(toks), "net_sol": round(net, 6), "gross_loss_sol": round(gross_loss, 6),
            "losses": len(losses), "losses_making_80pct_of_gross_loss": k80,
            "worst_tokens": [{"symbol": t.symbol, "mint": t.mint, "trades": int(t.n), "net_sol": _f(t.net)} for t in worst],
            "worst5_share_of_gross_loss_pct": round(100 * -sum(min(0.0, float(t.net)) for t in worst) / gross_loss, 1)
            if gross_loss else None,
            "best_tokens": [{"symbol": t.symbol, "mint": t.mint, "trades": int(t.n), "net_sol": _f(t.net)} for t in best],
            "net_without_best5_tokens_sol": round(net - sum(max(0.0, float(t.net)) for t in best), 6),
            "loss_size_bands_pct": {
                "0..-10": sum(1 for x in pcts if x > -10), "-10..-20": sum(1 for x in pcts if -20 < x <= -10),
                "-20..-40": sum(1 for x in pcts if -40 < x <= -20), "below -40": sum(1 for x in pcts if x <= -40)},
            "median_loss_pct": round(statistics.median(pcts), 1) if pcts else None,
        }
    return out


async def build(s, sections: tuple[str, ...], engines: tuple[str, ...], since: datetime | None,
                until: datetime | None) -> dict[str, Any]:
    p = {"engines": list(engines), "since": since, "until": until}
    out: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "engines": list(engines),
                           "since": _iso(since), "until": _iso(until), "timezone": "UTC"}
    if "range" in sections:
        out["range"] = await table_range(s)
    if "positions" in sections:
        out["status"] = [{"engine": r.engine, "mode": r.mode, "status": r.status, "count": int(r.n),
                          "first_entry": _iso(r.first_entry), "last_entry": _iso(r.last_entry)}
                         for r in (await s.execute(text(STATUS_SQL), p)).all()]
        lo, hi = since or EPOCH, until or datetime.now(timezone.utc)
        out["metrics"] = {}
        for e in engines:
            rep = await sp.report(s, since=lo, until=hi, engine=e, outcomes=False)
            out["metrics"][e] = {m: {k: v[k] for k in ("overall", "by_exit_reason", "by_age", "by_stage", "by_entry_quality",
                                                       "by_regime", "by_hold") if k in v}
                                 for m, v in rep["trades_by_mode"].items()}
    for kind in ("daily", "hourly", "weekly", "versions"):
        if kind in sections:
            out[kind] = await buckets(s, kind, p)
    if "concentration" in sections:
        out["concentration"] = concentration((await s.execute(text(TOKENS_SQL), p)).all(),
                                             (await s.execute(text(TRADES_SQL), p)).all())
    if "ledger" in sections:
        out["ledger"] = [{"engine": r.engine, "stage": r.stage, "decision": r.decision, "traded": r.traded,
                          "count": int(r.n), "unresolved": int(r.unresolved), "measured": int(r.measured),
                          "positive": int(r.positive), "mean_exec_pct": _f(r.mean_exec, 2), "median_exec_pct": _f(r.med_exec, 2),
                          "min_exec_pct": _f(r.min_exec, 2), "max_exec_pct": _f(r.max_exec, 2),
                          "constant": int(r.measured) >= 20 and r.min_exec is not None
                          and float(r.max_exec - r.min_exec) < 0.01,
                          "first": _iso(r.first_at), "last": _iso(r.last_at)}
                         for r in (await s.execute(text(LEDGER_SQL), p)).all()]
    return out


def _v(v: Any, d: int = 4) -> str:
    return "-" if v is None else f"{v:+.{d}f}" if isinstance(v, float) else str(v)


def _metrics_line(o: dict) -> str:
    return (f"closed {o['trades']}  W/L/BE {o['wins']}/{o['losses']}/{o['breakeven']}  win {o.get('win_rate_pct') if o.get('win_rate_pct') is not None else '-'}%  "
            f"net {_v(o.get('net_pnl_sol'), 5)}  gross {_v(o.get('gross_pnl_sol'), 5)}  fees {_v(o.get('fees_sol'), 5)}  "
            f"PF {o.get('profit_factor') if o.get('profit_factor') is not None else '-'}  exp/trade {_v(o.get('expectancy_sol'), 5)}  "
            f"DD {_v(o.get('max_drawdown_sol'), 5)}  return on capital {_v(o.get('return_on_starting_capital_pct'), 2)}%\n"
            f"        avg/median win {_v(o.get('avg_win_sol'), 5)} / {_v(o.get('median_win_sol'), 5)} "
            f"({_v(o.get('avg_win_pct'), 1)}% / {_v(o.get('median_win_pct'), 1)}%)  avg/median loss {_v(o.get('avg_loss_sol'), 5)} / "
            f"{_v(o.get('median_loss_sol'), 5)} ({_v(o.get('avg_loss_pct'), 1)}% / {_v(o.get('median_loss_pct'), 1)}%)  "
            f"largest win {_v(o.get('largest_win_sol'), 5)}  largest loss {_v(o.get('largest_loss_sol'), 5)}  "
            f"median hold {o.get('median_hold_s')} s")


def render(r: dict[str, Any]) -> str:
    out = [f"HISTORY AUDIT at {r['at'][:16]} UTC; strategies {', '.join(r['engines'])}; window "
           f"{(r['since'] or 'first record')[:16]} .. {(r['until'] or 'now')[:16]}; all times UTC. Read-only."]
    if "range" in r:
        out.append("\nDATA RANGE (earliest .. latest, approximate rows)")
        for t in r["range"]:
            if "error" in t:
                out.append(f"  {t['table']:<22} unreadable ({t['error']})")
            elif "earliest" in t:
                out.append(f"  {t['table']:<22} {(t['earliest'] or '-')[:16]} .. {(t['latest'] or '-')[:16]}  ~{t['approx_rows']}  {t['what']}")
            else:
                out.append(f"  {t['table']:<22} {t['what']}")
    if "status" in r:
        out.append("\nPOSITIONS BY STATUS (entry time)")
        for x in r["status"]:
            out.append(f"  {x['engine']:<17} {x['mode']:<5} {x['status']:<13} {x['count']:<6} "
                       f"{(x['first_entry'] or '-')[:16]} .. {(x['last_entry'] or '-')[:16]}")
        out.append("\nCLOSED POSITIONS, FULL METRICS (each cost counted once; slippage and price impact are inside the fill "
                   "prices, so inside gross and net)")
        for e, modes in r["metrics"].items():
            for m, v in modes.items():
                o = v.get("overall")
                if not o:
                    continue
                out.append(f"  {e} {m}: " + _metrics_line(o))
                for dim in ("by_exit_reason", "by_age", "by_stage", "by_entry_quality", "by_regime", "by_hold"):
                    for k, g in (v.get(dim) or {}).items():
                        out.append(f"      {dim[3:]:<14} {k:<18} n {g['trades']:<4}{' (small)' if g['trades'] < 20 else '        '} "
                                   f"win {g.get('win_rate_pct') if g.get('win_rate_pct') is not None else '-'}%  net {_v(g.get('net_pnl_sol'), 5)}  "
                                   f"exp {_v(g.get('expectancy_sol'), 5)}  PF {g.get('profit_factor') if g.get('profit_factor') is not None else '-'}")
    for kind, title in (("daily", "NET PER UTC DAY (exit date)"), ("hourly", "NET PER UTC HOUR OF THE EXIT"),
                        ("weekly", "NET PER ISO WEEK (week starting)"), ("versions", "NET PER STRATEGY | MODEL VERSION")):
        if kind in r:
            out.append(f"\n{title}")
            for x in r[kind]:
                out.append(f"  {x['engine']:<17} {x['mode']:<5} {x['key']:<36} n {x['trades']:<4} wins {x['wins']:<4} "
                           f"net {_v(x['net_sol'], 5)}  fees {_v(x['fees_sol'], 5)}")
    if "concentration" in r:
        out.append("\nCONCENTRATION: a few tokens or many small losses?")
        for k, c in r["concentration"].items():
            out.append(f"  {k}: {c['trades']} trades on {c['tokens']} tokens, net {_v(c['net_sol'], 5)}, gross loss "
                       f"{_v(c['gross_loss_sol'], 5)} from {c['losses']} losses; {c['losses_making_80pct_of_gross_loss']} of them make "
                       f"80 % of it; the 5 worst tokens are {c['worst5_share_of_gross_loss_pct']}% of it; net without the 5 best tokens "
                       f"{_v(c['net_without_best5_tokens_sol'], 5)}")
            out.append(f"      loss sizes (% of the trade): {c['loss_size_bands_pct']}  median loss {c['median_loss_pct']}%")
            out.append("      worst: " + "; ".join(f"{t['symbol']} {t['mint'][:12]}.. x{t['trades']} {_v(t['net_sol'], 5)}" for t in c["worst_tokens"]))
            out.append("      best:  " + "; ".join(f"{t['symbol']} {t['mint'][:12]}.. x{t['trades']} {_v(t['net_sol'], 5)}" for t in c["best_tokens"]))
    if "ledger" in r:
        out.append("\nLEDGER DECISIONS (every decision, traded or not; executable return = 0.05 SOL reference trade at the "
                   "primary horizon after fees, impact, latency, fixed costs; migrated tokens are not simulated)")
        for x in r["ledger"]:
            out.append(f"  {x['engine']:<17} {x['stage']:<11} {x['decision']:<12} {'traded' if x['traded'] else 'not traded':<10} "
                       f"n {x['count']:<7} unresolved {x['unresolved']:<5} measured {x['measured']:<7} positive {x['positive']:<6} "
                       f"mean {_v(x['mean_exec_pct'], 2)}% median {_v(x['median_exec_pct'], 2)}%  "
                       f"{(x['first'] or '-')[:10]} .. {(x['last'] or '-')[:10]}")
            if x.get("constant"):
                out.append("      ^ every measured row has the same value: costs only, no price move was measured "
                           "(not a result of the decisions)")
    out.append("\nA group under 20 trades is (small): variation, not an edge. PAPER is not evidence of LIVE results. "
               "Nothing was written.")
    return "\n".join(out)


def _date(v: str | None) -> datetime | None:
    if not v:
        return None
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="full-history audit, read-only")
    ap.add_argument("--section", action="append", choices=SECTIONS, help="repeatable; default all")
    ap.add_argument("--engine", choices=ENGINES)
    ap.add_argument("--since", help="ISO date or time, UTC (default: the first record)")
    ap.add_argument("--until", help="ISO date or time, UTC, exclusive (default: now)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            await s.execute(text("SET LOCAL statement_timeout = '120s'"))
            r = await build(s, tuple(args.section or SECTIONS), (args.engine,) if args.engine else ENGINES,
                            _date(args.since), _date(args.until))
    finally:
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
