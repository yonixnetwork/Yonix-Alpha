"""ML readiness: one explicit lifecycle state per decision model, and whether
(and how) ML is allowed to touch a decision right now.

    INSUFFICIENT_DATA   fewer clean, labelled samples than training needs
    LEARNING            enough samples, no challenger trained yet
    VALIDATING          a challenger exists but did not clear the statistical
                        bar (AUC >= 0.55 with a 95% lower bound > 0.5, beats
                        the champion) on a forward-in-time holdout
    SHADOW              a promotable challenger awaits the operator, or the
                        champion is ignored because drift was detected, or it
                        has not yet been checked on forward outcomes
    PAPER_VALIDATED     an operator-promoted champion whose accuracy on
                        recent (forward) outcomes has been measured and has
                        not degraded
    PRODUCTION_CONTRIBUTOR  PAPER_VALIDATED and a minimum ML confidence is
                        set, so the gate uses it

Contribution is defined by the gate itself (safety/gate.py): a champion can
only make the gate WAIT when its confidence is below min_ml_confidence. It
never approves, sizes, or overrides a safety rule. So "contributing" is
true only in PRODUCTION_CONTRIBUTOR, and its effect is at most a WAIT.

Nothing here trains, promotes or changes anything: it reads the model
registry, the samples and the drift flag and reports.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion, OpportunityOutcome
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, ENGINES_FOR_MODEL, FEATURE_VERSION

# Same bars as services/ml (gate_ml.MIN_SAMPLES, train.MIN_ACTIVATION_AUC).
MIN_LABELED_SAMPLES = 50
MIN_AUC = 0.55
MIN_FORWARD_SAMPLES = 10  # labelled outcomes the drift check needs to measure accuracy

# Last scanner-intelligence feature ablation (services/ml/app/ablation.py).
ABLATION_KEY = "yx:ml:ablation:last"

STATES = ("INSUFFICIENT_DATA", "LEARNING", "VALIDATING", "SHADOW", "PAPER_VALIDATED", "PRODUCTION_CONTRIBUTOR")


def classify(*, clean_labeled: int, challenger: dict | None, champion: dict | None, drift_flag: bool,
             min_ml_confidence: float | None) -> dict[str, Any]:
    """State, whether ML contributes, and the reason in plain words.
    challenger / champion: {"version", "metrics"} or None."""
    def out(state: str, reason: str, contributing: bool = False) -> dict[str, Any]:
        return {"state": state, "contributing": contributing, "reason": reason,
                "effect": ("may make the gate WAIT when confidence < min_ml_confidence; never approves, sizes or "
                           "overrides safety") if contributing else "none: decisions are rules only"}

    if champion is not None:
        drift = (champion.get("metrics") or {}).get("drift") or {}
        if drift_flag or drift.get("status") == "MODEL_DRIFT_DETECTED":
            return out("SHADOW", f"champion v{champion['version']} is ignored: drift detected "
                                 f"(worst PSI / accuracy drop over threshold)")
        recent = drift.get("recent_accuracy")
        drop = drift.get("accuracy_drop")
        if recent is None:
            return out("SHADOW", f"champion v{champion['version']} not yet measured on forward outcomes "
                                 f"(needs {MIN_FORWARD_SAMPLES}+ labelled recent samples)")
        if drop is not None and drop > 0.15:
            return out("SHADOW", f"champion v{champion['version']} forward accuracy fell {drop:.0%}")
        if min_ml_confidence is None:
            return out("PAPER_VALIDATED", f"champion v{champion['version']} holds up on forward outcomes "
                                          f"(accuracy {recent:.0%}); not used: min_ml_confidence is not set")
        return out("PRODUCTION_CONTRIBUTOR", f"champion v{champion['version']} validated forward (accuracy {recent:.0%}); "
                                             f"gate WAITs below confidence {min_ml_confidence}", contributing=True)
    if clean_labeled < MIN_LABELED_SAMPLES:
        return out("INSUFFICIENT_DATA", f"{clean_labeled} clean labelled samples; training needs {MIN_LABELED_SAMPLES} "
                                        "(a sample is labelled when its trade closes)")
    if challenger is None:
        return out("LEARNING", f"{clean_labeled} clean labelled samples; the next hourly training run builds a challenger")
    m = (challenger.get("metrics") or {})
    c = m.get("challenger") or {}
    if not m.get("promotable"):
        auc, lb = c.get("auc"), c.get("auc_lower_bound")
        return out("VALIDATING", f"challenger v{challenger['version']} not promotable: holdout AUC "
                                 f"{'n/a' if auc is None else f'{auc:.3f}'} (lower bound "
                                 f"{'n/a' if lb is None else f'{lb:.3f}'}; needs >= {MIN_AUC} and > 0.5)")
    return out("SHADOW", f"challenger v{challenger['version']} passed the holdout bar; waiting for operator promotion "
                         "(ML Review)")


def _brief(m: ModelVersion | None) -> dict | None:
    if m is None:
        return None
    metrics = {k: v for k, v in (m.metrics or {}).items() if k not in ("reference",)}
    return {"version": m.version, "status": m.status, "trained_at": m.trained_at.isoformat() if m.trained_at else None,
            "activated_at": m.activated_at.isoformat() if m.activated_at else None,
            "training_samples": m.training_sample_count, "metrics": metrics}


async def model_readiness(session: AsyncSession, redis, min_confidence_for: dict[str, float | None]) -> list[dict[str, Any]]:
    """One entry per gate model. `min_confidence_for`: engine -> the
    effective min_ml_confidence setting of that engine."""
    out = []
    for name, engines in ENGINES_FOR_MODEL.items():
        champion = await registry.get_active_model_row(session, name)
        challenger = (await session.execute(select(ModelVersion).where(
            ModelVersion.name == name, ModelVersion.status == "challenger")
            .order_by(ModelVersion.version.desc()).limit(1))).scalar_one_or_none()
        total, labeled, clean, quarantined, first, last = (await session.execute(select(
            func.count(), func.count().filter(MLFeatureSnapshot.label.is_not(None)),
            func.count().filter(MLFeatureSnapshot.quality_status == "ok", MLFeatureSnapshot.label.is_not(None)),
            func.count().filter(MLFeatureSnapshot.quality_status == "quarantined"),
            func.min(MLFeatureSnapshot.created_at), func.max(MLFeatureSnapshot.created_at),
        ).where(MLFeatureSnapshot.engine.in_(engines), MLFeatureSnapshot.feature_version == FEATURE_VERSION))).one()
        drift_flag = bool(await redis.exists(f"{DRIFT_FLAG_PREFIX}{name}")) if redis is not None else False
        # The gate applies the model per engine; ML contributes if any engine sets a minimum.
        confidences = [min_confidence_for.get(e) for e in engines if min_confidence_for.get(e) is not None]
        champ, chall = _brief(champion), _brief(challenger)
        state = classify(clean_labeled=clean, challenger=chall, champion=champ, drift_flag=drift_flag,
                         min_ml_confidence=min(confidences) if confidences else None)
        cm = ((chall or {}).get("metrics") or {}).get("challenger") or {}
        out.append({"model": name, "engines": engines, "feature_version": FEATURE_VERSION, **state,
                    "samples": {"total": total, "labeled": labeled, "clean_labeled": clean, "quarantined": quarantined,
                                "needed": MIN_LABELED_SAMPLES,
                                "first_at": first.isoformat() if first else None, "last_at": last.isoformat() if last else None},
                    "validation": {"holdout_size": cm.get("holdout_size"), "auc": cm.get("auc"),
                                   "auc_lower_bound": cm.get("auc_lower_bound"), "brier": cm.get("brier"),
                                   "split": ((chall or {}).get("metrics") or {}).get("split"),
                                   "promotable": ((chall or {}).get("metrics") or {}).get("promotable")},
                    "drift": ((champ or {}).get("metrics") or {}).get("drift"), "drift_flag": drift_flag,
                    "champion": champ and {k: v for k, v in champ.items() if k != "metrics"},
                    "challenger": chall and {k: v for k, v in chall.items() if k != "metrics"},
                    "min_ml_confidence": min(confidences) if confidences else None})
    return out


async def dataset_overview(session: AsyncSession) -> dict[str, Any]:
    """What the learning dataset holds: every decision, traded or not."""
    o = OpportunityOutcome
    row = (await session.execute(select(
        func.count(), func.count().filter(o.traded.is_(False)),
        func.count().filter(o.traded.is_(True), o.execution_mode == "PAPER"),
        func.count().filter(o.traded.is_(True), o.execution_mode == "LIVE"),
        func.count().filter(o.snapshot["operator_request"].astext == "true"),
        func.count().filter(o.status == "COMPLETE"), func.count().filter(o.labels.is_not(None)),
        func.count().filter(o.analysis["counterfactual"]["classification"].astext == "MISSED_WIN"),
        func.min(o.decided_at), func.max(o.decided_at)))).one()
    shadow = (await session.execute(select(ModelVersion.name, ModelVersion.version, ModelVersion.trained_at).where(
        ModelVersion.status == "shadow").order_by(ModelVersion.trained_at.desc()))).all()
    return {"decisions": row[0], "rejected_or_not_traded": row[1], "paper_trades": row[2], "live_trades": row[3],
            "manual_requests": row[4], "completed_outcomes": row[5], "labelled": row[6], "missed_winners": row[7],
            "first_decision_at": row[8].isoformat() if row[8] else None,
            "last_decision_at": row[9].isoformat() if row[9] else None,
            "shadow_models": [{"name": n, "version": v, "trained_at": t.isoformat() if t else None} for n, v, t in shadow],
            "last_shadow_training_at": shadow[0][2].isoformat() if shadow and shadow[0][2] else None,
            "note": "shadow models are review-only: they never influence a decision"}
