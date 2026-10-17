"""Chronological validation of one module on one symbol.

1. Splits, in time order: train 60% | validation 20% | out-of-sample 20%.
   Indicators may read bars before a window (they are the past), never after.
2. The DEFAULT parameters (from the published rule, never fitted here) are
   run on every split. Their out-of-sample result is the main evidence.
3. A small grid (at most 3 x 3 around the defaults of the module's first two
   numeric parameters) is searched on TRAIN only; the chosen set is then
   reported on validation and out-of-sample. Nothing is tuned on the
   out-of-sample period.
4. Anchored walk-forward, 4 folds: choose on everything before the fold,
   test on the fold. Reported: positive folds and the pooled fold trades.
5. Sensitivity: the grid's train+validation results; "robust" when at least
   two thirds of the grid points have positive expectancy.
6. Benchmark: buy-and-hold over the out-of-sample period.

Verdict (documented ranking rule, win rate is never used):
- insufficient evidence: fewer than MIN_TRADES out-of-sample trades (default parameters);
- failed: out-of-sample expectancy <= 0 R or profit factor < 1;
- weak: positive, but fewer than 60% positive walk-forward folds, or a
  sensitivity that is not robust, or a drawdown above 25%;
- candidate: none of the above. A candidate is only "approved for
  consideration" for paper / demo testing; never for live trading.
Ranking: candidate, weak, insufficient evidence, failed; then out-of-sample
expectancy in R (higher first); then drawdown (lower first).
"""

from __future__ import annotations

from decimal import Decimal
from itertools import product
from typing import Any

from yonixalpha_crypto.backtest import metrics
from yonixalpha_crypto.backtest.engine import Costs, RiskConfig, simulate
from yonixalpha_crypto.candles import INTERVAL_SECONDS
from yonixalpha_crypto.strategies.library import COMMON_PARAMS, Module, Series

TIERS = ("candidate", "weak", "insufficient evidence", "failed")
FOLDS = 4


def splits(n: int, start: int) -> dict[str, tuple[int, int]]:
    usable = n - start
    a, b = start + int(usable * 0.6), start + int(usable * 0.8)
    return {"train": (start, a), "validation": (a, b), "out_of_sample": (b, n)}


def grid(module: Module) -> list[dict[str, Any]]:
    keys = [k for k, v in module.params.items() if v[1] in ("int", "dec") and k not in COMMON_PARAMS
            and Decimal(str(v[3])) > 1][:2]
    axes = []
    for k in keys:
        default, kind, lo, hi = module.params[k]
        vals = []
        for f in (Decimal("0.75"), Decimal(1), Decimal("1.25")):
            x = Decimal(str(default)) * f
            x = min(max(x, Decimal(str(lo))), Decimal(str(hi)))
            vals.append(int(x.to_integral_value()) if kind == "int" else str(x.quantize(Decimal("0.0001"))))
        axes.append([(k, v) for v in dict.fromkeys(vals)])
    combos = [dict(c) for c in product(*axes)] if axes else [{}]
    return combos


def _objective(m: dict[str, Any]) -> tuple:
    if m["trades"] < 10 or m["expectancy_r"] is None:
        return (0, 0.0, 0.0)
    return (1, m["expectancy_r"], -m["max_drawdown_pct"])


def buy_and_hold(s: Series, a: int, b: int) -> dict[str, Any]:
    if b - a < 2:
        return {"return_pct": None, "max_drawdown_pct": None}
    peak, mdd = 0.0, 0.0
    for c in s.c[a:b]:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak * 100)
    return {"return_pct": round((s.c[b - 1] / s.o[a] - 1) * 100, 4), "max_drawdown_pct": round(mdd, 4)}


