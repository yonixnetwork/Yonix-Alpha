"""Momentum strategy research: read-only evidence for the momentum upgrade.

Usage (on the server, inside the api container):
    python -m yonixalpha_core.tools.momentum_research [--section trades|compare|replay|exits ...] [--split ISO]

Sections:
  trades   every closed momentum position: what the decision saw (features
           recorded AT the decision: 1 m / 5 m returns, accelerations, flow
           state, buyer breadth, curve state, creator sells), the v2 checks
           re-run on those features, then what happened (MFE / MAE, exit,
           loss class, post-exit). One line per trade.
  compare  winners vs losers on the same decision-time features (medians,
           shares), and how often each v2 check would have failed.
  replay   every recorded momentum decision with a complete outcome (GATE
           episodes, traded or not, and WATCH rows): the v1 baseline (v1
           signal qualified) against each v2 check and combinations, scored
           on the ledger's executable return at fixed horizons (reference
           0.05 SOL, pump fee both legs, curve impact, 2 s entry latency,
           fixed network costs). Chronological: the first 60 % of decisions
           (by time) is the selection period, the last 40 % out of sample.
           Two threshold sweeps choose a value on the first part only and
           report it on the second.
  exits    closed momentum positions by exit reason: net, MFE vs realized
           (give-back), and the ledger's post-exit classification.

Every feature is what was recorded at the decision. Outcomes are used only
as labels. Missing inputs are counted and shown, never filled in. Nothing
is written (read-only transaction).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.strategies import momentum_v2
from yonixalpha_core.tools.strategy_recovery import DEPLOYS, TRADES_SQL, metrics, period_of

SECTIONS = ("trades", "compare", "replay", "exits")
ENGINE = "solana_momentum"
HORIZONS = ("T+60s", "T+5m", "T+15m")
PRIMARY = "T+5m"
SELECTION_SHARE = 0.6
MIN_SELECT_N = 10
NUMERIC = ("return_1m", "return_5m", "volume_acceleration", "buyer_acceleration", "seller_acceleration",
           "new_wallet_acceleration", "trades_1m", "breadth_score", "top3_buy_share", "sell_share", "distance_from_high",
           "curve_progress", "market_cap_sol", "age_seconds", "liquidity_sol")
CATEGORICAL = ("flow_state", "manipulation_level", "manufactured_pump_risk", "creator_sold")

_X_COLS = ", ".join("o.path->'%s'->'executable'->>'executable_return_pct' AS \"x_%s\"" % (h, h) for h in HORIZONS)
LEDGER_SQL = f"""
SELECT o.id, o.decided_at, o.stage, o.decision, o.traded, o.execution_mode, o.position_id,
       o.snapshot->'intel'->'momentum' AS i_momentum, o.snapshot->'intel'->'flow_state' AS i_flow,
       o.snapshot->'intel'->'buyer_breadth' AS i_breadth, o.snapshot->'intel'->'now' AS i_now,
       o.snapshot->'intel'->'manipulation'->>'level' AS i_manip, o.snapshot->'intel'->'manufactured_pump'->>'risk' AS i_mp,
       o.snapshot->>'signal_qualified' AS signal_qualified, o.snapshot->>'curve_progress' AS curve_progress,
       o.snapshot->>'liquidity_sol' AS liquidity_sol, o.snapshot->>'creator_sold' AS creator_sold,
       o.snapshot->>'age_seconds' AS age_seconds, o.snapshot->>'market_cap_sol' AS market_cap_sol,
       {_X_COLS},
       o.labels->>'fast_dump' AS fast_dump, o.labels->>'rug_60m' AS rug_60m
