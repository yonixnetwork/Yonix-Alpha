"""Multi-target SHADOW models on the opportunity ledger.

Every completed opportunity (traded or not) is a sample: decision-time
features (yonixalpha_core.ml.opportunity_features) and the labels the ledger
wrote after T+60m (yonixalpha_core.opportunity_analysis.labels).

Targets
  P_UPSIDE_50 / 100 / 200, P_MIGRATE, P_FAST_DUMP, P_RECOVERY, P_RUG
      logistic regression (standardized), probability outputs
  E_EXECUTABLE_RETURN, E_MAX_DRAWDOWN
      ridge regression; the return target is the EXECUTABLE return (fees,
      impact, latency, fixed costs), not the chart return
  P_MANIPULATION
      not trained: there is no ground-truth label (reported as such)

Validation: time-based split (oldest 75 % train, newest 25 % holdout), with
training rows whose labels only became known after the holdout starts
purged. Metrics: ROC-AUC, PR-AUC, Brier, calibration bins, precision@K and
per segment (stage, engine, data regime); MAE vs a mean baseline for the
regressions.

SHADOW means: registered with status "shadow" under shadow_* names, scored
onto ledger rows (ml_shadow) for review only. Nothing in the decision
engine, the gate or the execution path loads these models; they cannot be
promoted by the challenger flow (it requires status "challenger"), and no
model output changes a position size.
"""

from __future__ import annotations

import io
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import joblib
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ModelVersion, OpportunityOutcome
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.opportunity_features import FEATURE_NAMES, FEATURE_VERSION, features

log = get_logger("ml.shadow")

BINARY_TARGETS = {"P_UPSIDE_50": "upside_50", "P_UPSIDE_100": "upside_100", "P_UPSIDE_200": "upside_200",
                  "P_MIGRATE": "migrate_60m", "P_FAST_DUMP": "fast_dump", "P_RECOVERY": "recovery", "P_RUG": "rug_60m"}
REGRESSION_TARGETS = {"E_EXECUTABLE_RETURN": "executable_return_primary_pct", "E_MAX_DRAWDOWN": "max_drawdown_pct"}
NOT_TRAINED = {"P_MANIPULATION": "no ground-truth label exists; the manipulation score at the decision is a feature"}
MIN_ROWS = 200
MIN_POSITIVES_TRAIN = 20
MIN_POSITIVES_HOLDOUT = 5
HOLDOUT_FRACTION = 0.25
CALIBRATION_BINS = 5
SCORE_LOOKBACK = timedelta(hours=6)
SHADOW = "shadow"


def model_name(target: str) -> str:
    return f"shadow_{target.lower()}"


class Sample:
    __slots__ = ("decided_at", "available_at", "x", "labels", "segment")

    def __init__(self, decided_at, available_at, x, labels, segment):
        self.decided_at, self.available_at, self.x, self.labels, self.segment = decided_at, available_at, x, labels, segment


def sample(row: OpportunityOutcome) -> Sample | None:
    labels = row.labels or {}
    if not labels or "unknown" in labels:
        return None
    try:
        available = datetime.fromisoformat(labels["available_at"])
    except (KeyError, ValueError):
        return None
    x = features(row.snapshot or {}, row.decided_at, row.engine, row.stage)
    seg = {"stage": row.stage, "engine": row.engine, "data_regime": (row.regime or {}).get("data_regime") or "unknown"}
    return Sample(row.decided_at, available, x, labels, seg)


def time_split(samples: list[Sample], holdout_fraction: float = HOLDOUT_FRACTION) -> tuple[list[Sample], list[Sample], dict]:
    """Oldest part trains, newest part is held out; training rows whose
    labels were not yet known when the holdout starts are purged."""
    samples = sorted(samples, key=lambda s: s.decided_at)
    cut = len(samples) - max(1, int(len(samples) * holdout_fraction))
    hold = samples[cut:]
    start = hold[0].decided_at
    train = [s for s in samples[:cut] if s.available_at <= start]
    return train, hold, {"holdout_start": start.isoformat(), "purged": cut - len(train), "train": len(train), "holdout": len(hold)}


def medians(train: list[Sample], names: tuple[str, ...] = FEATURE_NAMES) -> dict[str, float]:
    out = {}
    for n in names:
        vals = [s.x[n] for s in train if s.x.get(n) is not None]
        out[n] = statistics.median(vals) if vals else 0.0  # all-missing: the __missing indicator carries it
    return out


def matrix(samples: list[Sample], med: dict[str, float], names: tuple[str, ...] = FEATURE_NAMES) -> list[list[float]]:
    return [[s.x[n] if s.x.get(n) is not None else med[n] for n in names] for s in samples]


