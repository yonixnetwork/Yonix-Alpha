"""Models on safety-gate decisions: data quality, challenger training,
champion/challenger evaluation, and drift monitoring (spec §40-46, §88-89).

Loop per model name (gate_solana_fresh, gate_solana_momentum, ...):
1. quality check every labeled, unchecked sample; quarantine bad ones with a
   data_quality_events row (never silently trained on);
2. if enough clean samples: train a challenger (logistic regression) with a
   forward-in-time holdout; evaluate challenger AND the current champion on
   that same holdout (AUC with a confidence bound, Brier calibration,
   precision/recall, false-positive/negative rates, stability across the two
   holdout halves, paper return of trades the model would have favoured);
3. mark the challenger `promotable` only if it clears the statistical bar
   and beats the champion — promotion itself is an operator action;
4. drift: PSI of each feature and of predictions (recent vs the champion's
   training reference) plus recent labeled accuracy; above threshold ->
   MODEL_DRIFT_DETECTED (system event, notification, Redis flag that makes
   decision-engine ignore the model until it clears).
"""

import io
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events
from yonixalpha_core.db.models import DataQualityEvent, MLFeatureSnapshot, ModelVersion, SystemEvent
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.gate_features import (
    ACTIVE_MODELS,
    DRIFT_FLAG_PREFIX,
    ENGINES_FOR_MODEL,
    FEATURE_VERSION,
    FEATURES_FOR_MODEL,
    vector,
)

from app.train import AUC_CONFIDENCE_Z, MIN_ACTIVATION_AUC, _auc_standard_error

log = get_logger("ml.gate")

MIN_SAMPLES = 50
MIN_HOLDOUT = 10
HOLDOUT_FRACTION = 0.25
PSI_DRIFT = 0.25
ACCURACY_DROP_DRIFT = 0.15
DRIFT_WINDOW = timedelta(days=7)
DRIFT_FLAG_TTL = 2 * 3600
BINS = 10


def drift_flag_key(name: str) -> str:
    return f"{DRIFT_FLAG_PREFIX}{name}"


# --- 1. data quality -------------------------------------------------------------

def quality_issues(row: MLFeatureSnapshot, model_name: str, seen_assessments: set) -> list[str]:
    issues = []
    if row.assessment_id is not None:
        if row.assessment_id in seen_assessments:
            issues.append("duplicate")
        seen_assessments.add(row.assessment_id)
    if row.label not in (0, 1):
        issues.append("invalid_label")
    out = row.outcome or {}
    if not out:
        issues.append("incomplete_trade")
    elif out.get("profitable") is not None and bool(out.get("profitable")) != bool(row.label):
        issues.append("label_outcome_mismatch")
    if out and not out.get("exit_reason"):
        issues.append("corrupted_execution")
    vec = vector(model_name, row.features or {})
    if vec is None:
        issues.append("missing_fields")
    else:
        for k, v in vec.items():
            if (k.endswith("_share") and not 0 <= v <= 1) or (k in ("unique_buyers", "trade_count", "liquidity_quote",
                                                                     "window_volume", "age_seconds", "volatility") and v < 0):
                issues.append(f"impossible_value:{k}")
    decided = (row.features or {}).get("decision_at")
    try:
        at = datetime.fromisoformat(decided) if decided else None
    except ValueError:
        at = None
    if at is None:
        issues.append("timestamp_missing")
    elif row.created_at and at > row.created_at + timedelta(seconds=5):
        # Features claiming to be from after the row was written = future leakage.
        issues.append("future_leakage")
    return issues


