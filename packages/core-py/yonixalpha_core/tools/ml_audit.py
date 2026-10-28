"""ML audit (read-only): is the ML engine helping, hurting, or unknown? Prints
no secrets; nothing is written. One READ ONLY transaction.

    $C exec -T api python -m yonixalpha_core.tools.ml_audit [--section NAME ...] [--since 2026-10-01] [--until ...]
        [--engine E] [--json]

Sections (default: all)
  registry     every model name: versions by status, whether any version was ever ACTIVE (the only status
               a decision loads), the latest version's training size, split and holdout metrics, and for gate
               challengers whether they were marked promotable
  usage        did any model influence a decision? promotions / retirements in the audit log, the ML
               contribution setting, min_ml_confidence per risk scope, positions carrying a model version,
               ledger decisions scored by a champion at the decision, ML_BELOW_MIN findings (risk
               assessments are pruned after the retention days)
  predictions  the shadow models' scores on ledger decisions, PROSPECTIVE ones only (trained before the
               decision and scored before T+5m: ml.prediction_audit), against what happened:
                 binary targets: base rate, mean prediction, Brier score, ROC-AUC, calibration bins
                 executable-return model: actual return by prediction quintile, rank correlation, and the
                 top quintile against (a) the baseline gate's traded decisions, (b) a naive baseline that
                 takes every decision, with a 95 % bootstrap interval of the difference
  leakage      prospective vs retrospective scores, scores written after the outcome horizon, scores by
               models trained after the decision, repeated ledger rows per token (correlated samples),
               missing labels

ML influence on trades, by construction (safety.pipeline.champion_prediction, safety.gate):
  a gate model is used only when an operator promoted it to ACTIVE (ML Review, audited); it can only add
  caution (ML_BELOW_MIN -> WAIT, and only when min_ml_confidence is set), never lift a risk block.
  shadow_* models are never loaded for decisions. A verdict is UNKNOWN when fewer than MIN_N prospective
  predictions have outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core import forward_test
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.ml.prediction_audit import classify

SECTIONS = ("registry", "usage", "predictions", "leakage")
MIN_N = 100
# shadow targets -> ledger label key (services/ml/app/shadow_ml.BINARY_TARGETS; kept equal by test)
BINARY_LABELS = {"P_UPSIDE_50": "upside_50", "P_UPSIDE_100": "upside_100", "P_UPSIDE_200": "upside_200",
                 "P_MIGRATE": "migrate_60m", "P_FAST_DUMP": "fast_dump", "P_RECOVERY": "recovery", "P_RUG": "rug_60m"}
RETURN_TARGET = "E_EXECUTABLE_RETURN"

WINDOW = ("(CAST(:since AS timestamptz) IS NULL OR decided_at >= :since) AND "
          "(CAST(:until AS timestamptz) IS NULL OR decided_at < :until) AND engine = ANY(:engines)")

REGISTRY_SQL = """
SELECT name, count(*) AS versions, count(*) FILTER (WHERE activated_at IS NOT NULL) AS ever_activated,
       jsonb_object_agg(status, n) AS by_status, max(version) AS latest
FROM (SELECT name, status, activated_at, version, count(*) OVER (PARTITION BY name, status) AS n FROM model_versions) x
GROUP BY name ORDER BY name
"""

LATEST_SQL = """
SELECT DISTINCT ON (name) name, version, status, trained_at, activated_at, training_sample_count,
       metrics->'split' AS split, metrics->'holdout' AS holdout, metrics->'challenger' AS challenger,
       metrics->'promotable' AS promotable, metrics->>'promotion_note' AS promotion_note
FROM model_versions ORDER BY name, version DESC
"""

PRED_SQL = f"""
SELECT engine, decided_at, traded, executable_return_pct AS ret, labels, mint,
       ml_shadow->>'scored_at' AS scored_at, ml_shadow->'scores' AS scores
