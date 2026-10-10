"""Entry-timing model (SHADOW): does a recorded entry signal win after
costs?

Samples: labelled entry_signals of the three entry strategies (decision-time
features only; the label is the executable return of the reference trade,
entry_outcomes). Target: executable return > 0.

Validation, all chronological (entry_eval):
  - the frozen test period (platform setting entry_eval_frozen) is set once
    enough signals are labelled and never moves;
  - the model is fitted on the train split, its regularisation chosen on the
    validation split, and scored ONCE on the frozen test split;
  - baselines on the same test split: the strategy's own score used as the
    probability, and the base rate (always predicting the train win rate).

The model is registered as a new ModelVersion "entry_timing" with status
"shadow" (older versions are kept). Its standardized logistic coefficients
are exported in the metrics so the discovery service can score new signals
in pure Python (entry_intel.logistic_predict) and RECORD the probability.
Nothing activates it: it cannot change a decision, a size or an order.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import entry_eval, entry_intel as ei
from yonixalpha_core.db.models import EntrySignal, PlatformSetting
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import registry

log = get_logger("ml.entry")

MODEL_NAME = "entry_timing"
MIN_TRAIN = 200
MIN_TEST = 30
MIN_POSITIVES = 10
C_GRID = (0.1, 1.0)


def _matrix(rows: list[dict], medians: list[float]) -> list[list[float]]:
    return [[m if v is None else v for v, m in zip(r["x"], medians)] for r in rows]


def _medians(rows: list[dict]) -> list[float]:
    cols = list(zip(*[r["x"] for r in rows])) if rows else []
    out = []
    for c in cols:
        vals = [v for v in c if v is not None]
        out.append(float(statistics.median(vals)) if vals else 0.0)
    return out


async def ensure_frozen(session: AsyncSession) -> dict | None:
    """Freezes the test period once (entry_eval.freeze_window); returns it."""
    row = await session.get(PlatformSetting, entry_eval.FREEZE_SETTING)
    if row is not None:
        return row.value
    times = (await session.execute(select(EntrySignal.decided_at).where(EntrySignal.outcome_at.is_not(None))
                                   .order_by(EntrySignal.decided_at))).scalars().all()
    window = entry_eval.freeze_window(list(times))
    if window is None:
        return None
    window["frozen_at"] = datetime.now(timezone.utc).isoformat()
    session.add(PlatformSetting(key=entry_eval.FREEZE_SETTING, value=window))
    await session.commit()
    log.info("entry_eval.frozen", **window)
    return window


async def run_entry_cycle(session: AsyncSession) -> dict[str, Any]:
    frozen = await ensure_frozen(session)
    if frozen is None:
        return {"status": "INSUFFICIENT_DATA", "reason": f"the test period is frozen once {entry_eval.MIN_FREEZE_TOTAL} "
                                                         "signals are labelled"}
    rows_db = (await session.execute(select(EntrySignal.decided_at, EntrySignal.features, EntrySignal.score,
                                            EntrySignal.outcome).where(
        EntrySignal.strategy.in_(ei.STRATEGIES), EntrySignal.outcome_at.is_not(None)).order_by(EntrySignal.decided_at))).all()
    rows = []
    for at, feats, score, outcome in rows_db:
        ex = (outcome or {}).get("executable_return_pct")
        if ex is None or not feats:
            continue
        rows.append({"decided_at": at, "x": ei.model_vector(feats, float(score) if score is not None else None),
                     "y": 1 if float(ex) > 0 else 0, "score": float(score) if score is not None else None})
    parts = entry_eval.split(rows, frozen)
    tr, va, te = parts["train"], parts["validation"], parts["test"]
    if len(tr) < MIN_TRAIN or len(te) < MIN_TEST or sum(r["y"] for r in tr) < MIN_POSITIVES:
        return {"status": "LEARNING", "train": len(tr), "validation": len(va), "test": len(te),
                "reason": f"need >= {MIN_TRAIN} train (>= {MIN_POSITIVES} wins) and >= {MIN_TEST} frozen-test signals"}
    if len({r["y"] for r in tr}) < 2 or len({r["y"] for r in te}) < 2:
        return {"status": "LEARNING", "reason": "train or test has a single class"}
    med = _medians(tr)
    xtr, ytr = _matrix(tr, med), [r["y"] for r in tr]
    best = None
    for c in C_GRID:
        est = make_pipeline(StandardScaler(), LogisticRegression(C=c, max_iter=2000)).fit(xtr, ytr)
        if va and len({r["y"] for r in va}) == 2:
            auc = roc_auc_score([r["y"] for r in va], est.predict_proba(_matrix(va, med))[:, 1])
        else:
            auc = 0.0
        if best is None or auc > best[0]:
            best = (auc, c, est)
    val_auc, c, est = best
    yte = [r["y"] for r in te]
    pte = est.predict_proba(_matrix(te, med))[:, 1]
    base_rate = sum(ytr) / len(ytr)
    scored = [(r["score"], r["y"]) for r in te if r["score"] is not None]
    score_auc = roc_auc_score([y for _, y in scored], [s for s, _ in scored]) if len({y for _, y in scored}) == 2 else None
    metrics = {
        "frozen": frozen, "train": len(tr), "validation": len(va), "test": len(te), "C": c,
        "validation_auc": round(float(val_auc), 4), "test_auc": round(float(roc_auc_score(yte, pte)), 4),
        "test_brier": round(float(brier_score_loss(yte, pte)), 5),
        "baseline_base_rate_brier": round(float(brier_score_loss(yte, [base_rate] * len(yte))), 5),
        "baseline_strategy_score_auc": round(float(score_auc), 4) if score_auc is not None else None,
        "test_win_rate": round(sum(yte) / len(yte), 4),
        "label": "executable return of the reference trade > 0 (entry_outcomes)", "status_note": "SHADOW: never activated",
    }
    scaler, lr = est.named_steps["standardscaler"], est.named_steps["logisticregression"]
    metrics["coefficients"] = {"features": list(ei.MODEL_FEATURES), "medians": med,
                               "means": [float(v) for v in scaler.mean_], "scales": [float(v) for v in scaler.scale_],
                               "weights": [float(v) for v in lr.coef_[0]], "intercept": float(lr.intercept_[0])}
    beats = bool(metrics["test_brier"] < metrics["baseline_base_rate_brier"]
             and (score_auc is None or metrics["test_auc"] > score_auc))
    metrics["beats_baselines_on_frozen_test"] = beats
    mv = await registry.register_trained_model(session, name=MODEL_NAME, estimator=est, feature_names=list(ei.MODEL_FEATURES),
                                               training_sample_count=len(tr), metrics=metrics, status="shadow")
    await session.commit()
    return {"status": "REGISTERED_SHADOW", "version": mv.version, "test_auc": metrics["test_auc"],
            "beats_baselines": beats}
