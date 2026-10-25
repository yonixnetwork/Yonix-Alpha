"""Day comparison (read-only): why was one day profitable and the next not?
Each day's Solana trades one by one with what the decision saw, the day's
results, the decision funnel, the market, data quality, and every recorded
change (deployments, settings, strategy modes, models, automatic demotions)
around the days. Prints no secrets; nothing is written.

    $C exec -T api python -m yonixalpha_core.tools.day_compare [--date 2026-10-23 --date 2026-10-24 ...]
        [--tz UTC] [--engine solana_momentum] [--trades 60] [--json]

Default: the last 3 calendar days in --tz (today included, so it is partial).
Days run from 00:00 to 24:00 in --tz; the UTC boundaries are printed. The
other reports (performance_report, regression_report) use UTC days.

Per day:
  RESULTS    per strategy and mode: closed trades, net / gross / fees, wins and
             losses, average win and loss, the best trade's share of the net and
             the net without it, and the exits by reason
  FUNNEL     ledger decisions by outcome, traded or not, the most common refusals
  TRADES     every closed trade: token, mint, stage, entry / exit time and price,
             size, net, exit reason, hold, the decision-to-entry delay, the price
             change in the 1 and 5 minutes before the decision, liquidity, buy
             volume, buyers / sellers, top-10 and top-3 buyer shares, warning signs
             at entry, slippage estimate, highest / lowest price after entry,
             planned stop, model version, large buys seen before the decision
  MARKET     SOL/USD and stream activity (market_samples, recorded from
             2026-10-25), launches per hour seen by the decisions
  DATA       ledger rows with data errors, refusals for missing or stale data,
             missing slots or curve addresses; service warnings and errors
CHANGES      everything recorded from 24 h before the first day to the end of the
             last: audit log (operator and automatic), risk-settings versions,
             strategy config updates, model versions, platform settings, deployments
SIDE BY SIDE the main numbers of each day in one table

A value that was not recorded is "-". Small samples are marked: a day with
fewer than 20 trades shows variation, not an edge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from yonixalpha_core import forward_test, market_samples, solana_performance as sp
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.strategies import momentum_v2

ENGINES = forward_test.ENGINES
SMALL = 20

TRADES_SQL = """
SELECT pp.id, coalesce(pp.execution_mode, 'PAPER') AS mode, pp.engine, pp.strategy, pp.model_version, pp.symbol,
       pp.asset_id AS mint, pp.exit_reason, pp.entry_at, pp.exit_at, pp.entry_price, pp.exit_price,
       pp.highest_price, pp.lowest_price, pp.entry_cost_quote AS cost, pp.fees_paid_quote AS fees,
       pp.realized_pnl AS pnl, pp.realized_pnl_pct AS pnl_pct,
       pp.plan->>'stop_distance_pct' AS stop_pct, pp.plan->'position_size'->>'value' AS size,
       pp.plan->>'entry_price' AS decision_price, pp.plan->'venue'->>'kind' AS venue,
       pp.plan->>'lifecycle' AS lifecycle, pp.plan->'entry_delay' AS fill_delay, pp.plan->'exit_steps' AS exit_steps,
       o.decided_at, o.snapshot, o.regime
FROM paper_positions pp
LEFT JOIN LATERAL (SELECT decided_at, snapshot, regime FROM opportunity_outcomes oo
                   WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
WHERE pp.engine = ANY(:engines) AND pp.status = 'closed' AND pp.exit_at >= :since AND pp.exit_at < :until
ORDER BY pp.exit_at LIMIT :limit
"""

# every closed trade's net, for the best-trade share (independent of how many trades are listed)
NETS_SQL = """
SELECT pp.engine, coalesce(pp.execution_mode, 'PAPER') AS mode, pp.realized_pnl AS pnl
FROM paper_positions pp
WHERE pp.engine = ANY(:engines) AND pp.status = 'closed' AND pp.exit_at >= :since AND pp.exit_at < :until
"""

FUNNEL_SQL = """
SELECT engine, decision, traded, count(*) AS n
FROM opportunity_outcomes WHERE decided_at >= :since AND decided_at < :until AND engine = ANY(:engines)
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC
"""

QUALITY_SQL = f"""
SELECT engine, count(*) AS n,
       count(*) FILTER (WHERE jsonb_typeof(snapshot->'data_errors') = 'array'
                        AND jsonb_array_length(snapshot->'data_errors') > 0) AS data_errors,
       count(*) FILTER (WHERE reasons::text ILIKE '%unavailable%' OR reasons::text ILIKE '%stale%'
                        OR reasons::text ILIKE '%unknown%') AS missing_or_stale,
       count(*) FILTER (WHERE reasons::text ILIKE '%slot%') AS no_slot,
       count(*) FILTER (WHERE reasons::text ILIKE '%curve address%') AS no_curve_address,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY CASE WHEN regime->>'launches_last_hour_seen' ~ '{sp.NUM}'
                                          THEN (regime->>'launches_last_hour_seen')::numeric END) AS launches,
       count(*) FILTER (WHERE regime->>'sol_usd' IS NOT NULL) AS with_sol_usd
