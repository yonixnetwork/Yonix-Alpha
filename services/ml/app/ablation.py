"""Do the scanner-intelligence features actually help? Measured, not assumed.

1. Incremental evaluation (the required experiment):
     A  price + volume + buyers
     B  A + wallet graph
     C  B + deployer intelligence
     D  C + organic demand
     E  D + manipulation (score families + manufactured-pump detector)
     F  full feature set
2. Ablations: the full set without each layer (without_wallet_graph,
   without_deployer_intelligence, without_smart_money,
   without_organic_volume, without_manipulation_detector).
3. Activity-matched validation: tokens WITH a relationship / manipulation
   signal vs tokens WITHOUT it in the same strata (engine, stage, data
   regime, time-of-day, buyers, market cap, age, liquidity, and the number
   of high-activity recycled wallets among the early buyers), so a
   difference is not just "busier tokens behave differently".

Every model is trained on the same forward-in-time split as the shadow
models (oldest 75 % train, newest 25 % holdout, labels known only after
the holdout starts purged) and scored on the same holdout. Reported per
feature set and target: sample size and period, feature availability,
ROC-AUC / PR-AUC with a paired-bootstrap 95 % interval of the difference to
the baseline, Brier and calibration error, precision / recall / false
positives / false negatives at the top decile, the mean executable return
and drawdown of the rows the model would select, missed winners, and
results by engine (Fresh / Migrated / Momentum) and market regime.

A layer is reported as ADDS_VALUE only when its ranking improvement's
interval excludes zero AND calibration does not get worse; otherwise
NO_EVIDENCE (or HURTS). The largest historical return is never the
selection criterion. Matched differences are associations within
comparable tokens, not causal proof.

Nothing here changes a decision: results are stored for review
(Redis ABLATION_KEY, the ML Review page) and printed by
`python -m app.ablation`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.ml.opportunity_features import FEATURE_NAMES, FEATURE_VERSION, GROUPS, group_features
from yonixalpha_core.ml.readiness import ABLATION_KEY

from app.shadow_ml import MIN_POSITIVES_TRAIN, Sample, load_ledger, time_split

ABLATION_TTL = 14 * 86400
ABLATION_EVERY_SECONDS = 6 * 3600
VERSION = "ablation-1"
TARGETS = {"P_FAST_DUMP": "fast_dump", "P_RUG": "rug_60m", "P_UPSIDE_50": "upside_50", "P_UPSIDE_100": "upside_100"}
AVOID_TARGETS = ("P_FAST_DUMP", "P_RUG")  # the model's flag means "avoid"
RETURN_LABEL, DRAWDOWN_LABEL, WINNER_LABEL = "executable_return_primary_pct", "max_drawdown_pct", "upside_100"
BOOTSTRAP = 200
MIN_ROWS = 200
MIN_STRATUM_ROWS = 2

_BASE = group_features("price_volume_buyers")
_CUM = {
    "A_price_volume_buyers": _BASE,
    "B_plus_wallet_graph": _BASE + group_features("wallet_graph"),
}
_CUM["C_plus_deployer"] = _CUM["B_plus_wallet_graph"] + group_features("deployer")
_CUM["D_plus_organic_demand"] = _CUM["C_plus_deployer"] + group_features("organic_demand")
_CUM["E_plus_manipulation"] = _CUM["D_plus_organic_demand"] + group_features("manipulation")
_CUM["F_full"] = FEATURE_NAMES
EXPERIMENTS: dict[str, tuple[str, ...]] = dict(_CUM)
ABLATIONS: dict[str, str] = {"without_wallet_graph": "wallet_graph", "without_deployer_intelligence": "deployer",
                             "without_smart_money": "smart_money", "without_organic_volume": "organic_demand",
                             "without_manipulation_detector": "manipulation"}
for _name, _group in ABLATIONS.items():
    _drop = set(group_features(_group))
    EXPERIMENTS[_name] = tuple(n for n in FEATURE_NAMES if n not in _drop)
# Which cumulative step adds which layer (for the verdicts).
STEPS = [("B_plus_wallet_graph", "A_price_volume_buyers", "wallet_graph"),
         ("C_plus_deployer", "B_plus_wallet_graph", "deployer"),
         ("D_plus_organic_demand", "C_plus_deployer", "organic_demand"),
         ("E_plus_manipulation", "D_plus_organic_demand", "manipulation")]


def _cols(samples: list[Sample], names: tuple[str, ...], med: dict[str, float]) -> list[list[float]]:
    return [[s.x[n] if s.x.get(n) is not None else med.get(n, 0.0) for n in names] for s in samples]


def _medians(train: list[Sample]) -> dict[str, float]:
    out = {}
    for n in FEATURE_NAMES:
        vals = [s.x[n] for s in train if s.x.get(n) is not None]
        out[n] = statistics.median(vals) if vals else 0.0
    return out


def ece(y: list[int], p: list[float], bins: int = 10) -> float:
    """Expected calibration error."""
    total, err = len(y), 0.0
    for i in range(bins):
        idx = [j for j, v in enumerate(p) if i / bins <= v < (i + 1) / bins or (i == bins - 1 and v == 1.0)]
        if idx:
            err += len(idx) / total * abs(statistics.fmean(p[j] for j in idx) - statistics.fmean(y[j] for j in idx))
    return round(err, 4)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def decision_metrics(target: str, y: list[int], p: list[float], hold: list[Sample]) -> dict[str, Any]:
    """Top-decile policy: for upside targets the flagged rows are the ones
    the model would ENTER; for dump / rug targets the ones it would AVOID."""
    k = max(1, len(p) // 10)
    order = sorted(range(len(p)), key=lambda j: p[j], reverse=True)
    flagged = set(order[:k])
    tp = sum(1 for j in flagged if y[j])
    fp = k - tp
    fn = sum(y) - tp
    out: dict[str, Any] = {"policy": "top decile of scores", "flagged": k, "precision": round(tp / k, 4),
                           "recall": round(tp / sum(y), 4) if sum(y) else None, "false_positives": fp, "false_negatives": fn}
    entered = [j for j in range(len(p)) if (j not in flagged) == (target in AVOID_TARGETS)]
    entered_set = set(entered)
    rets = [r for j in entered if (r := _num(hold[j].labels.get(RETURN_LABEL))) is not None]
    dds = [d for j in entered if (d := _num(hold[j].labels.get(DRAWDOWN_LABEL))) is not None]
    all_rets = [r for s in hold if (r := _num(s.labels.get(RETURN_LABEL))) is not None]
    winners = [j for j in range(len(hold)) if hold[j].labels.get(WINNER_LABEL) is True]
    out.update({
        "entered_rows": len(entered),
        "expected_executable_return_pct": round(statistics.fmean(rets), 3) if rets else None,
        "all_rows_executable_return_pct": round(statistics.fmean(all_rets), 3) if all_rets else None,
        "mean_max_drawdown_pct": round(statistics.fmean(dds), 3) if dds else None,
        "missed_winners": sum(1 for j in winners if j not in entered_set),
        "false_entries": (sum(1 for j in entered if y[j]) if target in AVOID_TARGETS
                          else sum(1 for j in entered if not y[j])),
        "premature_exits": "not applicable to entry models (see the ledger's exit analysis)",
    })
    return out


def _segments(y: list[int], p: list[float], hold: list[Sample]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("engine", "data_regime"):
        for val in sorted({s.segment[key] for s in hold}):
            idx = [j for j, s in enumerate(hold) if s.segment[key] == val]
            ys, ps = [y[j] for j in idx], [p[j] for j in idx]
            e: dict[str, Any] = {"n": len(idx), "positives": sum(ys)}
            if 0 < sum(ys) < len(ys):
                e["roc_auc"] = round(roc_auc_score(ys, ps), 4)
                e["pr_auc"] = round(average_precision_score(ys, ps), 4)
            out[f"{key}={val}"] = e
    return out


def paired_bootstrap(y: list[int], p_new: list[float], p_base: list[float], seed: int = 7) -> dict[str, Any]:
    """95 % interval of (AUC_new − AUC_base) and (PR-AUC_new − PR-AUC_base)
    over resamples of the same holdout rows."""
    rng = random.Random(seed)
    n = len(y)
    d_auc, d_pr = [], []
    for _ in range(BOOTSTRAP):
        idx = [rng.randrange(n) for _ in range(n)]
        ys = [y[i] for i in idx]
        if 0 < sum(ys) < n:
            a, b = [p_new[i] for i in idx], [p_base[i] for i in idx]
            d_auc.append(roc_auc_score(ys, a) - roc_auc_score(ys, b))
            d_pr.append(average_precision_score(ys, a) - average_precision_score(ys, b))

    def ci(v: list[float]) -> list[float] | None:
        if len(v) < 20:
            return None
        v = sorted(v)
        return [round(v[int(0.025 * len(v))], 4), round(v[int(0.975 * len(v)) - 1], 4)]
    return {"delta_roc_auc_ci95": ci(d_auc), "delta_pr_auc_ci95": ci(d_pr), "resamples": len(d_auc)}


def availability(samples: list[Sample]) -> dict[str, float]:
    """Share of rows where at least one feature of the group is observed."""
    out = {}
    for g, names in GROUPS.items():
        out[g] = round(sum(1 for s in samples if any(s.x.get(n) is not None for n in names)) / len(samples), 4) if samples else 0.0
    return out


def evaluate(samples: list[Sample]) -> dict[str, Any]:
    samples = sorted(samples, key=lambda s: s.decided_at)
    out: dict[str, Any] = {"version": VERSION, "feature_version": FEATURE_VERSION,
                           "computed_at": datetime.now(timezone.utc).isoformat(), "samples": len(samples)}
    if len(samples) < MIN_ROWS:
        return {**out, "status": f"skipped: {len(samples)} labelled rows (needs {MIN_ROWS})"}
    train, hold, split = time_split(samples)
    out.update({"status": "EVALUATED", "split": split,
                "period": {"first": samples[0].decided_at.isoformat(), "last": samples[-1].decided_at.isoformat()},
                "feature_availability": {"train": availability(train), "holdout": availability(hold)},
                "missing_share_holdout": round(statistics.fmean(
                    sum(1 for n in FEATURE_NAMES if not n.endswith("__missing") and s.x.get(n) is None) /
                    sum(1 for n in FEATURE_NAMES if not n.endswith("__missing")) for s in hold), 4)})
    med = _medians(train)
    targets: dict[str, Any] = {}
    for target, key in TARGETS.items():
        tr = [s for s in train if isinstance(s.labels.get(key), bool)]
        ho = [s for s in hold if isinstance(s.labels.get(key), bool)]
        ytr, yho = [int(s.labels[key]) for s in tr], [int(s.labels[key]) for s in ho]
        if sum(ytr) < MIN_POSITIVES_TRAIN or not (0 < sum(yho) < len(yho)):
            targets[target] = {"status": "skipped", "reason": f"{sum(ytr)} training / {sum(yho)} holdout positives"}
            continue
        preds: dict[str, list[float]] = {}
        res: dict[str, Any] = {"status": "EVALUATED", "train_rows": len(tr), "holdout_rows": len(ho),
                               "holdout_positives": sum(yho), "sets": {}}
        for name, cols in EXPERIMENTS.items():
            est = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)).fit(_cols(tr, cols, med), ytr)
            p = [float(v) for v in est.predict_proba(_cols(ho, cols, med))[:, 1]]
            preds[name] = p
            res["sets"][name] = {"features": len(cols), "roc_auc": round(roc_auc_score(yho, p), 4),
                                 "pr_auc": round(average_precision_score(yho, p), 4),
                                 "brier": round(brier_score_loss(yho, p), 4), "calibration_error": ece(yho, p),
                                 "decision": decision_metrics(target, yho, p, ho), "segments": _segments(yho, p, ho)}
        verdicts: dict[str, Any] = {}
        for new, base, layer in STEPS:
            verdicts[f"{layer} (step {new} vs {base})"] = _verdict(yho, preds[new], preds[base], res["sets"][new], res["sets"][base])
        for name, layer in ABLATIONS.items():
            verdicts[f"{layer} (F_full vs {name})"] = _verdict(yho, preds["F_full"], preds[name], res["sets"]["F_full"],
                                                               res["sets"][name])
        res["verdicts"] = verdicts
        targets[target] = res
    out["targets"] = targets
    out["matched"] = matched_validation(samples)
    out["note"] = ("Layers are judged on out-of-sample ranking with a bootstrap interval and on calibration, never on the "
                   "largest historical return. Matched comparisons are associations within comparable tokens, not causal proof.")
    return out


def _verdict(y, p_new, p_base, m_new, m_base) -> dict[str, Any]:
    b = paired_bootstrap(y, p_new, p_base)
    lo = (b["delta_roc_auc_ci95"] or [None])[0]
    hi = (b["delta_roc_auc_ci95"] or [None, None])[1]
    calib_ok = m_new["brier"] <= m_base["brier"] + 0.002
    if lo is not None and lo > 0 and calib_ok:
        verdict = "ADDS_VALUE"
    elif hi is not None and hi < 0:
        verdict = "HURTS"
    else:
        verdict = "NO_EVIDENCE"
    return {"verdict": verdict, "delta_roc_auc": round(m_new["roc_auc"] - m_base["roc_auc"], 4),
            "delta_pr_auc": round(m_new["pr_auc"] - m_base["pr_auc"], 4),
            "delta_brier": round(m_new["brier"] - m_base["brier"], 4), **b}


# --- activity-matched validation --------------------------------------------------------------

def _bucket(v: float | None, edges: tuple[float, ...]) -> str:
    if v is None:
        return "?"
    for i, e in enumerate(edges):
        if v < e:
            return str(i)
    return str(len(edges))


def stratum(s: Sample) -> tuple:
    x = s.x
    return (s.segment["engine"], s.segment["stage"], s.segment["data_regime"], s.decided_at.hour // 6,
            _bucket(x.get("raw_unique_buyers") if x.get("raw_unique_buyers") is not None else x.get("buyers"), (5, 10, 20, 50)),
            _bucket(x.get("market_cap_sol"), (30, 40, 60, 100)), _bucket(x.get("age_seconds"), (30, 120, 600)),
            _bucket(x.get("liquidity_sol"), (5, 20, 80)), _bucket(x.get("recycled_wallets"), (1, 3, 6)))


TREATMENTS: dict[str, Callable[[dict], bool | None]] = {
    "coordinated_or_funding_cluster": lambda x: None if x.get("funding_cluster_size") is None else x["funding_cluster_size"] >= 2,
    "creator_related_buyers": lambda x: None if x.get("creator_related_buyers") is None else x["creator_related_buyers"] >= 1,
    "co_dump_cohort_medium_plus": lambda x: None if x.get("dump_cluster_level") is None else x["dump_cluster_level"] >= 2,
    "manufactured_pump_pattern": lambda x: None if x.get("manufactured_pattern_duration") is None else x["manufactured_pattern_duration"] > 0,
    "low_organic_demand": lambda x: None if x.get("organic_demand_ratio") is None else x["organic_demand_ratio"] < 0.2,
    "smart_money_present": lambda x: None if x.get("smart_proven_wallets") is None else x["smart_proven_wallets"] >= 1,
}
OUTCOMES = ("fast_dump", "rug_60m", "upside_50", "upside_100")


def matched_comparison(samples: list[Sample], treat: Callable[[dict], bool | None]) -> dict[str, Any]:
    rows = [(s, t) for s in samples if (t := treat(s.x)) is not None]
    treated = [s for s, t in rows if t]
    strata: dict[tuple, dict[str, list[Sample]]] = defaultdict(lambda: {"t": [], "c": []})
    for s, t in rows:
        strata[stratum(s)]["t" if t else "c"].append(s)
    used = {k: v for k, v in strata.items() if v["t"] and v["c"] and len(v["t"]) + len(v["c"]) >= MIN_STRATUM_ROWS}
    matched_t = sum(len(v["t"]) for v in used.values())
    out: dict[str, Any] = {"rows_with_evidence": len(rows), "treated": len(treated), "matched_treated": matched_t,
                           "strata_used": len(used), "unmatched_treated": len(treated) - matched_t}
    if not matched_t:
        return {**out, "status": "NO_MATCHES"}
    res: dict[str, Any] = {}
    for o in OUTCOMES + (RETURN_LABEL,):
        num = var = 0.0
        w_tot = 0
        raw_t, raw_c = [], []
        for v in used.values():
            ft = [float(s.labels[o]) for s in v["t"] if _num(s.labels.get(o)) is not None or isinstance(s.labels.get(o), bool)]
            fc = [float(s.labels[o]) for s in v["c"] if _num(s.labels.get(o)) is not None or isinstance(s.labels.get(o), bool)]
            if not ft or not fc:
                continue
            mt, mc = statistics.fmean(ft), statistics.fmean(fc)
            vt = statistics.pvariance(ft) / len(ft) if len(ft) > 1 else 0.0
            vc = statistics.pvariance(fc) / len(fc) if len(fc) > 1 else 0.0
            w = len(ft)
            num += w * (mt - mc)
            var += w * w * (vt + vc)
            w_tot += w
        for s, t in rows:
            val = s.labels.get(o)
            if isinstance(val, (bool, int, float)):
                (raw_t if t else raw_c).append(float(val))
        if not w_tot:
            continue
        diff = num / w_tot
        se = (var ** 0.5) / w_tot
        res[o] = {"matched_difference": round(diff, 4), "ci95": [round(diff - 1.96 * se, 4), round(diff + 1.96 * se, 4)],
                  "raw_difference": round(statistics.fmean(raw_t) - statistics.fmean(raw_c), 4) if raw_t and raw_c else None,
                  "treated_rows": w_tot}
    return {**out, "status": "COMPARED", "outcomes": res,
            "note": "treated minus matched controls in the same strata (engine, stage, regime, time of day, buyers, market cap, "
                    "age, liquidity, recycled-wallet activity); an association, not a causal effect"}


def matched_validation(samples: list[Sample]) -> dict[str, Any]:
    return {name: matched_comparison(samples, fn) for name, fn in TREATMENTS.items()}


async def load_samples(session: AsyncSession, since: datetime | None = None) -> list[Sample]:
    """The newest labelled ledger rows (bounded: shadow_ml.load_ledger)."""
    return await load_ledger(session, since)


async def run_ablation(session_factory, redis, *, force: bool = False, since: datetime | None = None) -> dict[str, Any]:
    """Every ABLATION_EVERY_SECONDS (or on demand): evaluate and store."""
    if redis is not None and not force:
        last = await redis.get(ABLATION_KEY)
        if last:
            try:
                at = datetime.fromisoformat(json.loads(last)["computed_at"])
                if (datetime.now(timezone.utc) - at).total_seconds() < ABLATION_EVERY_SECONDS:
                    return {"status": "skipped: evaluated recently"}
            except (ValueError, KeyError, TypeError):
                pass
    async with session_factory() as session:
        samples = await load_samples(session, since)
    result = await asyncio.to_thread(evaluate, samples)
    if redis is not None:
        await redis.set(ABLATION_KEY, json.dumps(result, default=str), ex=ABLATION_TTL)
    return result


def summary_lines(r: dict[str, Any]) -> list[str]:
    lines = [f"ablation {r.get('version')} · features {r.get('feature_version')} · {r.get('samples')} labelled rows · {r.get('status')}"]
    if r.get("status") != "EVALUATED":
        return lines
    lines.append(f"period {r['period']['first']} → {r['period']['last']} · split {r['split']}")
    lines.append(f"feature availability (holdout): {r['feature_availability']['holdout']} · missing share {r['missing_share_holdout']}")
    for target, t in r["targets"].items():
        if t.get("status") != "EVALUATED":
            lines.append(f"\n{target}: {t.get('status')} ({t.get('reason')})")
            continue
        lines.append(f"\n{target}: train {t['train_rows']} · holdout {t['holdout_rows']} ({t['holdout_positives']} positive)")
        lines.append(f"  {'set':32} {'feat':>4} {'AUC':>6} {'PR-AUC':>6} {'Brier':>6} {'ECE':>6} {'prec':>5} {'recall':>6} "
                     f"{'FP':>5} {'FN':>5} {'E[ret]%':>8} {'DD%':>7} {'missW':>5}")
        for name, m in t["sets"].items():
            d = m["decision"]
            lines.append(f"  {name:32} {m['features']:>4} {m['roc_auc']:>6} {m['pr_auc']:>6} {m['brier']:>6} "
                         f"{m['calibration_error']:>6} {d['precision']:>5} {str(d['recall']):>6} {d['false_positives']:>5} "
                         f"{d['false_negatives']:>5} {str(d['expected_executable_return_pct']):>8} "
                         f"{str(d['mean_max_drawdown_pct']):>7} {d['missed_winners']:>5}")
        for k, v in t["verdicts"].items():
            lines.append(f"  {v['verdict']:12} {k}: ΔAUC {v['delta_roc_auc']} CI {v['delta_roc_auc_ci95']} · "
                         f"ΔPR {v['delta_pr_auc']} · ΔBrier {v['delta_brier']}")
    lines.append("\nactivity-matched validation (treated − matched controls):")
    for name, m in r["matched"].items():
        lines.append(f"  {name}: {m['status']} · treated {m['treated']} (matched {m['matched_treated']} in {m['strata_used']} strata)")
        for o, v in (m.get("outcomes") or {}).items():
            lines.append(f"      {o:32} matched {v['matched_difference']:>8} CI {v['ci95']} · raw {v['raw_difference']}")
    lines.append("\n" + r.get("note", ""))
    return lines


async def _main() -> None:
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory
    from yonixalpha_core.db.redis import make_redis

    ap = argparse.ArgumentParser(description="Scanner-intelligence feature ablation (read-only; stores the result for the dashboard)")
    ap.add_argument("--since", help="only decisions at or after this ISO time (UTC)")
    ap.add_argument("--json", action="store_true", help="print the full JSON result")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    settings = get_settings()
    engine = make_engine(settings)
    redis = make_redis(settings)
    try:
        r = await run_ablation(make_session_factory(engine), redis, force=True, since=since)
    finally:
        await redis.aclose()
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else "\n".join(summary_lines(r)))


if __name__ == "__main__":
    asyncio.run(_main())