async def run_quality_check(session: AsyncSession, model_name: str) -> dict[str, int]:
    rows = (await session.execute(
        select(MLFeatureSnapshot).where(MLFeatureSnapshot.engine.in_(ENGINES_FOR_MODEL[model_name]),
                                        MLFeatureSnapshot.label.is_not(None), MLFeatureSnapshot.quality_status.is_(None),
                                        MLFeatureSnapshot.feature_version == FEATURE_VERSION)
        .order_by(MLFeatureSnapshot.created_at)
    )).scalars().all()
    seen = set((await session.execute(
        select(MLFeatureSnapshot.assessment_id).where(MLFeatureSnapshot.quality_status == "ok",
                                                      MLFeatureSnapshot.assessment_id.is_not(None))
    )).scalars().all())
    counts = {"ok": 0, "quarantined": 0}
    for row in rows:
        issues = quality_issues(row, model_name, seen)
        if issues:
            row.quality_status = "quarantined"
            counts["quarantined"] += 1
            for issue in issues:
                session.add(DataQualityEvent(source="ml", record_type="ml_features", record_id=str(row.id), issue=issue,
                                             detail={"model": model_name}))
        else:
            row.quality_status = "ok"
            counts["ok"] += 1
    return counts


# --- 2/3. challenger training and champion comparison ------------------------------

def _psi_reference(values: list[float]) -> dict:
    s = sorted(values)
    edges = [s[min(len(s) - 1, int(len(s) * q / BINS))] for q in range(1, BINS)]
    return {"edges": edges, "props": _props(values, edges)}