FROM opportunity_outcomes WHERE decided_at >= :since AND decided_at < :until AND engine = ANY(:engines)
GROUP BY 1 ORDER BY 1
"""

EVENTS_SQL = """
SELECT service, severity, event_type, count(*) AS n
FROM system_events WHERE created_at >= :since AND created_at < :until AND severity <> 'info'
GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 12
"""

LARGE_SQL = """
SELECT mint, at, sol FROM large_trade_events
WHERE kind = 'EVENT' AND mint = ANY(:mints) AND at >= :since AND at < :until
"""

CHANGE_QUERIES = {
    "audit log": """SELECT created_at AS at, event_type AS what, left(coalesce(detail::text, ''), 220) AS detail
                    FROM audit_logs WHERE created_at >= :since AND created_at < :until
                      AND event_type NOT LIKE 'auth%' AND event_type NOT LIKE 'login%'
                    ORDER BY created_at LIMIT 200""",
    "risk settings": """SELECT created_at AS at, scope || ' v' || version AS what, coalesce(note, '') AS detail
                        FROM risk_settings WHERE created_at >= :since AND created_at < :until ORDER BY created_at""",
    "strategy configs (current row, last update)": """SELECT updated_at AS at, strategy || ' ' || mode AS what,
                        left(config::text, 220) AS detail FROM strategy_configs
                        WHERE updated_at >= :since AND updated_at < :until ORDER BY updated_at""",
    "model versions": """SELECT created_at AS at, name || ' v' || version || ' ' || status AS what,
                         coalesce('activated ' || activated_at::text, '') AS detail FROM model_versions
                         WHERE created_at >= :since AND created_at < :until
                            OR activated_at >= :since AND activated_at < :until ORDER BY created_at""",
    "platform settings (current row, last update)": """SELECT updated_at AS at, key AS what, '' AS detail
                        FROM platform_settings WHERE updated_at >= :since AND updated_at < :until ORDER BY updated_at""",
}


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(v: Any, d: int = 4) -> float | None:
    v = _num(v)
    return None if v is None else round(v, d)


def _med(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def day_window(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def stage_of(r: Any, snap: dict) -> str:
    if r.engine == "solana_momentum":
        return "MOMENTUM"
    if snap.get("migrated") is True or r.lifecycle == "MIGRATED" or r.engine == "solana_migration":
        return "MIGRATED"
    cp = _num(snap.get("curve_progress"))
    return "NEAR_MIGRATION" if cp is not None and cp >= float(sp.NEAR_MIGRATION_PROGRESS) else "FRESH"


def trade_row(r: Any, large: dict[str, list]) -> dict[str, Any]:
    snap = r.snapshot or {}
    f = momentum_v2.features_from_intel(snap.get("intel"), snap)
    entry, dec = _num(r.entry_price), _num(r.decision_price)
    hi, lo = _num(r.highest_price), _num(r.lowest_price)
    lb = [x for x in large.get(r.mint, []) if r.decided_at and r.decided_at - timedelta(minutes=5) <= x[0] <= r.decided_at]
    return {
        "mode": r.mode, "engine": r.engine, "stage": stage_of(r, snap), "symbol": r.symbol, "mint": r.mint,
        "strategy": r.strategy, "model_version": r.model_version,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "entry_at": r.entry_at.isoformat(), "exit_at": r.exit_at.isoformat(),
        # entry_at is the order time; the simulated fill lands later (paper_execution.settle_entry_delay)
        "entry_delay_s": round((r.entry_at - r.decided_at).total_seconds(), 1) if r.decided_at else None,
        "fill_latency_s": _r((r.fill_delay or {}).get("latency_s"), 1),
        "fill_move_pct": _r((r.fill_delay or {}).get("price_change_pct") or (r.fill_delay or {}).get("charged_live_drift_pct"), 2),
        "fill_measured": (r.fill_delay or {}).get("measured"),
        "exit_path": [x.get("reason") for x in (r.exit_steps or [])] or None,
        "hold_s": round((r.exit_at - r.entry_at).total_seconds(), 1),
        "entry_price": _num(r.entry_price), "exit_price": _num(r.exit_price), "size_sol": _r(r.size, 6),
        "cost_sol": _r(r.cost, 6), "fees_sol": _r(r.fees, 6), "net_sol": _r(r.pnl, 6), "net_pct": sp._pct(r.pnl_pct),
        "exit_reason": r.exit_reason, "planned_stop_pct": _r(r.stop_pct, 4),
        "slippage_estimate_pct": round((entry / dec - 1) * 100, 2) if entry and dec else None,
        "max_up_pct": round((hi / entry - 1) * 100, 1) if hi and entry else None,
        "max_down_pct": round((lo / entry - 1) * 100, 1) if lo and entry else None,
        "age_s": snap.get("age_seconds"), "liquidity_sol": _r(snap.get("liquidity_sol"), 3),
        "buy_volume_sol": _r(snap.get("buy_volume_sol"), 3), "sell_volume_sol": _r(snap.get("sell_volume_sol"), 3),
        "buyers": snap.get("buyers"), "sellers": snap.get("sellers"), "buy_sell_ratio": _r(snap.get("buy_sell_ratio"), 2),
        "top10_share": _r(snap.get("top10_share"), 3), "top3_buy_share": _r(f.get("top3_buy_share"), 3),
        "return_1m_before_pct": round(f["return_1m"] * 100, 1) if f.get("return_1m") is not None else None,
        "return_5m_before_pct": round(f["return_5m"] * 100, 1) if f.get("return_5m") is not None else None,
        "qualified_wallets_buying": f.get("qualified_wallets_buying"),
        "warning_signs": snap.get("deterioration_indicators"), "ml_score": snap.get("ml_score"),
        "launches_last_hour": (r.regime or {}).get("launches_last_hour_seen"), "sol_usd": (r.regime or {}).get("sol_usd"),
        "large_buys_5m_before": len(lb) if r.mint in large else None,
        "no_ledger_record": r.decided_at is None,
    }


def concentration(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {}
    best = max(nets)
    total = sum(nets)
    wins = sum(n for n in nets if n > 0)
    return {"net": round(total, 6), "best_trade": round(best, 6), "net_without_best": round(total - best, 6),
            "best_share_of_wins_pct": round(100 * best / wins, 1) if wins > 0 else None,
            "winning_trades": sum(1 for n in nets if n > 0)}


async def day(session, d: date, tz: ZoneInfo, engines: tuple[str, ...], limit: int) -> dict[str, Any]:
    since, until = day_window(d, tz)
    p = {"since": since, "until": until, "engines": list(engines)}
    rows = (await session.execute(text(TRADES_SQL), {**p, "limit": limit})).all()
    mints = sorted({r.mint for r in rows if r.decided_at})
    large: dict[str, list] = {}
    if mints:
        for m in mints:
            large[m] = []
        for x in (await session.execute(text(LARGE_SQL), {"mints": mints, "since": since - timedelta(minutes=10),
                                                           "until": until})).all():
            large[x.mint].append((x.at, x.sol))
        has_large_table_data = (await session.execute(text(
            "SELECT EXISTS (SELECT 1 FROM large_trade_events WHERE at >= :since AND at < :until)"), p)).scalar()
        if not has_large_table_data:
            large = {}  # not recorded that day (before 2026-10-24 21:27 UTC): unknown, not zero
    trades = [trade_row(r, large) for r in rows]
    nets: dict[tuple[str, str], list[float]] = {}
    for r in (await session.execute(text(NETS_SQL), p)).all():
        if r.pnl is not None:
            nets.setdefault((r.engine, r.mode), []).append(float(r.pnl))
    results = {}
    for e in engines:
        rep = await sp.report(session, since=since, until=until, engine=e, outcomes=False)
        results[e] = {
            "modes": {m: {"overall": v.get("overall"), "by_exit_reason": v.get("by_exit_reason"),
                          "by_stage": v.get("by_stage"), "by_age": v.get("by_age"),
                          "by_entry_quality": v.get("by_entry_quality")}
                      for m, v in rep["trades_by_mode"].items()},
            "concentration": {m: concentration(nets[(e, m)]) for m in ("PAPER", "LIVE") if (e, m) in nets},
            "decisions_by_target": rep["decisions_by_target"], "entries_by_mode": rep["entries_by_mode"],
            "failed_entries": rep.get("failed_entries"), "coverage": rep["coverage"],
        }
    funnel: dict[str, list] = {}
    for r in (await session.execute(text(FUNNEL_SQL), p)).all():
        funnel.setdefault(r.engine, []).append({"decision": r.decision, "traded": r.traded, "n": int(r.n)})
    refusals = [g for g in await forward_test.signals(session, since, until, engines) if not g["traded"]]
    quality = {r.engine: {"decisions": int(r.n), "with_data_errors": int(r.data_errors),
                          "refused_missing_or_stale_data": int(r.missing_or_stale), "refused_no_slot": int(r.no_slot),
                          "refused_no_curve_address": int(r.no_curve_address),
                          "median_launches_per_hour": _r(r.launches, 0), "with_sol_usd": int(r.with_sol_usd)}
               for r in (await session.execute(text(QUALITY_SQL), p)).all()}
    events = [{"service": r.service, "severity": r.severity, "event": r.event_type, "n": int(r.n)}
              for r in (await session.execute(text(EVENTS_SQL), p)).all()]
    entries = [t for t in trades if t["entry_delay_s"] is not None]
    return {
        "date": d.isoformat(), "since_utc": since.isoformat(), "until_utc": until.isoformat(),
        "partial": until > datetime.now(timezone.utc), "results": results, "trades": trades,
        "trades_truncated": len(rows) >= limit, "funnel": funnel,
        "top_refusals": sorted(refusals, key=lambda g: -g["count"])[:12], "data_quality": quality,
        "service_events": events, "market": await market_samples.window(session, since, until),
        "medians": {"entry_delay_s": _med([t["entry_delay_s"] for t in entries]),
                    "fill_latency_s": _med([t["fill_latency_s"] for t in trades]),
                    "fill_move_pct": _med([t["fill_move_pct"] for t in trades]),
                    "return_5m_before_pct": _med([t["return_5m_before_pct"] for t in trades]),
                    "liquidity_sol": _med([t["liquidity_sol"] for t in trades]),
                    "buy_sell_ratio": _med([t["buy_sell_ratio"] for t in trades]),
                    "slippage_estimate_pct": _med([t["slippage_estimate_pct"] for t in trades]),
                    "age_s": _med([_num(t["age_s"]) for t in trades])},
    }


async def changes(session, since: datetime, until: datetime) -> dict[str, list]:
    out: dict[str, list] = {}
    for name, sql in CHANGE_QUERIES.items():
        out[name] = [{"at": r.at.isoformat(), "what": r.what, "detail": r.detail}
                     for r in (await session.execute(text(sql), {"since": since, "until": until})).all()]
    dep = (await session.execute(text("SELECT value FROM platform_settings WHERE key = :k"),
                                 {"k": forward_test.DEPLOY_KEY})).scalar_one_or_none() or {}
    hist = [dep.get("current")] + list(dep.get("history") or []) if dep.get("current") else list(dep.get("history") or [])
    out["deployments recorded by deploy.sh (from 2026-10-24)"] = [
        {"at": h.get("at"), "what": h.get("revision"), "detail": ""} for h in hist if h and since.isoformat() <= (h.get("at") or "")]
    modes = (await session.execute(text("SELECT value FROM platform_settings WHERE key = 'entry_intelligence'"))).scalar_one_or_none()
    out["entry strategy modes now"] = [{"at": "", "what": k, "detail": v}
                                       for k, v in sorted(((modes or {}).get("modes") or {}).items())]
    return out


async def build(session, dates: list[date], tz: ZoneInfo, engines: tuple[str, ...], limit: int) -> dict[str, Any]:
    days = [await day(session, d, tz, engines, limit) for d in dates]
    first, _ = day_window(min(dates), tz)
    _, last = day_window(max(dates), tz)
    return {"tz": str(tz), "engines": list(engines), "days": days,
            "changes": await changes(session, first - timedelta(hours=24), last),
            "changes_since": (first - timedelta(hours=24)).isoformat()}


def _v(v: Any, d: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:+.{d}f}"
    return str(v)


def _t(iso: str | None) -> str:
    return iso[5:19].replace("T", " ") if iso else "-"


def render(r: dict[str, Any]) -> str:
    out = [f"DAY COMPARISON, days in {r['tz']}, strategies {', '.join(r['engines'])}. Read-only; nothing was written."]
    for d in r["days"]:
        out.append(f"\n{'=' * 100}\n{d['date']} ({r['tz']})  UTC {d['since_utc'][:16]} .. {d['until_utc'][:16]}"
                   + ("  PARTIAL DAY (still running)" if d["partial"] else ""))
        out.append("RESULTS (closed trades by exit time)")
        for e, x in d["results"].items():
            for mode, m in x["modes"].items():
                o = m["overall"] or {}
                n = o.get("trades") or 0
                c = x["concentration"].get(mode) or {}
                out.append(f"  {e:<17} {mode:<5} closed {n}{' (small)' if n < SMALL else ''}  W/L {o.get('wins')}/{o.get('losses')}"
                           f"  net {_v(o.get('net_pnl_sol'), 5)}  gross {_v(o.get('gross_pnl_sol'), 5)}  fees {_v(o.get('fees_sol'), 5)}"
                           f"  avg win {_v(o.get('avg_win_pct'), 1)}%  avg loss {_v(o.get('avg_loss_pct'), 1)}%"
                           f"  best {_v(c.get('best_trade'), 5)} ({_v(c.get('best_share_of_wins_pct'), 1)}% of wins)"
                           f"  net without best {_v(c.get('net_without_best'), 5)}")
                for k, g in (m["by_exit_reason"] or {}).items():
                    out.append(f"      exit {k:<20} n {g['trades']:<3} net {_v(g['net_pnl_sol'], 5)}  avg {_v(g['avg_win_pct'] if g['net_pnl_sol'] and g['net_pnl_sol'] > 0 else g['avg_loss_pct'], 1)}%")
            if not x["modes"]:
                out.append(f"  {e:<17} no closed trade")
            ent = ", ".join(f"{k} entries {v['entries']} (still open {v['still_open']})" for k, v in x["entries_by_mode"].items())
            dec = ", ".join(f"{k} signals {v['signals']} qualified {v['qualified']}" for k, v in x["decisions_by_target"].items())
            out.append(f"      decisions: {dec or 'none'};  {ent or 'no entry'}"
                       + (f";  never filled {x['failed_entries']}" if x.get("failed_entries") else ""))
        out.append("FUNNEL (ledger decisions)")
        for e, rows in d["funnel"].items():
            out.append(f"  {e:<17} " + "  ".join(f"{g['decision']}{'/traded' if g['traded'] else ''} {g['n']}" for g in rows[:8]))
        for g in d["top_refusals"][:8]:
            out.append(f"    refused {g['engine']:<17} {g['decision']:<10} n {g['count']:<5} {g['reason'][:70]}")
        out.append(f"TRADES ({len(d['trades'])}{', list limited by --trades' if d['trades_truncated'] else ''}); prices in SOL per token")
        for t in d["trades"]:
            out.append(f"  {t['mode']:<5} {t['stage']:<14} {str(t['symbol'] or '?')[:10]:<10} {t['mint']}  {t['strategy'] or '-'} model {t['model_version'] or '-'}")
            out.append(f"      decided {_t(t['decided_at'])}  order {_t(t['entry_at'])}  simulated fill after {t['fill_latency_s'] if t['fill_latency_s'] is not None else '-'} s, "
                       f"price moved {_v(t['fill_move_pct'], 2)}%{'' if t['fill_measured'] in (None, True) else ' (not measured)'} "
                       f"-> entry @ {t['entry_price']}"
                       f"  exit {_t(t['exit_at'])} @ {t['exit_price']}  hold {t['hold_s']} s  {t['exit_reason']}"
                       + (f" (exit path: {' > '.join(t['exit_path'])})" if t['exit_path'] else " (exit path not recorded before 2026-10-25)"))
            out.append(f"      size {_v(t['size_sol'], 4)}  fees {_v(t['fees_sol'], 5)}  NET {_v(t['net_sol'], 5)} ({_v(t['net_pct'], 1)}%)"
                       f"  planned stop {_v(t['planned_stop_pct'], 3)}  max up {_v(t['max_up_pct'], 1)}%  max down {_v(t['max_down_pct'], 1)}%"
                       f"  slippage est {_v(t['slippage_estimate_pct'], 2)}%")
            out.append(f"      at decision: age {t['age_s'] if t['age_s'] is not None else '-'} s  liq {_v(t['liquidity_sol'], 2)}"
                       f"  buy vol {_v(t['buy_volume_sol'], 2)}  sell vol {_v(t['sell_volume_sol'], 2)}  buyers/sellers {t['buyers']}/{t['sellers']}"
                       f"  top10 {_v(t['top10_share'], 3)}  top3 buy share {_v(t['top3_buy_share'], 3)}"
                       f"  1m {_v(t['return_1m_before_pct'], 1)}%  5m {_v(t['return_5m_before_pct'], 1)}%"
                       f"  warnings {t['warning_signs'] if t['warning_signs'] is not None else '-'}"
                       f"  large buys 5m before {t['large_buys_5m_before'] if t['large_buys_5m_before'] is not None else '- (not recorded)'}"
                       + ("  NO LEDGER RECORD" if t["no_ledger_record"] else ""))
        mk = d["market"]
        out.append(f"MARKET  SOL/USD {_v(mk['sol_usd_first'], 2)} -> {_v(mk['sol_usd_last'], 2)} (low {_v(mk['sol_usd_low'], 2)} high "
                   f"{_v(mk['sol_usd_high'], 2)}, change {_v(mk['sol_usd_change_pct'], 2)}%)  launches/h {mk['median_launches_per_hour']}"
                   f"  migrations/h {mk['median_migrations_per_hour']}  active tokens 5m {mk['median_active_tokens_5m']}"
                   f"  samples {mk['samples']}/{mk['expected_samples']} (stream stale in {mk['stream_stale_samples']})"
                   + ("  [no samples: recorded from the 2026-10-25 deploy on]" if not mk["samples"] else ""))
        out.append("DATA QUALITY (ledger decisions)")
        for e, q in d["data_quality"].items():
            out.append(f"  {e:<17} decisions {q['decisions']}  with data errors {q['with_data_errors']}  refused for missing/stale/unknown "
                       f"data {q['refused_missing_or_stale_data']}  no slot {q['refused_no_slot']}  no curve address "
                       f"{q['refused_no_curve_address']}  median launches/h {q['median_launches_per_hour']}  with SOL price {q['with_sol_usd']}")
        out.append("  service warnings / errors: " + ("; ".join(f"{x['service']} {x['severity']} {x['event']} x{x['n']}"
                                                           for x in d["service_events"]) or "none kept (info / warning rows are pruned after the retention days)"))
    out.append(f"\n{'=' * 100}\nCHANGES recorded since {r['changes_since'][:16]} UTC")
    for name, rows in r["changes"].items():
        out.append(f"  {name}:" + ("" if rows else " none"))
        for x in rows:
            out.append(f"    {_t(x['at']) if x['at'] else '':<15} {x['what']}  {x['detail']}")
    out.append(f"\n{'=' * 100}\nSIDE BY SIDE (all selected strategies, PAPER)")
    keys = [("closed trades", lambda d: _sum(d, "trades")), ("net SOL", lambda d: _sum(d, "net_pnl_sol")),
            ("gross SOL", lambda d: _sum(d, "gross_pnl_sol")), ("fees SOL", lambda d: _sum(d, "fees_sol")),
            ("wins", lambda d: _sum(d, "wins")), ("losses", lambda d: _sum(d, "losses")),
            ("stop-loss exits", lambda d: _exit(d, "stop_loss", "trades")), ("stop-loss net", lambda d: _exit(d, "stop_loss", "net_pnl_sol")),
            ("take-profit exits", lambda d: sum(_exit(d, k, "trades") or 0 for k in ("take_profit_1", "take_profit_2", "take_profit_3"))),
            ("trailing-stop exits", lambda d: _exit(d, "trailing_stop", "trades")),
            ("best trade", lambda d: max((c.get("best_trade") for x in d["results"].values()
                                          for m, c in x["concentration"].items() if m == "PAPER" and c), default=None)),
            ("signals evaluated", lambda d: sum(v["signals"] for x in d["results"].values() for v in x["decisions_by_target"].values())),
            ("PAPER entries", lambda d: sum((x["entries_by_mode"].get("PAPER") or {}).get("entries", 0) for x in d["results"].values())),
            ("median fill latency s", lambda d: d["medians"]["fill_latency_s"]),
            ("median move during fill %", lambda d: d["medians"]["fill_move_pct"]),
            ("median 5m move before %", lambda d: d["medians"]["return_5m_before_pct"]),
            ("median liquidity SOL", lambda d: d["medians"]["liquidity_sol"]),
            ("median buy/sell ratio", lambda d: d["medians"]["buy_sell_ratio"]),
            ("median slippage est %", lambda d: d["medians"]["slippage_estimate_pct"]),
            ("median token age s", lambda d: d["medians"]["age_s"]),
            ("SOL/USD change %", lambda d: d["market"]["sol_usd_change_pct"]),
            ("median launches/h", lambda d: _med([q["median_launches_per_hour"] for q in d["data_quality"].values()]))]
    out.append(f"  {'':<26}" + "".join(f"{d['date'] + ('*' if d['partial'] else ''):>16}" for d in r["days"]))
    for label, fn in keys:
        out.append(f"  {label:<26}" + "".join(f"{_v(fn(d), 4) if isinstance(fn(d), float) else (fn(d) if fn(d) is not None else '-'):>16}"
                                              for d in r["days"]))
    out.append("  (* partial day.) A day with fewer than 20 trades shows variation, not an edge. Nothing was written.")
    return "\n".join(out)


def _sum(d: dict, k: str) -> float | int | None:
    vals = [((x["modes"].get("PAPER") or {}).get("overall") or {}).get(k) for x in d["results"].values()]
    vals = [v for v in vals if v is not None]
    return (round(sum(vals), 6) if any(isinstance(v, float) for v in vals) else sum(vals)) if vals else None


def _exit(d: dict, reason: str, k: str) -> float | int | None:
    vals = [(((x["modes"].get("PAPER") or {}).get("by_exit_reason") or {}).get(reason) or {}).get(k) for x in d["results"].values()]
    vals = [v for v in vals if v is not None]
    return (round(sum(vals), 6) if any(isinstance(v, float) for v in vals) else sum(vals)) if vals else None


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="day comparison, read-only")
    ap.add_argument("--date", action="append", help="YYYY-MM-DD, repeatable; default the last 3 days in --tz")
    ap.add_argument("--tz", default="UTC", help="IANA time zone for the day boundaries, e.g. UTC or Europe/London")
    ap.add_argument("--engine", choices=ENGINES, help="one strategy; default all Solana strategies")
    ap.add_argument("--trades", type=int, default=60, help="at most this many trades listed per day (0: none, the summary only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    tz = ZoneInfo(args.tz)
    today = datetime.now(tz).date()
    dates = sorted({date.fromisoformat(x) for x in args.date}) if args.date else [today - timedelta(days=i) for i in (2, 1, 0)]
    engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            r = await build(s, dates, tz, (args.engine,) if args.engine else ENGINES, max(0, args.trades))
    finally:
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