FROM opportunity_outcomes WHERE ml_shadow IS NOT NULL AND {WINDOW}
"""
STREAM_ROWS = 2000  # rows per fetch: the ledger is read in batches, never held in memory at once

# A champion score can only exist after some model was first made ACTIVE.
FIRST_ACTIVE_SQL = "SELECT min(coalesce(activated_at, trained_at)) AS at, count(*) AS n FROM model_versions " \
                   "WHERE activated_at IS NOT NULL OR status = 'active'"


BOOT_CAP = 5000  # values per group the bootstrap resamples (a fixed-seed subsample above that keeps it fast)


def _cap(v: list[float]) -> list[float]:
    return v if len(v) <= BOOT_CAP else random.Random(7).sample(v, BOOT_CAP)


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def auc(y: list[int], p: list[float]) -> float | None:
    """ROC-AUC by ranks (Mann-Whitney U), ties averaged."""
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(p)), key=lambda i: p[i])
    ranks = [0.0] * len(p)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rsum = sum(r for r, t in zip(ranks, y) if t)
    return round((rsum - pos * (pos + 1) / 2) / (pos * neg), 4)


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None

    def rank(v: list[float]) -> list[float]:
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(o):
            r[i] = float(pos)
        return r

    ra, rb = rank(a), rank(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 4) if va and vb else None


def binary_eval(y: list[int], p: list[float]) -> dict[str, Any]:
    n = len(y)
    out: dict[str, Any] = {"n": n, "base_rate": round(sum(y) / n, 4) if n else None,
                           "mean_prediction": round(statistics.fmean(p), 4) if n else None}
    if not n:
        return out
    out["brier"] = round(statistics.fmean((pi - yi) ** 2 for pi, yi in zip(p, y)), 4)
    base = out["base_rate"]
    out["brier_of_base_rate"] = round(statistics.fmean((base - yi) ** 2 for yi in y), 4)  # the naive forecast
    out["roc_auc"] = auc(y, p)
    bins = []
    for i in range(5):
        lo, hi = i / 5, (i + 1) / 5
        idx = [j for j, v in enumerate(p) if lo <= v < hi or (i == 4 and v == 1.0)]
        if idx:
            bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx), "mean_prediction": round(statistics.fmean(p[j] for j in idx), 4),
                         "observed_rate": round(sum(y[j] for j in idx) / len(idx), 4)})
    out["calibration"] = bins
    out["verdict"] = ("UNKNOWN: fewer than %d prospective predictions with outcomes" % MIN_N if n < MIN_N else
                      "better than the base-rate forecast" if out["brier"] < out["brier_of_base_rate"] else
                      "NOT better than the base-rate forecast")
    return out


def return_eval(pred: list[float], actual: list[float], traded: list[bool]) -> dict[str, Any]:
    n = len(pred)
    out: dict[str, Any] = {"n": n}
    if not n:
        return out
    order = sorted(range(n), key=lambda i: pred[i])
    q = []
    for k in range(5):
        idx = order[k * n // 5:(k + 1) * n // 5]
        if idx:
            q.append({"quintile": k + 1, "n": len(idx), "mean_prediction_pct": round(statistics.fmean(pred[i] for i in idx), 2),
                      "mean_actual_pct": round(statistics.fmean(actual[i] for i in idx), 2),
                      "positive_share": round(sum(1 for i in idx if actual[i] > 0) / len(idx), 3)})
    out["quintiles"] = q
    out["rank_correlation"] = spearman(pred, actual)
    top = [actual[i] for i in order[4 * n // 5:]]
    allv = actual
    base = [a for a, t in zip(actual, traded) if t]
    favoured_traded = [a for a, t, pr in zip(actual, traded, pred) if t and pr > 0]
    out["naive_take_every_decision"] = {"n": n, "mean_pct": round(statistics.fmean(allv), 3)}
    out["baseline_gate_traded"] = {"n": len(base), "mean_pct": round(statistics.fmean(base), 3) if base else None}
    out["ml_top_quintile"] = {"n": len(top), "mean_pct": round(statistics.fmean(top), 3) if top else None}
    out["baseline_traded_and_ml_favoured"] = {"n": len(favoured_traded),
                                              "mean_pct": round(statistics.fmean(favoured_traded), 3) if favoured_traded else None}
    d1 = forward_test.bootstrap_diff_ci(_cap(top), _cap(allv)) if len(top) >= 2 and n >= 2 else None
    d2 = forward_test.bootstrap_diff_ci(_cap(top), _cap(base)) if len(top) >= 2 and len(base) >= 2 else None
    out["top_minus_naive_ci"] = [round(x, 3) for x in d1] if d1 else None
    out["top_minus_baseline_ci"] = [round(x, 3) for x in d2] if d2 else None
    if n < MIN_N:
        out["verdict"] = f"UNKNOWN: fewer than {MIN_N} prospective predictions with outcomes"
    elif d1 and d1[0] > 0:
        out["verdict"] = "top quintile beats taking every decision (interval above 0); says nothing about live fills"
    elif d1 and d1[1] < 0:
        out["verdict"] = "top quintile WORSE than taking every decision"
    else:
        out["verdict"] = "no difference established (interval includes 0)"
    return out


async def build(s, sections: tuple[str, ...], engines: tuple[str, ...], since: datetime | None,
                until: datetime | None) -> dict[str, Any]:
    p = {"since": since, "until": until, "engines": list(engines)}
    out: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "since": _iso(since), "until": _iso(until),
                           "engines": list(engines)}
    if "registry" in sections:
        latest = {r.name: r for r in (await s.execute(text(LATEST_SQL))).all()}
        out["registry"] = []
        for r in (await s.execute(text(REGISTRY_SQL))).all():
            lt = latest.get(r.name)
            out["registry"].append({
                "name": r.name, "versions": int(r.versions), "by_status": r.by_status, "ever_activated": int(r.ever_activated),
                "latest_version": int(r.latest), "latest_status": lt.status if lt else None,
                "latest_trained_at": _iso(lt.trained_at) if lt else None,
                "training_samples": lt.training_sample_count if lt else None,
                "split": lt.split if lt else None, "holdout": lt.holdout if lt else None,
                "challenger_metrics": lt.challenger if lt else None, "promotable": lt.promotable if lt else None})
    if "usage" in sections:
        ev = (await s.execute(text(
            "SELECT event_type, count(*) AS n, max(created_at) AS last FROM audit_logs "
            "WHERE event_type IN ('ml.promoted', 'ml.champion_retired') OR event_type LIKE 'ml_lab.%' "
            "OR event_type LIKE 'ml_governance%' OR event_type LIKE 'ml.contribution%' GROUP BY 1 ORDER BY 1"))).all()
        contrib = (await s.execute(text("SELECT value, updated_at FROM platform_settings WHERE key = 'ml_contribution'"))).one_or_none()
        minconf = (await s.execute(text(
            "SELECT DISTINCT ON (scope) scope, version, settings->>'min_ml_confidence' AS v FROM risk_settings "
            "ORDER BY scope, version DESC"))).all()
        pos = (await s.execute(text(
            "SELECT engine, coalesce(execution_mode, 'PAPER') AS mode, count(*) FILTER (WHERE model_version IS NOT NULL) AS with_model, "
            "count(*) AS n FROM paper_positions WHERE engine = ANY(:engines) GROUP BY 1, 2 ORDER BY 1, 2"), p)).all()
        first = (await s.execute(text(FIRST_ACTIVE_SQL))).one()
        # No model ever ACTIVE: no decision could carry a champion score, so the
        # snapshots (large, and many) are not read at all.
        champ = [] if first.at is None else (await s.execute(text(
            f"SELECT engine, count(*) AS n FROM opportunity_outcomes WHERE {WINDOW} AND stage = 'GATE' "
            "AND decided_at >= :first_active AND snapshot->>'ml_score' IS NOT NULL GROUP BY 1 ORDER BY 1"),
            {**p, "first_active": first.at})).all()
        below = (await s.execute(text(
            "SELECT count(*) AS n, min(evaluated_at) AS lo, max(evaluated_at) AS hi FROM risk_assessments ra "
            "WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(ra.assessment->'findings') f WHERE f->>'code' = 'ML_BELOW_MIN')"))).one()
        out["usage"] = {
            "audit_events": [{"event": r.event_type, "count": int(r.n), "last": _iso(r.last)} for r in ev],
            "ml_contribution": {"value": contrib.value, "updated_at": _iso(contrib.updated_at)} if contrib else
            "not set: every model at the default SHADOW, 0 % (ml.governance)",
            "min_ml_confidence_by_scope": {r.scope: r.v for r in minconf},
            "positions_with_model_version": [{"engine": r.engine, "mode": r.mode, "with_model": int(r.with_model), "of": int(r.n)}
                                             for r in pos],
            "models_ever_active": int(first.n), "first_active_at": _iso(first.at),
            "ledger_rows_scored_by_a_champion_at_the_decision": {r.engine: int(r.n) for r in champ},
            "ml_below_min_findings_retained": {"count": int(below.n), "from": _iso(below.lo), "to": _iso(below.hi)},
        }
    if "predictions" in sections or "leakage" in sections:
        trained = {(r.name, int(r.version)): r.trained_at for r in (await s.execute(text(
            "SELECT name, version, trained_at FROM model_versions"))).all()}
        stream = await s.stream(text(PRED_SQL).execution_options(yield_per=STREAM_ROWS), p)
        n_rows = 0
        kinds = {"PROSPECTIVE": 0, "RETROSPECTIVE": 0}
        late, newer_model = 0, 0
        per_target: dict[tuple[str, str], dict[str, list]] = {}
        mints: dict[str, int] = {}
        unlabelled = 0
        async for r in stream:
            n_rows += 1
            mints[r.mint] = mints.get(r.mint, 0) + 1
            labels = r.labels or {}
            if not labels or "unknown" in labels:
                unlabelled += 1
            try:
                scored = datetime.fromisoformat(r.scored_at) if r.scored_at else None
            except ValueError:
                scored = None
            for target, sc in (r.scores or {}).items():
                if not isinstance(sc, dict) or sc.get("value") is None:
                    continue
                t_at = trained.get((sc.get("model"), int(sc.get("version") or 0)))
                kind = classify(r.decided_at, scored, t_at)
                kinds[kind] += 1
                if scored is not None and kind == "RETROSPECTIVE" and t_at is not None and t_at < r.decided_at:
                    late += 1
                if t_at is not None and t_at >= r.decided_at:
                    newer_model += 1
                if kind != "PROSPECTIVE":
                    continue
                for key in (("ALL", target), (r.engine, target)):
                    b = per_target.setdefault(key, {"pred": [], "y": [], "ret": [], "traded": []})
                    if target == RETURN_TARGET:
                        if r.ret is not None:
                            b["pred"].append(float(sc["value"]))
                            b["ret"].append(float(r.ret))
                            b["traded"].append(bool(r.traded))
                    elif target in BINARY_LABELS and isinstance(labels.get(BINARY_LABELS[target]), bool):
                        b["pred"].append(float(sc["value"]))
                        b["y"].append(1 if labels[BINARY_LABELS[target]] else 0)
        if "predictions" in sections:
            preds: dict[str, Any] = {}
            for (eng, target), b in sorted(per_target.items()):
                preds[f"{eng} {target}"] = (return_eval(b["pred"], b["ret"], b["traded"]) if target == RETURN_TARGET
                                            else binary_eval(b["y"], b["pred"]))
            out["predictions"] = preds
        if "leakage" in sections:
            repeated = sorted(mints.values(), reverse=True)
            out["leakage"] = {
                "ledger_rows_with_scores": n_rows,
                "scores": kinds, "scored_after_the_t5m_horizon": late, "scored_by_a_model_trained_after_the_decision": newer_model,
                "tokens": len(mints), "rows_per_token_max": repeated[0] if repeated else None,
                "rows_on_tokens_with_more_than_one_row": sum(v for v in repeated if v > 1),
                "rows_without_labels": unlabelled,
                "note": "only PROSPECTIVE scores are evaluated; several rows of one token are correlated, so the effective "
                        "sample is smaller than the row count"}
    return out


def _v(v: Any, d: int = 3) -> str:
    return "-" if v is None else f"{v:+.{d}f}" if isinstance(v, float) else str(v)


def render(r: dict[str, Any]) -> str:
    out = [f"ML AUDIT at {r['at'][:16]} UTC; window {(r['since'] or 'first record')[:16]} .. {(r['until'] or 'now')[:16]}. "
           "Read-only; nothing was written."]
    if "registry" in r:
        out.append("\nREGISTRY (only an ACTIVE version is ever loaded for a decision)")
        for m in r["registry"]:
            ho = m.get("holdout") or {}
            ch = m.get("challenger_metrics") or {}
            summary = {k: ho.get(k) for k in ("n", "roc_auc", "brier", "base_rate", "mae", "baseline_mae_train_mean", "beats_baseline")
                       if ho.get(k) is not None} or {k: v for k, v in ch.items() if not isinstance(v, (dict, list))}
            out.append(f"  {m['name']:<32} versions {m['versions']:<4} ever ACTIVE {m['ever_activated']}  status {m['by_status']}")
            out.append(f"      latest v{m['latest_version']} {m['latest_status']} trained {(m['latest_trained_at'] or '-')[:16]} "
                       f"samples {m['training_samples']} split {json.dumps(m['split']) if m['split'] else '-'} "
                       f"promotable {m['promotable']}  holdout {json.dumps(summary)[:240]}")
    if "usage" in r:
        u = r["usage"]
        out.append("\nUSAGE: did ML influence any decision?")
        out.append("  audit events: " + ("; ".join(f"{e['event']} x{e['count']} (last {(e['last'] or '-')[:16]})"
                                                    for e in u["audit_events"]) or "none (no promotion, retirement or lab change)"))
        out.append(f"  ML contribution setting: {json.dumps(u['ml_contribution'], default=str)[:300]}")
        out.append(f"  min_ml_confidence per scope (latest version): {u['min_ml_confidence_by_scope']}")
        out.append("  positions carrying a model version: " + ", ".join(
            f"{x['engine']} {x['mode']} {x['with_model']}/{x['of']}" for x in u["positions_with_model_version"]))
        out.append(f"  models ever made ACTIVE: {u['models_ever_active']}"
                   f"{' (first ' + u['first_active_at'][:16] + ')' if u['first_active_at'] else ': none, so no decision used a model'}")
        out.append(f"  ledger decisions scored by a champion at the decision: {u['ledger_rows_scored_by_a_champion_at_the_decision'] or 'none'}")
        b = u["ml_below_min_findings_retained"]
        out.append(f"  ML_BELOW_MIN findings in the retained risk assessments: {b['count']} ({(b['from'] or '-')[:16]} .. {(b['to'] or '-')[:16]})")
    if "predictions" in r:
        out.append(f"\nPROSPECTIVE PREDICTIONS vs OUTCOMES (shadow models; a verdict needs {MIN_N}+; outcomes are the ledger's "
                   "labels and the 0.05 SOL executable return after costs)")
        for k, e in r["predictions"].items():
            if "quintiles" in e:
                out.append(f"  {k}: n {e['n']}  rank correlation {_v(e.get('rank_correlation'))}  -> {e.get('verdict')}")
                for q in e["quintiles"]:
                    out.append(f"      quintile {q['quintile']} n {q['n']:<6} predicted {_v(q['mean_prediction_pct'], 2)}%  "
                               f"actual {_v(q['mean_actual_pct'], 2)}%  positive {q['positive_share']}")
                out.append(f"      naive (every decision) {_v(e['naive_take_every_decision']['mean_pct'])}% n {e['naive_take_every_decision']['n']}; "
                           f"baseline gate (traded) {_v(e['baseline_gate_traded']['mean_pct'])}% n {e['baseline_gate_traded']['n']}; "
                           f"ML top quintile {_v(e['ml_top_quintile']['mean_pct'])}% n {e['ml_top_quintile']['n']}; "
                           f"traded and ML favoured {_v(e['baseline_traded_and_ml_favoured']['mean_pct'])}% "
                           f"n {e['baseline_traded_and_ml_favoured']['n']}")
                out.append(f"      top - naive 95 % interval {e.get('top_minus_naive_ci')}; top - baseline {e.get('top_minus_baseline_ci')}")
            elif e.get("n"):
                out.append(f"  {k}: n {e['n']} base rate {e['base_rate']} mean prediction {e['mean_prediction']} Brier {e.get('brier')} "
                           f"(base-rate forecast {e.get('brier_of_base_rate')}) AUC {e.get('roc_auc')} -> {e.get('verdict')}")
                out.append("      calibration: " + "  ".join(f"{b['bin']}: n {b['n']} pred {b['mean_prediction']} obs {b['observed_rate']}"
                                                         for b in e.get("calibration") or []))
        if not r["predictions"]:
            out.append("  no prospective prediction with an outcome in the window: ML contribution UNKNOWN")
    if "leakage" in r:
        lk = r["leakage"]
        out.append("\nLEAKAGE AND SAMPLE CHECKS")
        out.append(f"  ledger rows with scores {lk['ledger_rows_with_scores']}; "
                   f"scores {lk['scores']}; scored after T+5m {lk['scored_after_the_t5m_horizon']}; by a model trained after the "
                   f"decision {lk['scored_by_a_model_trained_after_the_decision']}")
        out.append(f"  tokens {lk['tokens']}; max rows on one token {lk['rows_per_token_max']}; rows on tokens with more than one row "
                   f"{lk['rows_on_tokens_with_more_than_one_row']}; rows without labels {lk['rows_without_labels']}")
        out.append(f"  {lk['note']}")
    return "\n".join(out)


def _date(v: str | None) -> datetime | None:
    if not v:
        return None
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ML audit, read-only")
    ap.add_argument("--section", action="append", choices=SECTIONS, help="repeatable; default all")
    ap.add_argument("--engine", choices=forward_test.ENGINES)
    ap.add_argument("--since", help="ISO date or time, UTC (default: the first record)")
    ap.add_argument("--until", help="ISO date or time, UTC, exclusive (default: now)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    engine = make_engine(get_settings(), pool_size=1, max_overflow=0)
    try:
        async with make_session_factory(engine)() as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            await s.execute(text("SET LOCAL statement_timeout = '180s'"))
            r = await build(s, tuple(args.section or SECTIONS),
                            (args.engine,) if args.engine else forward_test.ENGINES, _date(args.since), _date(args.until))
    finally:
        await engine.dispose()
    print(json.dumps(r, indent=1, default=str) if args.json else render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