def _props(values: list[float], edges: list[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        i = sum(1 for e in edges if v > e)
        counts[i] += 1
    n = max(len(values), 1)
    return [c / n for c in counts]


def psi(ref_props: list[float], cur_props: list[float]) -> float:
    total = 0.0
    for r, c in zip(ref_props, cur_props):
        r, c = max(r, 1e-4), max(c, 1e-4)
        total += (c - r) * math.log(c / r)
    return total


def _metrics(y: list[int], scores: list[float], returns: list[float]) -> dict[str, Any]:
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    out: dict[str, Any] = {"holdout_size": len(y), "positives": n_pos, "negatives": n_neg}
    if n_pos == 0 or n_neg == 0:
        out["auc"] = None
        return out
    auc = roc_auc_score(y, scores)
    out["auc"] = auc
    out["auc_lower_bound"] = auc - AUC_CONFIDENCE_Z * _auc_standard_error(auc, n_pos, n_neg)
    out["brier"] = brier_score_loss(y, scores)
    pred = [1 if s >= 0.5 else 0 for s in scores]
    tp = sum(1 for p, t in zip(pred, y) if p and t)
    fp = sum(1 for p, t in zip(pred, y) if p and not t)
    fn = sum(1 for p, t in zip(pred, y) if not p and t)
    tn = sum(1 for p, t in zip(pred, y) if not p and not t)
    out["precision"] = tp / (tp + fp) if tp + fp else None
    out["recall"] = tp / (tp + fn) if tp + fn else None
    out["false_positive_rate"] = fp / (fp + tn) if fp + tn else None
    out["false_negative_rate"] = fn / (fn + tp) if fn + tp else None
    half = len(y) // 2
    halves = []
    for a, b in ((0, half), (half, len(y))):
        ys, ss = y[a:b], scores[a:b]
        if 0 < sum(ys) < len(ys):
            halves.append(roc_auc_score(ys, ss))
    out["stability_auc_gap"] = abs(halves[0] - halves[1]) if len(halves) == 2 else None
    favoured = [r for r, s in zip(returns, scores) if s >= 0.5]
    out["paper_return_favoured_avg"] = sum(favoured) / len(favoured) if favoured else None
    out["paper_return_all_avg"] = sum(returns) / len(returns) if returns else None
    return out


async def train_challenger(session: AsyncSession, model_name: str) -> dict[str, Any]:
    rows = (await session.execute(
        select(MLFeatureSnapshot).where(MLFeatureSnapshot.engine.in_(ENGINES_FOR_MODEL[model_name]),
                                        MLFeatureSnapshot.quality_status == "ok").order_by(MLFeatureSnapshot.created_at)
    )).scalars().all()
    names = FEATURES_FOR_MODEL[model_name]
    # One sample per trade: a candidate re-evaluated every cycle has many
    # snapshots carrying the same single outcome; keep the last one (the
    # decision that actually entered). Counting them all would overstate
    # the evidence (see app.train on independent observations).
    latest: dict = {}
    for r in rows:
        latest[r.candidate_id or r.assessment_id or r.id] = r
    rows = sorted(latest.values(), key=lambda r: r.created_at)
    data = [(vector(model_name, r.features), r.label, float((r.outcome or {}).get("return_pct") or 0)) for r in rows]
    data = [d for d in data if d[0] is not None]
    if len(data) < MIN_SAMPLES:
        return {"status": "skipped_insufficient_samples", "samples": len(data), "required": MIN_SAMPLES}
    previous = (await session.execute(
        select(ModelVersion).where(ModelVersion.name == model_name, ModelVersion.status.in_(("challenger", "active")))
        .order_by(ModelVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    if previous is not None and (previous.metrics or {}).get("dataset_size") == len(data):
        return {"status": "skipped_no_new_samples", "samples": len(data)}
    if len({d[1] for d in data}) < 2:
        return {"status": "skipped_single_class", "samples": len(data)}
    cut = len(data) - max(MIN_HOLDOUT, int(len(data) * HOLDOUT_FRACTION))
    train, hold = data[:cut], data[cut:]
    if len({d[1] for d in train}) < 2:
        return {"status": "skipped_single_class_train", "samples": len(data)}
    X = [[d[0][n] for n in names] for d in train]
    y = [d[1] for d in train]
    est = LogisticRegression(max_iter=1000).fit(X, y)
    Xh = [[d[0][n] for n in names] for d in hold]
    yh = [d[1] for d in hold]
    rets = [d[2] for d in hold]
    scores = [float(p) for p in est.predict_proba(Xh)[:, 1]]
    metrics: dict[str, Any] = {"challenger": _metrics(yh, scores, rets), "training_samples": len(train),
                               "split": "temporal", "feature_version": FEATURE_VERSION}
    champion = await registry.get_active_model_row(session, model_name)
    if champion is not None:
        champ_est = joblib.load(io.BytesIO(champion.artifact))
        champ_scores = [float(p) for p in champ_est.predict_proba([[d[0][n] for n in champion.feature_names] for d in hold])[:, 1]]
        metrics["champion"] = {"version": champion.version, **_metrics(yh, champ_scores, rets)}
    c = metrics["challenger"]
    ch = metrics.get("champion")
    beats = ch is None or (c.get("auc") is not None and ch.get("auc") is not None and c["auc"] >= ch["auc"]
                           and (c.get("brier") or 1) <= (ch.get("brier") or 1))
    metrics["promotable"] = bool(
        c.get("auc") is not None and len(hold) >= MIN_HOLDOUT and c["auc"] >= MIN_ACTIVATION_AUC
        and c.get("auc_lower_bound") is not None and c["auc_lower_bound"] > 0.5 and beats
    )
    metrics["promotion_note"] = ("eligible — promote from ML Review after checking the metrics" if metrics["promotable"]
                                 else "not eligible — needs AUC >= 0.55 with lower bound > 0.5 and to beat the champion")
    metrics["reference"] = {n: _psi_reference([row[i] for row in X]) for i, n in enumerate(names)}
    metrics["reference"]["__prediction__"] = _psi_reference([float(p) for p in est.predict_proba(X)[:, 1]])
    metrics["reference_accuracy"] = sum(1 for s, t in zip(scores, yh) if (s >= 0.5) == bool(t)) / len(yh)
    metrics["dataset_size"] = len(data)
    # Only the newest challenger is a candidate for promotion.
    await session.execute(update(ModelVersion).where(ModelVersion.name == model_name, ModelVersion.status == "challenger")
                          .values(status="superseded"))
    mv = await registry.register_trained_model(session, name=model_name, estimator=est, feature_names=names,
                                               training_sample_count=len(train), metrics=metrics, status="challenger")
    return {"status": "challenger_registered", "version": mv.version, "promotable": metrics["promotable"]}


# --- 4. drift ------------------------------------------------------------------

async def check_drift(session: AsyncSession, redis, app_settings, model_name: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    champion = await registry.get_active_model_row(session, model_name)
    if champion is None or "reference" not in (champion.metrics or {}):
        return {"status": "no champion"}
    ref = champion.metrics["reference"]
    rows = (await session.execute(
        select(MLFeatureSnapshot).where(MLFeatureSnapshot.engine.in_(ENGINES_FOR_MODEL[model_name]),
                                        MLFeatureSnapshot.created_at >= now - DRIFT_WINDOW)
    )).scalars().all()
    vecs = [v for v in (vector(model_name, r.features) for r in rows) if v is not None]
    if len(vecs) < 20:
        return {"status": "insufficient recent data", "recent": len(vecs)}
    feature_psi = {n: psi(ref[n]["props"], _props([v[n] for v in vecs], ref[n]["edges"])) for n in champion.feature_names if n in ref}
    est = joblib.load(io.BytesIO(champion.artifact))
    preds = [float(p) for p in est.predict_proba([[v[n] for n in champion.feature_names] for v in vecs])[:, 1]]
    pred_psi = psi(ref["__prediction__"]["props"], _props(preds, ref["__prediction__"]["edges"]))
    labeled = [(r, vector(model_name, r.features)) for r in rows if r.label is not None]
    labeled = [(r, v) for r, v in labeled if v is not None]
    acc = None
    if len(labeled) >= 10:
        ps = est.predict_proba([[v[n] for n in champion.feature_names] for _, v in labeled])[:, 1]
        acc = sum(1 for p, (r, _) in zip(ps, labeled) if (p >= 0.5) == bool(r.label)) / len(labeled)
    worst = max(list(feature_psi.values()) + [pred_psi])
    acc_drop = (champion.metrics.get("reference_accuracy") - acc) if acc is not None and champion.metrics.get("reference_accuracy") else None
    drifted = worst > PSI_DRIFT or (acc_drop is not None and acc_drop > ACCURACY_DROP_DRIFT)
    report = {"status": "MODEL_DRIFT_DETECTED" if drifted else "ok", "checked_at": now.isoformat(), "recent": len(vecs),
              "feature_psi": feature_psi, "prediction_psi": pred_psi, "recent_accuracy": acc, "accuracy_drop": acc_drop,
              "thresholds": {"psi": PSI_DRIFT, "accuracy_drop": ACCURACY_DROP_DRIFT}}
    champion.metrics = {**champion.metrics, "drift": report}
    if drifted:
        if redis is not None:
            await redis.set(drift_flag_key(model_name), "1", ex=DRIFT_FLAG_TTL)
        session.add(SystemEvent(service="ml", event_type="MODEL_DRIFT_DETECTED", severity="warning",
                                detail={"model": model_name, "version": champion.version, "worst_psi": worst, "accuracy_drop": acc_drop}))
        if redis is None or await redis.set(f"yx:notified:drift:{model_name}", "1", nx=True, ex=86400):
            await events.notify(session, redis, app_settings, "ml_drift", f"Model drift: {model_name} v{champion.version}",
                                f"worst PSI {worst:.3f}; model ignored by the decision engine until drift clears", "warning")
    elif redis is not None:
        await redis.delete(drift_flag_key(model_name))
    await events.publish(redis, "ml.model.updated", {"model": model_name, "drift": report["status"]}, "ml")
    return report


async def run_cycle(session_factory, redis, app_settings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for model_name in ACTIVE_MODELS:
        async with session_factory() as session:
            quality = await run_quality_check(session, model_name)
            training = await train_challenger(session, model_name)
            drift = await check_drift(session, redis, app_settings, model_name)
            await session.commit()
        out[model_name] = {"quality": quality, "training": training, "drift": drift.get("status")}
    return out