def binary_metrics(y: list[int], p: list[float], segments: list[dict] | None = None) -> dict[str, Any]:
    pos = sum(y)
    out: dict[str, Any] = {"n": len(y), "positives": pos, "base_rate": round(pos / len(y), 4) if y else None}
    if not y or pos == 0 or pos == len(y):
        out["note"] = "single class in the holdout: ranking metrics undefined"
        return out
    out["roc_auc"] = round(roc_auc_score(y, p), 4)
    out["pr_auc"] = round(average_precision_score(y, p), 4)
    out["brier"] = round(brier_score_loss(y, p), 4)
    bins = []
    for i in range(CALIBRATION_BINS):
        lo, hi = i / CALIBRATION_BINS, (i + 1) / CALIBRATION_BINS
        idx = [j for j, v in enumerate(p) if lo <= v < hi or (i == CALIBRATION_BINS - 1 and v == 1.0)]
        if idx:
            bins.append({"range": [lo, hi], "n": len(idx), "mean_predicted": round(statistics.fmean(p[j] for j in idx), 4),
                         "observed_rate": round(statistics.fmean(y[j] for j in idx), 4)})
    out["calibration"] = bins
    order = sorted(range(len(p)), key=lambda j: p[j], reverse=True)
    for k in (10, max(1, len(p) // 10)):
        top = order[:k]
        out[f"precision_at_{k}"] = round(sum(y[j] for j in top) / len(top), 4)
    if segments:
        seg_out: dict[str, Any] = {}
        for key in segments[0]:  # Solana: stage, engine, data_regime; EVM: category, chain, launchpad
            for val in sorted({s[key] for s in segments}):
                idx = [j for j, s in enumerate(segments) if s[key] == val]
                ys, ps = [y[j] for j in idx], [p[j] for j in idx]
                entry: dict[str, Any] = {"n": len(idx), "positives": sum(ys)}
                if 0 < sum(ys) < len(ys):
                    entry["roc_auc"] = round(roc_auc_score(ys, ps), 4)
                    entry["pr_auc"] = round(average_precision_score(ys, ps), 4)
                seg_out[f"{key}={val}"] = entry
        out["segments"] = seg_out
    return out


def regression_metrics(y: list[float], pred: list[float], train_mean: float) -> dict[str, Any]:
    if not y:
        return {"n": 0}
    mae = statistics.fmean(abs(a - b) for a, b in zip(y, pred))
    base = statistics.fmean(abs(a - train_mean) for a in y)
    corr = None
    if len(y) > 2 and statistics.pstdev(y) > 0 and statistics.pstdev(pred) > 0:
        corr = round(statistics.correlation(y, pred), 4)
    return {"n": len(y), "mae": round(mae, 4), "baseline_mae_train_mean": round(base, 4),
            "beats_baseline": mae < base, "correlation": corr}


def fit(target: str, train: list[Sample], hold: list[Sample], names: tuple[str, ...] = FEATURE_NAMES,
        binary_targets: dict[str, str] = BINARY_TARGETS, regression_targets: dict[str, str] = REGRESSION_TARGETS,
        ) -> tuple[Any, dict[str, Any]] | tuple[None, dict[str, Any]]:
    med = medians(train, names)
    if target in binary_targets:
        key = binary_targets[target]
        tr = [s for s in train if isinstance(s.labels.get(key), bool)]
        ho = [s for s in hold if isinstance(s.labels.get(key), bool)]
        ytr = [int(s.labels[key]) for s in tr]
        yho = [int(s.labels[key]) for s in ho]
        if sum(ytr) < MIN_POSITIVES_TRAIN or sum(ytr) == len(ytr):
            return None, {"status": "skipped", "reason": f"{sum(ytr)} positives in training (needs {MIN_POSITIVES_TRAIN})"}
        est = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)).fit(matrix(tr, med, names), ytr)
        p = [float(v) for v in est.predict_proba(matrix(ho, med, names))[:, 1]] if ho else []
        m = binary_metrics(yho, p, [s.segment for s in ho])
        if sum(yho) < MIN_POSITIVES_HOLDOUT:
            m["warning"] = f"only {sum(yho)} holdout positives: metrics are anecdotal"
        return est, {"status": "trained", "kind": "binary", "holdout": m, "train_rows": len(tr), "medians": med}
    key = regression_targets[target]
    tr = [s for s in train if isinstance(s.labels.get(key), (int, float)) and not isinstance(s.labels.get(key), bool)]
    ho = [s for s in hold if isinstance(s.labels.get(key), (int, float)) and not isinstance(s.labels.get(key), bool)]
    if len(tr) < MIN_ROWS // 2:
        return None, {"status": "skipped", "reason": f"{len(tr)} rows with this label (needs {MIN_ROWS // 2})"}
    ytr = [float(s.labels[key]) for s in tr]
    est = make_pipeline(StandardScaler(), Ridge(alpha=1.0)).fit(matrix(tr, med, names), ytr)
    pred = [float(v) for v in est.predict(matrix(ho, med, names))] if ho else []
    m = regression_metrics([float(s.labels[key]) for s in ho], pred, statistics.fmean(ytr))
    return est, {"status": "trained", "kind": "regression", "holdout": m, "train_rows": len(tr), "medians": med}