FROM opportunity_outcomes o
WHERE o.engine = '{ENGINE}' AND o.status = 'COMPLETE'
ORDER BY o.decided_at LIMIT 20000
"""
OUTCOMES_SQL = f"""
SELECT position_id, loss_analysis->>'classification' AS loss_class, post_exit->>'classification' AS exit_class
FROM opportunity_outcomes WHERE engine = '{ENGINE}' AND position_id IS NOT NULL
"""


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> bool | None:
    return True if v in (True, "true", "True") else False if v in (False, "false", "False") else None


def ledger_features(r: Any) -> dict[str, Any]:
    intel = {"momentum": r.i_momentum or {}, "flow_state": r.i_flow or {}, "buyer_breadth": r.i_breadth or {},
             "now": r.i_now or {}, "manipulation": {"level": r.i_manip}, "manufactured_pump": {"risk": r.i_mp}}
    snap = {"curve_progress": r.curve_progress, "liquidity_sol": r.liquidity_sol, "creator_sold": _bool(r.creator_sold),
            "age_seconds": r.age_seconds, "market_cap_sol": r.market_cap_sol}
    return momentum_v2.features_from_intel(intel, snap)


def trade_features(r: Any) -> dict[str, Any]:
    snap = dict(r.snap or {})
    if "creator_sold" in snap:
        snap["creator_sold"] = _bool(snap["creator_sold"])
    return momentum_v2.features_from_intel(snap.get("intel"), snap)


def _checks(features: dict[str, Any]) -> dict[str, str]:
    """Each v2 check on its own (all others off), with the default thresholds."""
    ev = momentum_v2.evaluate(features, True, {})
    by = {c.name: c.status for c in ev.checks}
    return {c: by.get(c, momentum_v2.UNKNOWN) for c in momentum_v2.CHECKS}  # no features at all: every check unknown


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:+.0%}" if pct else f"{v:.2f}"
    return str(v)


# --- trades -----------------------------------------------------------------------------------

def section_trades(rows: list[Any], outcomes: dict[str, tuple], marks) -> list[str]:
    out = ["\n== TRADES: closed momentum positions; features AS RECORDED AT THE DECISION, then the outcome =="]
    if not rows:
        return out + ["  none"]
    out.append("  id       mode  entry          per symbol      pnl SOL   pnl%   hold  exit           MFE    MAE  | "
               "r1m   r5m  volA  buyA sellA newA flow        brd  top3 curve  type        creatorSold | v2 checks "
               "(exh dec brd dmp liq qw) | loss class / post-exit")
    short = {"PASS": "P", "FAIL": "F", "UNKNOWN": "?"}
    for r in rows:
        f = trade_features(r)
        chk = _checks(f)
        entry = _f(r.entry_price)
        mfe = (_f(r.highest_price) / entry - 1) if entry and _f(r.highest_price) else None
        mae = (_f(r.lowest_price) / entry - 1) if entry and _f(r.lowest_price) else None
        hold = (r.exit_at - r.entry_at).total_seconds() if r.exit_at and r.entry_at else None
        lc, ec = outcomes.get(str(r.id), (None, None))
        out.append(
            f"  {str(r.id)[:8]} {r.mode:5s} {r.entry_at.astimezone(timezone.utc):%m-%d %H:%M} {period_of(r.entry_at, marks).split(' ')[0]:3s} "
            f"{str(r.symbol or '?')[:10]:10s} {float(r.pnl or 0):+.5f} {_fmt(_f(r.pnl_pct), True):>6} {('-' if hold is None else f'{hold:.0f}s'):>6} "
            f"{str(r.exit_reason or '-')[:14]:14s} {_fmt(mfe, True):>5} {_fmt(mae, True):>5} | "
            f"{_fmt(f['return_1m'], True):>5} {_fmt(f['return_5m'], True):>5} {_fmt(f['volume_acceleration']):>5} "
            f"{_fmt(f['buyer_acceleration']):>5} {_fmt(f['seller_acceleration']):>5} {_fmt(f['new_wallet_acceleration']):>4} "
            f"{str(f['flow_state'] or '-')[:11]:11s} {_fmt(f['breadth_score']):>4} {_fmt(f['top3_buy_share'], True):>5} "
            f"{_fmt(f['curve_progress'], True):>5} {momentum_v2.entry_type(f)[:11]:11s} {_fmt(f['creator_sold']):>3} | "
            f"{' '.join(short[chk[c]] for c in momentum_v2.CHECKS):11s} | {lc or '-'} / {ec or '-'}")
    covered = sum(1 for r in rows if (r.snap or {}).get("intel"))
    out.append(f"  decision-time intelligence recorded for {covered} of {len(rows)} trades "
               "(trades without it show '-': not recorded at the time, not reconstructed)")
    return out


# --- compare ----------------------------------------------------------------------------------

def section_compare(rows: list[Any]) -> list[str]:
    out = ["\n== COMPARE: winners vs losers, decision-time features only (median; n = trades with the value) =="]
    if not rows:
        return out + ["  none"]
    feats = [(float(r.pnl or 0) > 0, trade_features(r)) for r in rows]
    wins = [f for w, f in feats if w]
    losses = [f for w, f in feats if not w]
    out.append(f"  winners {len(wins)}, losers {len(losses)} (PAPER and LIVE together; sizes differ, outcomes are signs only)")
    out.append(f"  {'feature':24s} {'winners median':>16s} {'n':>4s} {'losers median':>15s} {'n':>4s}")
    for k in NUMERIC:
        w = [f[k] for f in wins if f.get(k) is not None]
        lo = [f[k] for f in losses if f.get(k) is not None]
        out.append(f"  {k:24s} {(f'{statistics.median(w):.4f}' if w else '-'):>16s} {len(w):>4} "
                   f"{(f'{statistics.median(lo):.4f}' if lo else '-'):>15s} {len(lo):>4}")
    for k in CATEGORICAL + ("entry_type",):
        def dist(group):
            c = Counter(str(momentum_v2.entry_type(f) if k == "entry_type" else f.get(k)) for f in group)
            return ", ".join(f"{v} {n}" for v, n in c.most_common(5))
        out.append(f"  {k:24s} winners: {dist(wins) or '-'} | losers: {dist(losses) or '-'}")
    out.append("  v2 checks (default thresholds), share FAIL / UNKNOWN:")
    for c in momentum_v2.CHECKS:
        def share(group, status):
            s = [_checks(f)[c] for f in group]
            return f"{sum(x == status for x in s)}/{len(s)}"
        out.append(f"    {c:20s} winners FAIL {share(wins, 'FAIL'):>7} UNKNOWN {share(wins, 'UNKNOWN'):>7} | "
                   f"losers FAIL {share(losses, 'FAIL'):>7} UNKNOWN {share(losses, 'UNKNOWN'):>7}")
    out.append("  (a check that fails on losers more often than on winners is a candidate; the replay below tests it "
               "on every recorded decision, not only on the trades)")
    return out


# --- replay -----------------------------------------------------------------------------------

def _score(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    gains = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return {"n": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
            "win": sum(v > 0 for v in values) / len(values), "pf": gains / loss if loss > 0 else None,
            "sum": sum(values), "sum_wo_best": sum(values) - max(values)}


def _sline(name: str, s: dict[str, Any]) -> str:
    if not s.get("n"):
        return f"    {name:34s} n=0"
    pf = "-" if s["pf"] is None else f"{s['pf']:.2f}"
    return (f"    {name:34s} n={s['n']:<5} mean {s['mean']:+7.2f}% med {s['median']:+7.2f}% win {s['win']:4.0%} "
            f"PF {pf:>5} sum {s['sum']:+9.1f} w/o best {s['sum_wo_best']:+9.1f}")


def _variant_ok(features, v1: bool, enabled: list[str], overrides: dict | None = None) -> bool:
    if not enabled:
        return v1  # the baseline is the v1 signal alone, whatever features were recorded
    cfg = {f"mv2_{c}": True for c in enabled}
    cfg.update(overrides or {})
    return momentum_v2.evaluate(features, v1, cfg).qualified


def section_replay(rows: list[Any]) -> list[str]:
    out = ["\n== REPLAY: recorded momentum decisions with a complete outcome; executable return % of a 0.05 SOL "
           "reference trade (fees, curve impact, 2 s latency, network costs) =="]
    data = []
    for r in rows:
        x = {h: _f(r._mapping[f"x_{h}"]) for h in HORIZONS}
        data.append((r, ledger_features(r), _bool(r.signal_qualified), x))
    if not data:
        return out + ["  no completed momentum decision in the ledger"]
    stages = Counter(r.stage for r, *_ in data)
    out.append(f"  rows {len(data)} ({', '.join(f'{k} {v}' for k, v in sorted(stages.items()))}), "
               f"{data[0][0].decided_at:%Y-%m-%d} to {data[-1][0].decided_at:%Y-%m-%d}; "
               f"v1 qualified {sum(1 for d in data if d[2])}, executable {PRIMARY} known for "
               f"{sum(1 for d in data if d[3][PRIMARY] is not None)}")
    cut = int(len(data) * SELECTION_SHARE)
    parts = {"selection (first 60%)": data[:cut], "out of sample (last 40%)": data[cut:]}
    if cut < len(data):
        out.append(f"  out-of-sample period starts {data[cut][0].decided_at:%Y-%m-%d %H:%M} UTC")
    variants: list[tuple[str, list[str]]] = [("v1 baseline", [])] + [(f"v1 + {c}", [c]) for c in momentum_v2.CHECKS] + [
        ("v1 + exhaustion + anti_dump", ["exhaustion", "anti_dump"]),
        ("v1 + exh + dec + brd + dmp", ["exhaustion", "decay", "independent_buyers", "anti_dump"])]
    for h in HORIZONS:
        out.append(f"  -- horizon {h} (sell at the decision + {h[2:]}) --")
        for pname, part in parts.items():
            out.append(f"   {pname}")
            allx = [d[3][h] for d in part if d[3][h] is not None]
            out.append(_sline("every decision (context)", _score(allx)))
            for name, en in variants:
                vals = [d[3][h] for d in part if d[2] and d[3][h] is not None and _variant_ok(d[1], True, en)]
                out.append(_sline(name, _score(vals)))
    out.append("  coverage of each check on v1-qualified rows (UNKNOWN blocks an entry when the check is on):")
    q = [d for d in data if d[2]]
    for c in momentum_v2.CHECKS:
        st = Counter(_checks(d[1])[c] for d in q)
        out.append(f"    {c:20s} PASS {st['PASS']:>5} FAIL {st['FAIL']:>5} UNKNOWN {st['UNKNOWN']:>5}")
    out += _sweeps(data)
    out.append("  Read with care: overlapping decisions on the same token are not independent; a small n is noise; a "
               "filter is worth testing forward only if it helps out of sample, not just in the selection period.")
    return out


def _sweeps(data) -> list[str]:
    out = ["  threshold sweeps (value chosen on the selection period, by mean T+5m return with n >= "
           f"{MIN_SELECT_N}; then applied out of sample):"]
    cut = int(len(data) * SELECTION_SHARE)
    sel, oos = data[:cut], data[cut:]
    sweeps = {"exhaustion": ("mv2_max_return_5m", ["0.1", "0.2", "0.3", "0.5", "1.0", "3.0"]),
              "independent_buyers": ("mv2_min_breadth", ["0.3", "0.4", "0.5", "0.6", "0.7"])}
    for check, (key, values) in sweeps.items():
        scored = []
        for v in values:
            vals = [d[3][PRIMARY] for d in sel if d[2] and d[3][PRIMARY] is not None and _variant_ok(d[1], True, [check], {key: v})]
            s = _score(vals)
            scored.append((v, s))
            out.append(f"    {check} {key}={v:<4} selection {_sline('', s).strip()}")
        ok = [(v, s) for v, s in scored if s.get("n", 0) >= MIN_SELECT_N]
        if not ok:
            out.append(f"    {check}: no value has n >= {MIN_SELECT_N} in the selection period; nothing chosen")
            continue
        best_v, _ = max(ok, key=lambda t: t[1]["mean"])
        vals = [d[3][PRIMARY] for d in oos if d[2] and d[3][PRIMARY] is not None and _variant_ok(d[1], True, [check], {key: best_v})]
        base = [d[3][PRIMARY] for d in oos if d[2] and d[3][PRIMARY] is not None]
        out.append(f"    {check}: chosen {key}={best_v}; out of sample {_sline('', _score(vals)).strip()}")
        out.append(f"    {'':{len(check)}}  out-of-sample v1 baseline {_sline('', _score(base)).strip()}")
    return out


# --- exits ------------------------------------------------------------------------------------

def section_exits(rows: list[Any], outcomes: dict[str, tuple]) -> list[str]:
    out = ["\n== EXITS: closed momentum positions by exit reason =="]
    if not rows:
        return out + ["  none"]
    g: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        g[f"{r.mode} {r.exit_reason or '-'}"].append(r)
    out.append("  group                              n    net SOL  med pnl%  med MFE  med give-back (MFE - realized)")
    for k in sorted(g):
        v = g[k]
        m = metrics(v)
        pcts, mfes, gb = [], [], []
        for r in v:
            entry, hi, p = _f(r.entry_price), _f(r.highest_price), _f(r.pnl_pct)
            if p is not None:
                pcts.append(p)
            if entry and hi:
                mfes.append(hi / entry - 1)
                if p is not None:
                    gb.append(hi / entry - 1 - p)
        med = lambda xs: f"{statistics.median(xs):+.1%}" if xs else "-"  # noqa: E731
        out.append(f"  {k:32s} {m['n']:>4} {m['net']:+10.4f} {med(pcts):>9} {med(mfes):>8} {med(gb):>9}")
    ec = Counter(outcomes.get(str(r.id), (None, None))[1] or "not analysed" for r in rows)
    out.append("  post-exit classification (price after the exit vs at the exit): "
               + ", ".join(f"{k} {n}" for k, n in ec.most_common()))
    return out


# --- main -------------------------------------------------------------------------------------

async def build(s, sections: list[str], marks, now: datetime) -> list[str]:
    out = [f"MOMENTUM RESEARCH at {now:%Y-%m-%d %H:%M:%S} UTC (read-only transaction)"]
    trades: list[Any] = []
    outcomes: dict[str, tuple] = {}
    if any(x in sections for x in ("trades", "compare", "exits")):
        trades = [r for r in (await s.execute(text(TRADES_SQL))).all() if r.engine == ENGINE]
        outcomes = {str(r.position_id): (r.loss_class, r.exit_class) for r in (await s.execute(text(OUTCOMES_SQL))).all()}
    out.append(f"closed momentum positions: {len(trades)} "
               f"({', '.join(f'{k} {v}' for k, v in sorted(Counter(r.mode for r in trades).items())) or 'none'})"
               if trades else "")
    if "trades" in sections:
        out += section_trades(trades, outcomes, marks)
    if "compare" in sections:
        out += section_compare(trades)
    if "replay" in sections:
        out += section_replay(list((await s.execute(text(LEDGER_SQL))).all()))
    if "exits" in sections:
        out += section_exits(trades, outcomes)
    out.append("\nNothing was written.")
    return [x for x in out if x != ""]


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--section", action="append", choices=SECTIONS, help="repeatable; default all")
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
            await s.execute(text("SET LOCAL statement_timeout = '90s'"))
            lines = await build(s, a.section or list(SECTIONS), marks, now)
            await s.rollback()
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