def study(s: Series, module: Module, params: dict[str, Any], exits: dict[str, Any] | None, costs: Costs,
          risk: RiskConfig, interval: str, *, progress=None, optimize: bool = True) -> dict[str, Any]:
    bpd = 86400 / INTERVAL_SECONDS[interval]
    start = module.lookback(module.p(params))
    if len(s) - start < 200:
        return {"error": f"not enough bars: {len(s)} (need {start + 200})"}
    sp = splits(len(s), start)

    def run(p, a, b):
        res = simulate(s, module, p, exits, costs, risk, a, b, bpd)
        return res, metrics.compute(res, risk.starting_equity, bpd)

    out: dict[str, Any] = {"splits": {k: {"from": s.times[a].isoformat() if s.times else a,
                                          "to": s.times[b - 1].isoformat() if s.times else b - 1, "bars": b - a}
                                      for k, (a, b) in sp.items()}}
    default = {}
    curve = None
    for name, (a, b) in sp.items():
        res, m = run(params, a, b)
        default[name] = m
        if name == "out_of_sample":
            curve = metrics.sample_curve(res.equity, res.times)
            out["oos_trades_sample"] = [t.as_dict() for t in res.trades[-100:]]
    out["default_params"] = {"params": params, "metrics": default}
    out["oos_curve"] = curve
    if progress:
        progress("splits done")

    combos = grid(module) if optimize else [{}]
    tr_a, tr_b = sp["train"]
    va_a, va_b = sp["validation"]
    sens = []
    best, best_key = None, None
    for c in combos:
        p = {**params, **c}
        _, m_tr = run(p, tr_a, tr_b)
        _, m_tv = run(p, tr_a, va_b)
        sens.append({"params": c, "train": _brief(m_tr), "train_validation": _brief(m_tv)})
        k = _objective(m_tr)
        if best_key is None or k > best_key:
            best, best_key = c, k
    pos = sum(1 for x in sens if (x["train_validation"]["expectancy_r"] or 0) > 0)
    out["sensitivity"] = {"grid": sens, "positive_points": pos, "points": len(sens),
                          "robust": len(sens) > 1 and pos >= 2 * len(sens) / 3}
    sel = {**params, **(best or {})}
    out["selected_on_train"] = {"params": best or {}, "metrics": {n: run(sel, a, b)[1] for n, (a, b) in sp.items()
                                                                  if n != "train"}}
    if progress:
        progress("grid done")

    wf = []
    n0 = start
    L = (len(s) - n0) // (FOLDS + 1)
    pooled = 0
    for f in range(FOLDS):
        a, b = n0 + (f + 1) * L, n0 + (f + 2) * L if f < FOLDS - 1 else len(s)
        best_f, key_f = None, None
        for c in combos:
            k = _objective(run({**params, **c}, n0, a)[1])
            if key_f is None or k > key_f:
                best_f, key_f = c, k
        _, m = run({**params, **(best_f or {})}, a, b)
        pooled += m["trades"]
        wf.append({"fold": f + 1, "train_to": s.times[a - 1].isoformat() if s.times else a,
                   "test": [s.times[a].isoformat() if s.times else a, s.times[b - 1].isoformat() if s.times else b],
                   "params": best_f or {}, "metrics": _brief(m)})
    positive = sum(1 for x in wf if (x["metrics"]["expectancy_r"] or 0) > 0 and x["metrics"]["trades"] > 0)
    out["walk_forward"] = {"folds": wf, "positive_folds": positive, "pooled_trades": pooled}
    oa, ob = sp["out_of_sample"]
    out["benchmark_buy_and_hold_oos"] = buy_and_hold(s, oa, ob)
    out["verdict"] = verdict(default["out_of_sample"], out["walk_forward"], out["sensitivity"])
    return out


def _brief(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m[k] for k in ("trades", "net_profit", "net_return_pct", "expectancy_r", "profit_factor",
                              "max_drawdown_pct", "win_rate")}


def verdict(oos: dict[str, Any], wf: dict[str, Any], sens: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    if oos["trades"] < metrics.MIN_TRADES:
        tier = "insufficient evidence"
        reasons.append(f"{oos['trades']} out-of-sample trades (< {metrics.MIN_TRADES})")
    elif (oos["expectancy_r"] or 0) <= 0 or (oos["profit_factor"] is not None and oos["profit_factor"] < 1):
        tier = "failed"
        reasons.append(f"out-of-sample expectancy {oos['expectancy_r']} R, profit factor {oos['profit_factor']}")
    else:
        weak = []
        if wf["positive_folds"] < 0.6 * FOLDS:
            weak.append(f"only {wf['positive_folds']}/{FOLDS} positive walk-forward folds")
        if not sens["robust"]:
            weak.append(f"sensitivity not robust ({sens['positive_points']}/{sens['points']} grid points positive)")
        if oos["max_drawdown_pct"] > 25:
            weak.append(f"drawdown {oos['max_drawdown_pct']}% above 25%")
        tier = "weak" if weak else "candidate"
        reasons += weak or ["positive out-of-sample expectancy, robust walk-forward and sensitivity"]
    return {"tier": tier, "reasons": reasons, "rank_key": [TIERS.index(tier), -(oos["expectancy_r"] or 0),
                                                           oos["max_drawdown_pct"]],
            "approved_for_consideration": tier == "candidate",
            "note": "a candidate may go to paper / exchange-demo testing; nothing here authorises live trading"}