async def train_all(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.status == "COMPLETE",
                                                                   OpportunityOutcome.labels.is_not(None))
                                  .order_by(OpportunityOutcome.decided_at))).scalars().all()
    samples = [s for s in (sample(r) for r in rows) if s is not None]
    out: dict[str, Any] = {"samples": len(samples), "not_trained": NOT_TRAINED}
    if len(samples) < MIN_ROWS:
        out["status"] = f"skipped: {len(samples)} completed, labelled opportunities (needs {MIN_ROWS})"
        return out
    train, hold, split = time_split(samples)
    out["split"] = split
    for target in (*BINARY_TARGETS, *REGRESSION_TARGETS):
        name = model_name(target)
        prev = (await session.execute(select(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == SHADOW)
                                      .order_by(ModelVersion.version.desc()).limit(1))).scalar_one_or_none()
        if prev is not None and (prev.metrics or {}).get("dataset_size") == len(samples):
            out[target] = {"status": "skipped_no_new_samples"}
            continue
        est, metrics = fit(target, train, hold)
        if est is None:
            out[target] = metrics
            continue
        metrics.update({"target": target, "dataset_size": len(samples), "split": split, "feature_version": FEATURE_VERSION,
                        "role": "SHADOW: review only, never used for decisions or sizing"})
        await session.execute(update(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == SHADOW)
                              .values(status="superseded"))
        mv = await registry.register_trained_model(session, name=name, estimator=est, feature_names=list(FEATURE_NAMES),
                                                   training_sample_count=metrics["train_rows"], metrics=metrics, status=SHADOW)
        out[target] = {"status": "registered", "version": mv.version, "holdout": metrics["holdout"]}
    return out


async def score_recent(session: AsyncSession, now: datetime | None = None, limit: int = 500) -> int:
    """Shadow scores on recent ledger rows (decision-time features only)."""
    now = now or datetime.now(timezone.utc)
    models = (await session.execute(select(ModelVersion).where(ModelVersion.status == SHADOW,
                                                               ModelVersion.name.like("shadow_%")))).scalars().all()
    if not models:
        return 0
    loaded = [(m, joblib.load(io.BytesIO(m.artifact))) for m in models]
    rows = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.ml_shadow.is_(None),
                                                                   OpportunityOutcome.decided_at >= now - SCORE_LOOKBACK)
                                  .order_by(OpportunityOutcome.decided_at.desc()).limit(limit))).scalars().all()
    for row in rows:
        x = features(row.snapshot or {}, row.decided_at, row.engine, row.stage)
        scores: dict[str, Any] = {}
        for m, est in loaded:
            med = (m.metrics or {}).get("medians") or {}
            vec = [[x[n] if x.get(n) is not None else med.get(n, 0.0) for n in m.feature_names]]
            target = (m.metrics or {}).get("target") or m.name
            if (m.metrics or {}).get("kind") == "binary":
                value = float(est.predict_proba(vec)[0, 1])
            else:
                value = float(est.predict(vec)[0])
            if math.isfinite(value):
                scores[target] = {"value": round(value, 4), "model": m.name, "version": m.version}
        row.ml_shadow = {"scored_at": now.isoformat(), "feature_version": FEATURE_VERSION, "scores": scores,
                         "not_trained": NOT_TRAINED, "note": "SHADOW: never used for decisions or position sizing"}
    return len(rows)


async def run_shadow_cycle(session_factory) -> dict[str, Any]:
    async with session_factory() as session:
        trained = await train_all(session)
        await session.commit()
    async with session_factory() as session:
        scored = await score_recent(session)
        await session.commit()
    def brief(v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        if "status" in v:  # a target: its status and, when skipped, why
            return f"{v['status']}: {v['reason']}" if v.get("reason") else v["status"]
        return v  # the split summary (train / holdout / purged / holdout_start)

    return {"training": {k: brief(v) for k, v in trained.items() if k != "not_trained"}, "scored": scored}
