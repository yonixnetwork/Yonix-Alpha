"""Deep strategy recovery evidence: read-only, prints no secrets.

    $C exec -T api python -m yonixalpha_core.tools.strategy_recovery [--section NAME ...] [--top 10] [--deep 12]

Everything runs in one READ ONLY transaction with a 60 s statement timeout:
the database cannot be changed by this tool. Bounded queries only (indexed
time windows, LIMITs); no full scans of the large event tables.

Sections (default: all)
  book        paper accounts; positions per mode / engine / status with first / last entry
  settings    every risk_settings version per scope (only the keys that changed), strategy
              modes and configs, paper-execution settings, operator audit log since Sep 25
  trades      every closed Solana position, PAPER and LIVE apart: per strategy, per token
              category at entry, per period between deploys, per day; migration with and
              without Oct 6 and without its best day; momentum per period
  top         best by return %, best and worst by SOL, best by peak; accounting checks
  deep        full reconstruction of the selected winners / losers: plan, candidate states,
              the decision and what it knew, timeline, outcome, other engines on the token
  compare     winners vs losers at entry (medians of what the gate saw), per category
  migration   migration pipeline, last 48 h per hour; last discovery / decision / approval /
              buy / exit; today's realized PnL per engine with the moment each limit was crossed;
              the blockers the risk engine applies right now
  parity      PAPER vs LIVE: same token both modes, LIVE positions not closed, LIVE orders,
              paper entry-delay measurement per engine

Token category at entry: plan.token_category when recorded (since PR #76); before
that MOMENTUM = momentum engine, MIGRATED = migrated at the decision / entered as
MIGRATED / migration engine, NEAR_MIGRATION = curve progress >= 0.70 at the
decision, FRESH otherwise (the same rule as regression_report). Migrated tokens are
split by pool age at the decision (snapshot age_seconds). Nothing is estimated: a
value that was not recorded prints "-".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana_performance import NUM

SECTIONS = ("book", "settings", "trades", "top", "deep", "compare", "migration", "parity")
SMALL = 20
MAX_ROWS = 20000
# Deploy times (UTC) taken from the operator's deploy output ("Updating a..b").
# Periods are "deployed at", not merge times. Extra markers: --split.
DEPLOYS = (
    ("2026-10-07T13:15:00+00:00", "#53 paper pays LIVE fixed costs"),
    ("2026-10-09T23:29:20+00:00", "risk save GLOBAL v11 (all engines)"),
    ("2026-10-10T16:44:00+00:00", "#69-#71 event re-checks, fast promotion"),
    ("2026-10-11T08:09:00+00:00", "#74 migration pool detection"),
)
SETTINGS_KEYS = ("trade_size_mode", "fixed_trade_size_sol", "max_loss_per_trade_sol", "fixed_size_policy",
                 "risk_per_trade_pct", "max_position_size_quote", "min_position_size_quote", "max_token_exposure_quote",
                 "max_daily_loss_quote", "max_open_positions", "cooldown_after_loss_seconds", "min_stop_pct",
                 "max_stop_pct", "max_fixed_cost_pct", "pool_fraction", "max_price_impact_bps", "min_liquidity_quote",
                 "max_data_age_seconds", "take_profit_levels", "trailing_stop_pct")
SNAP_FIELDS = ("market_cap_sol", "liquidity_sol", "age_seconds", "curve_progress", "buyers", "sellers", "buy_sell_ratio",
               "buy_volume_sol", "sell_volume_sol", "trades", "top10_share", "creator_share", "volatility",
               "signal_strength", "price_age_seconds", "decision_eval_ms")
AUDIT_PATTERNS = ("risk_settings.%", "strategy_mode%", "strategy_config%", "trading_control%", "kill_switch%",
                  "paper_execution%", "paper_account%", "live_execution%", "system_profile%", "retention%",
                  "exit_protection%", "entry_intelligence%", "strategy_routing%")
PLATFORM_KEYS = ("paper_execution", "data_retention", "exit_protection_settings", "entry_intelligence", "system_profile",
                 "global_mode", "strategy_routing")


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001 - a non-numeric stored value is shown as missing
        return None


def _n(v: Any, digits: int = 4) -> str:
    x = _d(v)
    return "-" if x is None else f"{x:+.{digits}f}"


def _u(v: Any, digits: int = 4) -> str:
    x = _d(v)
    return "-" if x is None else f"{x:.{digits}f}"


def _p(v: Any, digits: int = 1) -> str:
    x = _d(v)
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def _t(v: datetime | None) -> str:
    return "-" if v is None else v.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S")


def _j(v: Any, limit: int = 400) -> str:
    s = json.dumps(v, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _hold(a: datetime | None, b: datetime | None) -> str:
    if a is None or b is None:
        return "-"
    s = (b - a).total_seconds()
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m" if s < 7200 else f"{s / 3600:.1f}h"


# --- trade rows ---------------------------------------------------------------------

TRADES_SQL = f"""
SELECT pp.id, coalesce(pp.execution_mode, 'PAPER') AS mode, coalesce(pp.engine, '?') AS engine,
       coalesce(pp.strategy, '?') AS strategy, pp.symbol, pp.asset_id, pp.lifecycle, pp.source, pp.execution_route,
       pp.exit_reason, pp.realized_pnl AS pnl, pp.realized_pnl_pct AS pnl_pct, pp.fees_paid_quote AS fees,
       pp.entry_cost_quote AS cost, pp.proceeds_quote AS proceeds, pp.entry_at, pp.exit_at, pp.created_at,
       pp.entry_price, pp.exit_price, pp.highest_price, pp.lowest_price, pp.quantity, pp.initial_quantity,
       pp.tp_hits, pp.max_loss_quote, pp.model_version, pp.feature_version, pp.account_id,
       pp.plan->>'token_category' AS plan_category, pp.plan->>'lifecycle' AS plan_lifecycle,
       pp.plan->>'fixed_cost_quote' AS fixed_cost, pp.plan->'venue'->>'live_drift_pct' AS live_drift,
       pp.plan->'entry_delay'->>'measured' AS delay_measured, pp.plan->'entry_delay'->>'price_change_pct' AS delay_move,
       pp.plan->'entry_delay'->>'latency_s' AS delay_s,
       pp.plan->'position_size'->>'method' AS size_method, pp.plan->>'binding_cap' AS binding_cap,
       pp.plan->>'stop_distance_pct' AS stop_pct,
       o.snapshot AS snap
FROM paper_positions pp
LEFT JOIN LATERAL (SELECT snapshot FROM opportunity_outcomes oo
                   WHERE oo.position_id = pp.id ORDER BY oo.decided_at LIMIT 1) o ON true
WHERE pp.engine LIKE 'solana%' AND pp.status = 'closed' AND pp.exit_at IS NOT NULL
ORDER BY pp.entry_at LIMIT {MAX_ROWS}
"""


def category(r: Any) -> str:
    """Token state at entry (see module doc)."""
    snap = r.snap or {}
    if r.plan_category:
        base = r.plan_category
    elif r.engine == "solana_momentum":
        base = "MOMENTUM"
    elif snap.get("migrated") is True or r.plan_lifecycle == "MIGRATED" or r.lifecycle == "MIGRATED" \
            or r.engine == "solana_migration":
        base = "MIGRATED"
    else:
        cp = _d(snap.get("curve_progress"))
        base = "NEAR_MIGRATION" if cp is not None and cp >= Decimal("0.70") else "FRESH"
    if base != "MIGRATED":
        return base
    age = _d(snap.get("age_seconds"))
    if age is None:
        return "MIGRATED (pool age unknown)"
    return ("MIGRATED pool <10m" if age < 600 else "MIGRATED pool 10-30m" if age < 1800
            else "MIGRATED pool 30m-24h" if age < 86400 else "MIGRATED pool >24h")


def metrics(rows: list[Any]) -> dict[str, Any]:
    pnls = [float(r.pnl or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(p for p in losses if p < 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:  # rows are in entry order
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pcts = [float(r.pnl_pct) for r in rows if r.pnl_pct is not None]
    sizes = [float(r.cost) for r in rows if r.cost]
    holds = [(r.exit_at - r.entry_at).total_seconds() for r in rows if r.exit_at and r.entry_at]
    moves = [float(r.delay_move) for r in rows if r.delay_move not in (None, "")]
    n = len(pnls)
    return {
        "n": n, "wins": len(wins), "win_rate": len(wins) / n if n else None,
        "net": sum(pnls), "avg": sum(pnls) / n if n else None, "median": statistics.median(pnls) if n else None,
        "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
        "median_pct": statistics.median(pcts) if pcts else None,
        "pf": (sum(wins) / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        "max_dd": dd, "fees": sum(float(r.fees or 0) for r in rows),
        "avg_size": sum(sizes) / len(sizes) if sizes else None,
        "median_hold_s": statistics.median(holds) if holds else None,
        "delay_measured": sum(1 for r in rows if r.delay_measured == "True" or r.delay_measured == "true"),
        "median_delay_move_pct": statistics.median(moves) if moves else None,
    }


def mline(key: str, m: dict[str, Any]) -> str:
    pf = m["pf"]
    pf_s = "-" if pf is None else ("inf" if pf == float("inf") else f"{pf:.2f}")
    hold = m["median_hold_s"]
    fee_share = (m["fees"] / (m["avg_size"] * m["n"])) if m["avg_size"] and m["n"] else None
    return (f"  {key:46s} n={m['n']:<4}{' (small)' if m['n'] < SMALL else '        '} win {_p(m['win_rate'], 0):>4} "
            f"net {m['net']:+.4f} avg {_n(m['avg'], 5)} med {_n(m['median'], 5)} avgW {_n(m['avg_win'], 5)} "
            f"avgL {_n(m['avg_loss'], 5)} PF {pf_s:>5} DD {m['max_dd']:.4f} fees {m['fees']:.4f} ({_p(fee_share)}) "
            f"size {_u(m['avg_size'])} hold {'-' if hold is None else f'{hold:.0f}s'}")


def period_of(t: datetime, marks: list[tuple[datetime, str]]) -> str:
    label = f"P0 before {marks[0][1]}"
    for i, (at, name) in enumerate(marks):
        if t >= at:
            label = f"P{i + 1} after {name}"
    return label


async def load_trades(s) -> list[Any]:
    return list((await s.execute(text(TRADES_SQL))).all())


def section_trades(rows: list[Any], marks: list[tuple[datetime, str]]) -> list[str]:
    out = ["\n== TRADES: every closed Solana position (realized PnL in SOL, net of every fee charged) =="]
    if not rows:
        return out + ["  none"]
    out.append(f"  rows {len(rows)}; first entry {_t(rows[0].entry_at)}, last entry {_t(rows[-1].entry_at)} UTC")
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups["strategy"][f"{r.mode} {r.engine} {r.strategy}"].append(r)
        groups["category"][f"{r.mode} {category(r)}"].append(r)
        groups["engine x category"][f"{r.mode} {r.engine} | {category(r)}"].append(r)
        groups["period"][f"{r.mode} {r.engine} | {period_of(r.entry_at, marks)}"].append(r)
        groups["day"][f"{r.mode} {r.engine} {r.entry_at.astimezone(timezone.utc):%m-%d}"].append(r)
        groups["exit reason"][f"{r.mode} {r.engine} {r.exit_reason}"].append(r)
        groups["model"][f"{r.mode} {r.engine} model={r.model_version or '-'} feat={r.feature_version or '-'}"].append(r)
        groups["size cap"][f"{r.mode} {r.engine} cap={r.binding_cap or '-'}"].append(r)
    for name in ("strategy", "category", "engine x category", "period", "day", "exit reason", "model", "size cap"):
        out.append(f"\n  -- by {name} --")
        out += [mline(k, metrics(v)) for k, v in sorted(groups[name].items())]
    out.append("\n  -- migration: with / without Oct 6, without its single best day (PAPER) --")
    mig = [r for r in rows if r.mode == "PAPER" and r.engine == "solana_migration"]
    if mig:
        by_day: dict[str, list] = defaultdict(list)
        for r in mig:
            by_day[f"{r.entry_at.astimezone(timezone.utc):%Y-%m-%d}"].append(r)
        best = max(by_day, key=lambda d: sum(float(x.pnl or 0) for x in by_day[d]))
        out.append(mline("all", metrics(mig)))
        out.append(mline("Oct 6 only", metrics(by_day.get("2026-10-06", []))) if by_day.get("2026-10-06") else "  Oct 6: none")
        out.append(mline("excluding Oct 6", metrics([r for r in mig if f"{r.entry_at.astimezone(timezone.utc):%Y-%m-%d}" != "2026-10-06"])))
        out.append(mline(f"excluding best day {best}", metrics([r for r in mig if f"{r.entry_at.astimezone(timezone.utc):%Y-%m-%d}" != best])))
        out.append(mline("before #53 (no LIVE fixed costs)", metrics([r for r in mig if r.entry_at < marks[0][0]])))
        out.append(mline("after #53", metrics([r for r in mig if r.entry_at >= marks[0][0]])))
        top3 = sorted(mig, key=lambda r: -float(r.pnl or 0))[:3]
        out.append(mline("excluding its 3 largest winners", metrics([r for r in mig if r not in top3])))
    out.append("\n  -- paper cost realism per period (share of PAPER trades charged LIVE fixed costs / LIVE drift / "
               "measured entry delay) --")
    per: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.mode == "PAPER":
            per[f"{r.engine} | {period_of(r.entry_at, marks)}"].append(r)
    for k, v in sorted(per.items()):
        n = len(v)
        fixed = sum(1 for r in v if _d(r.fixed_cost) and _d(r.fixed_cost) > 0)
        drift = sum(1 for r in v if _d(r.live_drift) and _d(r.live_drift) > 0)
        delay = [r for r in v if r.delay_s is not None]
        meas = sum(1 for r in delay if str(r.delay_measured).lower() == "true")
        moves = [float(r.delay_move) for r in delay if r.delay_move not in (None, "")]
        out.append(f"  {k:60s} n={n:<4} fixed costs {fixed}/{n} drift {drift}/{n} delay scheduled {len(delay)}/{n} "
                   f"measured {meas}/{len(delay) or 0} median delay move {'-' if not moves else f'{statistics.median(moves):+.2f}%'}")
    return out


def section_top(rows: list[Any], top: int) -> tuple[list[str], list[Any]]:
    out = ["\n== TOP: best and worst closed trades (return % = realized PnL / entry cost incl. buy fees) =="]
    if not rows:
        return out + ["  none"], []

    def line(r: Any) -> str:
        price_move = (r.exit_price / r.entry_price - 1) if r.exit_price and r.entry_price else None
        peak = (r.highest_price / r.entry_price - 1) if r.highest_price and r.entry_price else None
        tps = len(r.tp_hits or [])
        return (f"  {str(r.id)[:8]} {r.mode:5s} {r.engine:17s} {category(r):24s} {str(r.symbol)[:10]:10s} "
                f"{(r.asset_id or '-')[:44]:44s} in {_t(r.entry_at)} hold {_hold(r.entry_at, r.exit_at):>6} "
                f"cost {_u(r.cost)} back {_u(r.proceeds)} pnl {_n(r.pnl, 5)} ret {_p(r.pnl_pct)} "
                f"exitpx/entrypx {_p(price_move)} peak {_p(peak)} tp {tps} exit {r.exit_reason}")

    def ranked(key, reverse=True):
        return sorted([r for r in rows if key(r) is not None], key=key, reverse=reverse)[:top]

    by_pct = ranked(lambda r: float(r.pnl_pct) if r.pnl_pct is not None else None)
    by_sol = ranked(lambda r: float(r.pnl) if r.pnl is not None else None)
    worst = ranked(lambda r: float(r.pnl) if r.pnl is not None else None, reverse=False)
    by_peak = ranked(lambda r: float(r.highest_price / r.entry_price) if r.highest_price and r.entry_price else None)
    for title, lst in (("best by return %", by_pct), ("best by SOL", by_sol), ("worst by SOL", worst),
                       ("best by peak (highest marked price / entry price)", by_peak)):
        out.append(f"\n  -- {title} --")
        out += [line(r) for r in lst]
    big = [r for r in rows if r.pnl_pct is not None and r.pnl_pct >= 2]
    out.append(f"\n  trades with return >= +200%: {len(big)}; >= +500%: {sum(1 for r in big if r.pnl_pct >= 5)}; "
               f">= +900%: {sum(1 for r in big if r.pnl_pct >= 9)}")
    out.append("\n  -- accounting checks (no partial sale: return should track exit price / entry price minus costs) --")
    flagged = 0
    for r in rows:
        if not (r.exit_price and r.entry_price and r.pnl_pct is not None) or (r.tp_hits or []):
            continue
        move = float(r.exit_price / r.entry_price - 1)
        if abs(float(r.pnl_pct) - move) > 0.25:
            flagged += 1
            if flagged <= 15:
                out.append(f"  {str(r.id)[:8]} {r.mode} {r.engine} ret {_p(r.pnl_pct)} vs price move {move * 100:.1f}% "
                           f"(entry {r.entry_price}, exit {r.exit_price}, cost {_u(r.cost)}, back {_u(r.proceeds)})")
    out.append(f"  flagged: {flagged} (difference above 25 points; a stop that fills far below its trigger, a drift or "
               "fixed cost on a tiny size, or a record error)")
    deep: list[Any] = []
    for lst, k in ((by_pct, 6), (by_sol, 3), (worst, 3)):
        for r in lst[:k]:
            if r not in deep:
                deep.append(r)
    for r in big:
        if r not in deep and len(deep) < 14:
            deep.append(r)
    return out, deep


# --- deep reconstruction ----------------------------------------------------------------

def _plan_view(plan: dict) -> dict:
    keep = ("position_size", "max_loss", "stop_loss", "stop_distance_pct", "take_profits", "entry_cost_bps",
            "exit_cost_bps", "fixed_cost_quote", "fixed_cost_detail", "binding_cap", "lifecycle", "token_category",
            "planned_loss_at_stop", "fill", "entry_delay", "trailing_stop_pct")
    v = {k: plan.get(k) for k in keep if plan.get(k) is not None}
    venue = plan.get("venue") or {}
    v["venue"] = {k: venue.get(k) for k in ("route", "pool", "decimals", "live_drift_pct", "provider") if venue.get(k) is not None}
    return v


def _features(snapshot: dict) -> dict:
    f = snapshot.get("features") if isinstance(snapshot, dict) else None
    if not isinstance(f, dict):
        return {}
    return {k: v for k, v in f.items() if isinstance(v, (int, float, str, bool)) or v is None}


async def section_deep(s, picks: list[Any]) -> list[str]:
    out = ["\n== DEEP: reconstruction of selected trades (what was known at entry vs afterwards) =="]
    for r in picks:
        pos = (await s.execute(text("SELECT * FROM paper_positions WHERE id = :id"), {"id": r.id})).mappings().one()
        out.append(f"\n### {str(r.id)[:8]} {r.mode} {r.engine} {r.strategy} {r.symbol} mint {r.asset_id}")
        out.append(f"  category at entry: {category(r)}; lifecycle {pos['lifecycle']}; route {pos['execution_route']}; "
                   f"pool {pos['pool'] or '-'}; source {pos['source']}; provider {pos['execution_provider']}")
        out.append(f"  entry {_t(pos['entry_at'])} exit {_t(pos['exit_at'])} hold {_hold(pos['entry_at'], pos['exit_at'])}; "
                   f"exit reason {pos['exit_reason']}; tp hits {_j(pos['tp_hits'], 200)}")
        out.append(f"  qty bought {pos['initial_quantity'] or pos['quantity']} (remaining {pos['remaining_quantity']}); "
                   f"entry price {pos['entry_price']} exit price {pos['exit_price']} highest {pos['highest_price']} "
                   f"lowest {pos['lowest_price']}")
        out.append(f"  SOL in (entry cost incl. buy fees) {pos['entry_cost_quote']}; SOL back (all sales after fees) "
                   f"{pos['proceeds_quote']}; fees {pos['fees_paid_quote']}; realized {pos['realized_pnl']}; "
                   f"return {_p(pos['realized_pnl_pct'], 2)} = realized / entry cost; planned max loss {pos['max_loss_quote']}")
        out.append(f"  model {pos['model_version'] or '-'} feature version {pos['feature_version'] or '-'}")
        out.append(f"  plan: {_j(_plan_view(pos['plan'] or {}), 1500)}")
        if pos["candidate_id"]:
            c = (await s.execute(text("SELECT engine, state, created_at, state_history, detail FROM trading_candidates "
                                      "WHERE id = :id"), {"id": pos["candidate_id"]})).mappings().first()
            if c:
                hist = [f"{h.get('state')}@{str(h.get('at'))[11:19]} {str(h.get('reason') or '')[:70]}"
                        for h in (c["state_history"] or [])][:14]
                det = {k: v for k, v in (c["detail"] or {}).items() if k in (
                    "source", "strategy", "mint", "created_ts", "promoted_reason", "promotion", "pool", "migration",
                    "entry_blocked", "observation", "route", "lifecycle")}
                out.append(f"  candidate {str(pos['candidate_id'])[:8]} engine {c['engine']} created {_t(c['created_at'])} "
                           f"(discovery to entry {_hold(c['created_at'], pos['entry_at'])}) final state {c['state']}")
                out.append(f"  states: {' > '.join(hist)}")
                out.append(f"  candidate detail: {_j(det, 700)}")
        if pos["assessment_id"]:
            a = (await s.execute(text("SELECT evaluated_at, decision, status_label, strategy, risk_engine_version, assessment "
                                      "FROM risk_assessments WHERE id = :id"), {"id": pos["assessment_id"]})).mappings().first()
            if a:
                body = a["assessment"] or {}
                fnd = body.get("findings") or []
                hard = [f["code"] for f in fnd if f.get("hard_block")]
                soft = [f"{f['code']}:{f.get('action')}" for f in fnd if not f.get("hard_block")]
                snap = body.get("inputs_snapshot") or {}
                sset = body.get("settings_snapshot") or {}
                out.append(f"  decision {a['decision']} '{a['status_label']}' at {_t(a['evaluated_at'])} "
                           f"(decision to entry {_hold(a['evaluated_at'], pos['entry_at'])}); risk engine "
                           f"{a['risk_engine_version']}; versions {_j(body.get('versions'), 300)}")
                out.append(f"  reasons: {_j(body.get('reasons'), 600)}")
                out.append(f"  hard blocks {hard or 'none'}; other findings {soft[:20]}")
                out.append(f"  settings at decision: {_j({k: sset.get(k) for k in SETTINGS_KEYS if k in sset}, 700)}")
                out.append(f"  evidence keys: {sorted(snap.keys())}")
                out.append(f"  features at decision: {_j(_features(snap), 1800)}")
                for k in ("signal", "pool", "curve", "flow", "quote", "pool_age_source", "migration", "errors"):
                    if snap.get(k) is not None:
                        out.append(f"  {k}: {_j(snap.get(k), 500)}")
        o = (await s.execute(text("SELECT decided_at, snapshot, horizons, peak_pct, drawdown_pct, migrated_at, trade_result, "
                                  "loss_analysis, post_exit, executable_return_pct, theoretical_return_pct, analysis->'counterfactual' cf "
                                  "FROM opportunity_outcomes WHERE position_id = :id ORDER BY decided_at LIMIT 1"),
                             {"id": r.id})).mappings().first()
        if o:
            snap = o["snapshot"] or {}
            out.append(f"  gate snapshot: {_j({k: snap.get(k) for k in SNAP_FIELDS + ('migrated', 'signal_reasons', 'deterioration_indicators', 'entry_exit_check', 'overall_risk')}, 900)}")
            intel = snap.get("intel") or {}
            if intel:
                out.append(f"  launch intel at decision: {_j(intel, 900)}")
            out.append(f"  AFTER entry (not known at decision): peak {o['peak_pct']}% drawdown {o['drawdown_pct']}% "
                       f"migrated_at {_t(o['migrated_at'])} horizons {_j(o['horizons'], 400)}")
            out.append(f"  trade result {_j(o['trade_result'], 400)}; loss analysis {_j(o['loss_analysis'], 300)}; "
                       f"post exit {_j(o['post_exit'], 300)}; executable return {o['executable_return_pct']} "
                       f"theoretical {o['theoretical_return_pct']}")
        ev = (await s.execute(text("SELECT event_type, occurred_at, detail FROM trade_timeline_events WHERE position_id = :p "
                                   "OR (candidate_id = :c AND :c IS NOT NULL) ORDER BY occurred_at LIMIT 60"),
                              {"p": r.id, "c": pos["candidate_id"]})).all()
        out.append(f"  timeline ({len(ev)} events, offsets from entry):")
        for e in ev:
            off = (e.occurred_at - pos["entry_at"]).total_seconds()
            out.append(f"    {off:+9.1f}s {e.event_type:32s} {_j(e.detail, 220)}")
        others = (await s.execute(text("SELECT id, engine, execution_mode, entry_at, realized_pnl, realized_pnl_pct, status "
                                       "FROM paper_positions WHERE asset_id = :m AND id <> :id ORDER BY entry_at LIMIT 10"),
                                  {"m": r.asset_id, "id": r.id})).all()
        if others:
            out.append("  other positions on this token: " + "; ".join(
                f"{str(x.id)[:8]} {x.execution_mode} {x.engine} {_t(x.entry_at)} {x.status} pnl {_n(x.realized_pnl, 5)} ({_p(x.realized_pnl_pct)})"
                for x in others))
        decs = (await s.execute(text("SELECT engine, decision, count(*) n, min(evaluated_at) a, max(evaluated_at) b "
                                     "FROM risk_assessments WHERE asset_id = :m GROUP BY 1, 2 ORDER BY 1, 2"),
                                {"m": r.asset_id})).all()
        if decs:
            out.append("  decisions still stored for this token: " + "; ".join(
                f"{x.engine} {x.decision} x{x.n} {_t(x.a)}..{_t(x.b)}" for x in decs))
    return out


# --- winners vs losers ------------------------------------------------------------------

def section_compare(rows: list[Any]) -> list[str]:
    out = ["\n== COMPARE: what the gate saw at entry, winners vs losers (medians; PAPER and LIVE apart) =="]
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        if not r.snap:
            continue
        cls = "BIG WIN >=50%" if (r.pnl_pct or 0) >= Decimal("0.5") else "WIN" if (r.pnl or 0) > 0 else "LOSS"
        groups[f"{r.mode} {category(r).split(' pool')[0]} | {cls}"].append(r)
        groups[f"{r.mode} {r.engine} | {cls}"].append(r)
    hdr = "  " + f"{'group':44s} {'n':>4} " + " ".join(f"{f[:11]:>11}" for f in SNAP_FIELDS)
    out.append(hdr)
    for k, v in sorted(groups.items()):
        vals = []
        for f in SNAP_FIELDS:
            xs = [float(x) for x in ((r.snap or {}).get(f) for r in v) if _d(x) is not None]
            vals.append(f"{statistics.median(xs):>11.4g}" if xs else f"{'-':>11}")
        out.append(f"  {k:44s} {len(v):>4} " + " ".join(vals))
    out.append("  (age_seconds = token age; for migrated tokens the pool age. Only trades with a gate snapshot are counted.)")
    reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r.snap:
            cls = "WIN" if (r.pnl or 0) > 0 else "LOSS"
            for x in (r.snap.get("signal_reasons") or [])[:6]:
                reasons[f"{r.mode} {r.engine} {cls}"][str(x)[:60]] += 1
    out.append("\n  signal reasons at entry (top 6 per group):")
    for k, v in sorted(reasons.items()):
        top = sorted(v.items(), key=lambda kv: -kv[1])[:6]
        out.append(f"  {k:40s} " + "; ".join(f"{a} x{b}" for a, b in top))
    return out


# --- settings and operator history ------------------------------------------------------------

async def section_settings(s) -> list[str]:
    out = ["\n== SETTINGS: every risk_settings version (changed keys only), modes, audit log =="]
    rows = (await s.execute(text("SELECT scope, version, created_at, settings, note FROM risk_settings "
                                 "ORDER BY scope, version"))).all()
    last: dict[str, dict] = {}
    for r in rows:
        raw = r.settings or {}
        cur = raw.get("__overrides__") if isinstance(raw.get("__overrides__"), dict) else raw
        kind = "overrides" if "__overrides__" in raw else "full"
        prev = last.get(r.scope, {})
        changed = {k: cur.get(k) for k in sorted(set(cur) | set(prev)) if cur.get(k) != prev.get(k)
                   and (k in SETTINGS_KEYS or kind == "overrides")}
        last[r.scope] = cur
        out.append(f"  {r.scope:18s} v{r.version:<3} {_t(r.created_at)} {kind:9s} {_j(changed, 600)}"
                   + (f" note: {r.note[:80]}" if r.note else ""))
    out.append("\n  current values of the key settings per scope:")
    for scope, cur in sorted(last.items()):
        out.append(f"  {scope:18s} {_j({k: cur.get(k) for k in SETTINGS_KEYS if k in cur}, 700)}")
    sc = (await s.execute(text("SELECT strategy, mode, config, updated_at FROM strategy_configs ORDER BY strategy"))).all()
    out.append("\n  strategy modes:")
    out += [f"  {r.strategy:28s} {r.mode:7s} updated {_t(r.updated_at)} config {_j(r.config, 300)}" for r in sc] or ["  none"]
    ps = (await s.execute(text("SELECT key, value, updated_at FROM platform_settings WHERE key = ANY(:k) ORDER BY key"),
                          {"k": list(PLATFORM_KEYS)})).all()
    out.append("\n  platform settings (selected, non-secret):")
    out += [f"  {r.key:26s} updated {_t(r.updated_at)} {_j(r.value, 500)}" for r in ps] or ["  none"]
    cond = " OR ".join(f"event_type LIKE :p{i}" for i in range(len(AUDIT_PATTERNS)))
    params = {f"p{i}": p for i, p in enumerate(AUDIT_PATTERNS)}
    al = (await s.execute(text(f"SELECT created_at, event_type, detail FROM audit_logs WHERE created_at > '2026-09-25' "
                               f"AND ({cond}) ORDER BY created_at LIMIT 400"), params)).all()
    out.append(f"\n  operator audit log since Sep 25 ({len(al)} rows; details shortened, secrets are never logged there):")
    for r in al:
        d = r.detail or {}
        if isinstance(d, dict):
            d = {k: v for k, v in d.items() if "secret" not in k.lower() and "key" not in k.lower() and "token" not in k.lower()}
        out.append(f"  {_t(r.created_at)} {r.event_type:34s} {_j(d, 420)}")
    return out


# --- book ------------------------------------------------------------------------------

async def section_book(s) -> list[str]:
    out = ["\n== BOOK: paper accounts and positions =="]
    acc = (await s.execute(text("SELECT id, name, quote_currency, starting_balance, cash_balance, reset_at, created_at "
                                "FROM paper_accounts ORDER BY name"))).all()
    out += [f"  {r.name:18s} {r.quote_currency:5s} start {_u(r.starting_balance)} cash {_u(r.cash_balance)} "
            f"reset {_t(r.reset_at)} created {_t(r.created_at)}" for r in acc]
    pos = (await s.execute(text("SELECT coalesce(execution_mode,'PAPER') m, coalesce(engine,'?') e, status, count(*) n, "
                                "min(entry_at) a, max(entry_at) b, round(sum(coalesce(realized_pnl,0)),5) pnl "
                                "FROM paper_positions WHERE engine LIKE 'solana%' GROUP BY 1,2,3 ORDER BY 1,2,3"))).all()
    out.append("  Solana positions (mode engine status: count, first..last entry, realized SOL):")
    out += [f"  {r.m:5s} {r.e:18s} {r.status:13s} {r.n:>5}  {_t(r.a)} .. {_t(r.b)}  {r.pnl}" for r in pos]
    return out


# --- migration pipeline ---------------------------------------------------------------------

async def section_migration(s, now: datetime) -> list[str]:
    out = ["\n== MIGRATION / ENTRY PIPELINE: last 48 h per hour (UTC) =="]
    since = now - timedelta(hours=48)
    cand = {r.h: r for r in (await s.execute(text(
        "SELECT date_trunc('hour', created_at) h, count(*) n, "
        "count(*) FILTER (WHERE detail ? 'entry_blocked') blocked, "
        "count(*) FILTER (WHERE state IN ('entered','managing','closed')) traded "
        "FROM trading_candidates WHERE engine = 'migration' AND created_at >= :since GROUP BY 1"), {"since": since})).all()}
    ra = (await s.execute(text(
        "SELECT engine, date_trunc('hour', evaluated_at) h, count(*) n, count(*) FILTER (WHERE executable) ex, "
        "count(*) FILTER (WHERE decision = 'WAIT') wait FROM risk_assessments "
        "WHERE engine LIKE 'solana%' AND evaluated_at >= :since GROUP BY 1, 2"), {"since": since})).all()
    pos = (await s.execute(text(
        "SELECT engine, coalesce(execution_mode,'PAPER') m, date_trunc('hour', entry_at) h, count(*) n "
        "FROM paper_positions WHERE engine LIKE 'solana%' AND entry_at >= :since GROUP BY 1, 2, 3"), {"since": since})).all()
    by: dict[tuple, dict] = defaultdict(dict)
    for r in ra:
        by[(r.engine, r.h)].update(evals=r.n, ex=r.ex, wait=r.wait)
    for r in pos:
        by[(r.engine, r.h)][f"opened_{r.m}"] = by[(r.engine, r.h)].get(f"opened_{r.m}", 0) + r.n
    hours = sorted({h for _, h in by} | set(cand))
    out.append(f"  {'hour':11s} | migration candidates new/blocked/traded | per engine evals/executable/wait/opened (fresh, migration, momentum)")
    for h in hours:
        c = cand.get(h)
        cells = []
        for e in ("solana_fresh", "solana_migration", "solana_momentum"):
            x = by.get((e, h), {})
            cells.append(f"{x.get('evals', 0)}/{x.get('ex', 0)}/{x.get('wait', 0)}/{x.get('opened_PAPER', 0) + x.get('opened_LIVE', 0)}")
        out.append(f"  {h:%m-%d %H}h   | {c.n if c else 0:>4}/{c.blocked if c else 0:>4}/{c.traded if c else 0:>3} | " + "  ".join(f"{x:>18s}" for x in cells))
    codes = (await s.execute(text(
        "SELECT ra.engine, date_trunc('hour', ra.evaluated_at) h, f->>'code' code, count(*) n FROM risk_assessments ra, "
        "jsonb_array_elements(ra.assessment->'findings') f WHERE ra.engine = 'solana_migration' AND ra.evaluated_at >= :since "
        "AND (f->>'hard_block')::boolean GROUP BY 1, 2, 3"), {"since": now - timedelta(hours=30)})).all()
    per_h: dict[datetime, list] = defaultdict(list)
    for r in codes:
        per_h[r.h].append((r.n, r.code))
    out.append("\n  migration hard blocks per hour (top 4, last 30 h; stored decisions only - account blocks since PR #76 are "
               "not stored as decisions, see candidates 'blocked'):")
    for h in sorted(per_h):
        out.append(f"  {h:%m-%d %H}h  " + ", ".join(f"{c} {n}" for n, c in sorted(per_h[h], reverse=True)[:4]))
    lasts = [
        ("last migration candidate created", "SELECT max(created_at) FROM trading_candidates WHERE engine = 'migration'"),
        ("last migration decision stored", "SELECT max(evaluated_at) FROM risk_assessments WHERE engine = 'solana_migration'"),
        ("last migration decision EXECUTE (approved)", "SELECT max(evaluated_at) FROM risk_assessments WHERE engine = 'solana_migration' AND executable"),
        ("last migration buy order (any mode)", "SELECT max(o.created_at) FROM execution_orders o JOIN paper_positions p ON p.id = o.position_id "
                                                "WHERE p.engine = 'solana_migration' AND o.side = 'BUY'"),
        ("last migration position opened", "SELECT max(entry_at) FROM paper_positions WHERE engine = 'solana_migration'"),
        ("last migration position closed", "SELECT max(exit_at) FROM paper_positions WHERE engine = 'solana_migration' AND status = 'closed'"),
        ("last fresh position opened", "SELECT max(entry_at) FROM paper_positions WHERE engine = 'solana_fresh'"),
        ("last losing close on the Solana paper book (cooldown clock)",
         "SELECT max(p.exit_at) FROM paper_positions p JOIN paper_accounts a ON a.id = p.account_id WHERE a.name = 'solana' "
         "AND p.status = 'closed' AND p.realized_pnl < 0"),
    ]
    out.append("\n  last events (these are different things):")
    for label, q in lasts:
        out.append(f"  {label:58s} {_t((await s.execute(text(q))).scalar_one())}")
    st = (await s.execute(text("SELECT state, count(*) n FROM trading_candidates WHERE engine = 'migration' AND created_at >= :since "
                               "GROUP BY 1 ORDER BY 2 DESC"), {"since": now - timedelta(hours=24)})).all()
    out.append("  migration candidates of the last 24 h by state: " + ", ".join(f"{r.state} {r.n}" for r in st))
    blk = (await s.execute(text("SELECT detail->'entry_blocked'->>'codes' codes, count(*) n, min(detail->'entry_blocked'->>'at') a, "
                                "max(detail->'entry_blocked'->>'at') b FROM trading_candidates WHERE engine = 'migration' "
                                "AND created_at >= :since AND detail ? 'entry_blocked' GROUP BY 1 ORDER BY 2 DESC"),
                           {"since": now - timedelta(hours=24)})).all()
    out.append("  migration candidates (24 h) whose entry was refused before token data, by codes (first..last refusal):")
    out += [f"    {r.codes:48s} {r.n:>5}  {str(r.a)[5:19]} .. {str(r.b)[5:19]}" for r in blk] or ["    none"]
    day0 = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for label, start in (("yesterday", day0 - timedelta(days=1)), ("today", day0)):
        closed = (await s.execute(text(
            "SELECT p.engine, p.exit_at, p.realized_pnl, p.entry_cost_quote, p.exit_reason, p.max_loss_quote FROM paper_positions p "
            "JOIN paper_accounts a ON a.id = p.account_id WHERE a.name = 'solana' AND p.status = 'closed' "
            "AND p.exit_at >= :a AND p.exit_at < :b ORDER BY p.exit_at"), {"a": start, "b": start + timedelta(days=1)})).all()
        out.append(f"\n  closed on the Solana paper book {label} (UTC day {start:%m-%d}); running sums:")
        acct, own = Decimal(0), defaultdict(Decimal)
        for r in closed:
            acct += r.realized_pnl or 0
            own[r.engine] += r.realized_pnl or 0
            out.append(f"    {_t(r.exit_at)} {r.engine:17s} size {_u(r.entry_cost_quote)} planned max loss {_u(r.max_loss_quote)} "
                       f"pnl {_n(r.realized_pnl, 5)} {r.exit_reason:16s} | engine today {own[r.engine]:+.5f} account today {acct:+.5f}")
        if not closed:
            out.append("    none")
    try:
        from yonixalpha_core import risk_summary
        from yonixalpha_core.db.redis import make_redis

        redis = make_redis(get_settings())
        try:
            rs = await risk_summary.build(s, redis, now)
        finally:
            await redis.aclose()
        out.append("\n  risk engine right now (yonixalpha_core.risk_summary, the same calculation the gate uses):")
        for a in rs["accounts"]:
            out.append(f"    account {a['account']} {a['mode']}: {a['status']}; realized today {a['realized_pnl_today_sol']} of limit "
                       f"{a['max_daily_loss_sol']}; equity {a['equity_sol']}; open {a['open_positions']}")
        for st_ in rs["strategies"]:
            eff = st_["effective"]
            out.append(f"    {st_['engine']:17s} {st_['strategy_mode']:6s} {st_['status']:30s} blockers "
                       f"{[b['code'] for b in st_['blockers']]}; own today {st_['own_realized_pnl_today_sol']} of own limit "
                       f"{st_['own_daily_limit_sol']}; size {st_['trade_size']}; per-trade loss limit "
                       f"{eff.get('max_loss_per_trade_sol')}; risk {eff.get('risk_per_trade_pct')}; engine values {_j(st_['engine_values'], 200)}")
            if st_.get("last_applied_trade"):
                la = st_["last_applied_trade"]
                out.append(f"      last trade {la['at'][5:19]} size {la['size_sol']} method {str(la['method'])[:90]}")
        out.append(f"    entries refused before token data today: {rs['entries_blocked_today']}")
    except Exception as exc:  # noqa: BLE001 - the rest of the report stands without it
        out.append(f"  risk summary unavailable: {type(exc).__name__}: {str(exc)[:200]}")
    return out


# --- paper vs live ---------------------------------------------------------------------------

async def section_parity(s) -> list[str]:
    out = ["\n== PARITY: PAPER vs LIVE =="]
    pairs = (await s.execute(text(
        "SELECT p.asset_id, p.engine, p.entry_at pa, l.entry_at la, p.entry_price pe, l.entry_price le, "
        "p.realized_pnl_pct pr, l.realized_pnl_pct lr, p.exit_reason px, l.exit_reason lx, l.status ls "
        "FROM paper_positions p JOIN paper_positions l ON l.asset_id = p.asset_id AND l.execution_mode = 'LIVE' "
        "AND abs(extract(epoch FROM l.entry_at - p.entry_at)) < 600 "
        "WHERE coalesce(p.execution_mode,'PAPER') = 'PAPER' AND p.engine LIKE 'solana%' ORDER BY p.entry_at LIMIT 60"))).all()
    out.append(f"  same token traded PAPER and LIVE within 10 minutes: {len(pairs)}")
    for r in pairs:
        ratio = (r.le / r.pe) if r.pe and r.le else None
        out.append(f"    {r.asset_id[:44]:44s} {r.engine:17s} paper {_t(r.pa)} live {_t(r.la)} live/paper entry price "
                   f"{'-' if ratio is None else f'{ratio:.3f}'} paper {_p(r.pr)} {r.px} | live {_p(r.lr)} {r.lx} {r.ls}")
    lo = (await s.execute(text("SELECT status, engine, count(*) n, round(sum(coalesce(entry_cost_quote,0)),5) cost, "
                               "round(sum(coalesce(proceeds_quote,0)),5) back FROM paper_positions WHERE execution_mode = 'LIVE' "
                               "AND status <> 'closed' GROUP BY 1, 2 ORDER BY 1, 2"))).all()
    out.append("  LIVE positions not closed (their cost is not in realized PnL):")
    out += [f"    {r.status:14s} {r.engine:17s} {r.n:>4} cost {r.cost} back {r.back}" for r in lo] or ["    none"]
    od = (await s.execute(text("SELECT side, status, count(*) n, min(created_at) a, max(created_at) b FROM execution_orders "
                               "WHERE mode = 'LIVE' GROUP BY 1, 2 ORDER BY 1, 2"))).all()
    out.append("  LIVE orders by side and status (first..last):")
    out += [f"    {r.side:5s} {r.status:10s} {r.n:>6}  {_t(r.a)} .. {_t(r.b)}" for r in od] or ["    none"]
    er = (await s.execute(text("SELECT side, left(coalesce(error,'-'), 90) e, count(*) n FROM execution_orders WHERE mode = 'LIVE' "
                               "AND status = 'FAILED' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10"))).all()
    out.append("  most common LIVE failure messages:")
    out += [f"    {r.side:5s} x{r.n:<5} {r.e}" for r in er] or ["    none"]
    lat = (await s.execute(text(
        "SELECT p.engine, count(*) n, percentile_cont(0.5) WITHIN GROUP (ORDER BY (o.diagnostics->'timing'->>'decision_to_confirm_ms')::numeric) "
        f"FILTER (WHERE o.diagnostics->'timing'->>'decision_to_confirm_ms' ~ '{NUM}') med_ms, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY (o.diagnostics->'timing'->>'discovery_to_decision_ms')::numeric) "
        f"FILTER (WHERE o.diagnostics->'timing'->>'discovery_to_decision_ms' ~ '{NUM}') disc_ms "
        "FROM execution_orders o JOIN paper_positions p ON p.id = o.position_id WHERE o.mode = 'LIVE' AND o.side = 'BUY' "
        "AND o.status = 'CONFIRMED' GROUP BY 1"))).all()
    out.append("  LIVE confirmed buys: median decision-to-confirmation and discovery-to-decision (execution_analysis timing):")
    out += [f"    {r.engine:17s} n={r.n:<4} decision->confirm {r.med_ms if r.med_ms is not None else '-'} ms, "
            f"discovery->decision {r.disc_ms if r.disc_ms is not None else '-'} ms" for r in lat] or ["    none"]
    cls = (await s.execute(text(
        "SELECT p.engine, coalesce(o.diagnostics->'price'->>'classification', '-') c, count(*) n FROM execution_orders o "
        "JOIN paper_positions p ON p.id = o.position_id WHERE o.mode = 'LIVE' AND o.side = 'BUY' AND o.status = 'CONFIRMED' "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC"))).all()
    out.append("  LIVE confirmed buys by price-execution class (execution_analysis):")
    out += [f"    {r.engine:17s} {r.c:28s} {r.n}" for r in cls] or ["    none"]
    return out


# --- main -----------------------------------------------------------------------------------------

async def build(s, sections: list[str], top: int, deep_n: int, marks: list[tuple[datetime, str]], now: datetime) -> list[str]:
    out = [f"STRATEGY RECOVERY EVIDENCE at {now:%Y-%m-%d %H:%M:%S} UTC (read-only transaction)"]
    out.append("periods (deploy times from the operator's deploy output): " + "; ".join(
        f"P{i + 1} from {at:%m-%d %H:%M} {name}" for i, (at, name) in enumerate(marks)))
    rows: list[Any] = []
    if any(x in sections for x in ("trades", "top", "deep", "compare")):
        rows = await load_trades(s)
    if "book" in sections:
        out += await section_book(s)
    if "settings" in sections:
        out += await section_settings(s)
    if "trades" in sections:
        out += section_trades(rows, marks)
    picks: list[Any] = []
    if "top" in sections or "deep" in sections:
        lines, picks = section_top(rows, top)
        if "top" in sections:
            out += lines
    if "deep" in sections:
        out += await section_deep(s, picks[:deep_n])
    if "compare" in sections:
        out += section_compare(rows)
    if "migration" in sections:
        out += await section_migration(s, now)
    if "parity" in sections:
        out += await section_parity(s)
    out.append("\nNothing was written.")
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--section", action="append", choices=SECTIONS, help="repeatable; default all")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--deep", type=int, default=12)
    ap.add_argument("--split", action="append", default=[], help="extra period marker, ISO time (UTC)")
    a = ap.parse_args(argv)
    marks = [(datetime.fromisoformat(at), name) for at, name in DEPLOYS]
    for x in a.split:
        t = datetime.fromisoformat(x)
        marks.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), f"marker {x}"))
    marks.sort()
    now = datetime.now(timezone.utc)
    engine = make_engine(get_settings())
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            await s.execute(text("SET LOCAL statement_timeout = '60s'"))
            lines = await build(s, a.section or list(SECTIONS), max(1, min(a.top, 50)), max(0, min(a.deep, 25)), marks, now)
            await s.rollback()
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
