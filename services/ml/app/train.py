from dataclasses import dataclass

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ModelVersion
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import registry

from app.dataset import FEATURE_NAMES, LabeledDataset, load_labeled_dataset

log = get_logger("ml.train")

# Must match services/decision-engine/app/evaluate.py's MODEL_NAME — that
# module predicts with the champion an operator promoted under this name
# (this job only registers challengers, master §39).
MODEL_NAME = "solana_candidate_momentum"

# Conservative gates for a system that has produced exactly zero labeled
# outcomes so far (see docs/ML.md): nothing in this codebase has ever
# closed a real or paper position, so ml_features.label is always NULL
# today and this job's dataset is empty. These thresholds are what keep it
# from ever training — let alone activating — a model on a handful of
# accidental rows once labels do start appearing.
#
# The gate counts distinct CANDIDATES, not rows. decision-engine writes one
# row per candidate per 15s cycle and paper-trading labels them all
# identically, so a row count says more about how long something was
# watched than about how much independent evidence exists: 50 rows can be
# two candidates. Independent outcomes are what bound what can be learned.
MIN_TRAINING_CANDIDATES = 50
MIN_ACTIVATION_AUC = 0.55
HOLDOUT_FRACTION = 0.2

# A point-estimate AUC from a small holdout is mostly noise. Measured: with
# grouping fixed but only this 0.55 point threshold, a model trained on
# pure coin-flip labels still activated in ~42% of runs, and that rate did
# NOT improve with more data (42% at 50 candidates, 45% at 100, 42% at
# 200) because the holdout grows proportionally while the threshold stays
# put. Training runs hourly, so "unlikely per run" becomes "certain by
# tomorrow". Activation therefore also requires the AUC to be
# statistically distinguishable from chance, not merely above a number.
MIN_HOLDOUT_CANDIDATES = 10
# One-sided 95% normal quantile, applied to the Hanley-McNeil standard
# error of the AUC.
AUC_CONFIDENCE_Z = 1.645


def _auc_standard_error(auc: float, n_positive: int, n_negative: int) -> float:
    """Hanley & McNeil (1982) standard error of the AUC.

    n_positive/n_negative must be counts of INDEPENDENT observations. Rows
    are not independent here — every row of a candidate carries that
    candidate's single outcome — so callers pass candidate counts, not row
    counts. Using rows would shrink the error bar by the observation
    frequency and re-introduce, as false confidence, the same problem
    grouping the split just removed.
    """
    q1 = auc / (2 - auc)
    q2 = 2 * auc**2 / (1 + auc)
    numerator = (
        auc * (1 - auc) + (n_positive - 1) * (q1 - auc**2) + (n_negative - 1) * (q2 - auc**2)
    )
    return (max(numerator, 0.0) / (n_positive * n_negative)) ** 0.5


@dataclass
class TrainingOutcome:
    status: str  # skipped_insufficient_samples | skipped_single_class | registered | challenger_ready
    available_samples: int
    model_version: ModelVersion | None = None
    metrics: dict | None = None


def _temporal_group_split(dataset: LabeledDataset, holdout_fraction: float) -> tuple[list[int], list[int]]:
    """Split row indices so that (a) every row of a candidate lands wholly
    on one side, and (b) the holdout is strictly LATER than the training
    set.

    (a) stops a model scoring near-duplicate siblings of its own training
    rows — the leak that measured AUC 0.73 on pure noise before this
    existed. (b) makes the holdout answer the only question worth asking
    of a trading model: does what it learned from the past hold up on data
    it has not seen yet? A shuffled split answers a question nobody is
    ever in a position to act on.
    """
    ordered_groups = sorted(dataset.group_started_at, key=lambda g: dataset.group_started_at[g])
    holdout_size = max(1, int(round(len(ordered_groups) * holdout_fraction)))
    holdout_groups = set(ordered_groups[-holdout_size:])

    train_idx = [i for i, g in enumerate(dataset.groups) if g not in holdout_groups]
    test_idx = [i for i, g in enumerate(dataset.groups) if g in holdout_groups]
    return train_idx, test_idx


async def train_and_maybe_register(session: AsyncSession) -> TrainingOutcome:
    """The one entry point the periodic loop (app/main.py) and tests both
    call. Trains nothing and registers nothing unless there's a real,
    sufficiently large, genuinely two-class labeled dataset to learn from
    — the honest default is `skipped_insufficient_samples`, and per
    docs/ML.md that is expected to be the outcome of every run for a long
    time.
    """
    dataset = await load_labeled_dataset(session)
    features, labels = dataset.features, dataset.labels
    if dataset.skipped_rows:
        log.warning("train.skipped_rows_missing_features", count=dataset.skipped_rows)

    if dataset.candidate_count < MIN_TRAINING_CANDIDATES:
        log.info(
            "train.skipped_insufficient_samples",
            available_candidates=dataset.candidate_count,
            available_rows=len(features),
            required_candidates=MIN_TRAINING_CANDIDATES,
        )
        return TrainingOutcome(status="skipped_insufficient_samples", available_samples=len(features))

    if len(set(labels)) < 2:
        log.info("train.skipped_single_class", available=len(features))
        return TrainingOutcome(status="skipped_single_class", available_samples=len(features))

    train_idx, test_idx = _temporal_group_split(dataset, HOLDOUT_FRACTION)
    X_train = [features[i] for i in train_idx]
    y_train = [labels[i] for i in train_idx]
    X_test = [features[i] for i in test_idx]
    y_test = [labels[i] for i in test_idx]

    # A forward-in-time split cannot be stratified, so either side may turn
    # out single-class. Training on one class learns nothing; scoring
    # against one class makes AUC undefined. Skip rather than register a
    # model whose headline metric would be meaningless.
    if len(set(y_train)) < 2:
        log.info("train.skipped_single_class", available=len(features), side="train")
        return TrainingOutcome(status="skipped_single_class", available_samples=len(features))

    estimator = LogisticRegression(max_iter=1000)
    estimator.fit(X_train, y_train)

    probabilities = estimator.predict_proba(X_test)[:, 1]
    predictions = estimator.predict(X_test)

    # Score per CANDIDATE, not per row. A candidate contributes one outcome
    # and many near-identical rows; scoring rows would count the same
    # evidence over and over and make the holdout look far larger (and the
    # AUC far more certain) than it is. One averaged probability per
    # candidate is both the independent unit and the unit decisions are
    # actually made on.
    per_candidate: dict[str, list[float]] = {}
    per_candidate_label: dict[str, int] = {}
    for position, row_index in enumerate(test_idx):
        group = dataset.groups[row_index]
        per_candidate.setdefault(group, []).append(probabilities[position])
        per_candidate_label[group] = labels[row_index]

    candidate_scores = [sum(v) / len(v) for v in per_candidate.values()]
    candidate_labels = [per_candidate_label[g] for g in per_candidate]
    n_positive = sum(candidate_labels)
    n_negative = len(candidate_labels) - n_positive

    holdout_auc = roc_auc_score(candidate_labels, candidate_scores) if n_positive and n_negative else None
    auc_lower_bound = (
        holdout_auc - AUC_CONFIDENCE_Z * _auc_standard_error(holdout_auc, n_positive, n_negative)
        if holdout_auc is not None
        else None
    )

    metrics = {
        "holdout_auc": holdout_auc,
        "holdout_auc_lower_bound": auc_lower_bound,
        "holdout_accuracy": accuracy_score(y_test, predictions),
        "holdout_size": len(X_test),
        "holdout_candidates": len(per_candidate),
        "holdout_positive_candidates": n_positive,
        "holdout_negative_candidates": n_negative,
        "training_candidates": len({dataset.groups[i] for i in train_idx}),
        "split": "temporal_grouped_by_candidate",
        "scored_per": "candidate",
        "dataset_end": dataset.dataset_end.isoformat() if dataset.dataset_end else None,
        "frozen_excluded": dataset.frozen_excluded,
    }

    model_version = await registry.register_trained_model(
        session,
        name=MODEL_NAME,
        estimator=estimator,
        feature_names=FEATURE_NAMES,
        training_sample_count=len(features),
        metrics=metrics,
    )

    # Three independent conditions, all required. The point estimate alone
    # is not evidence: see MIN_HOLDOUT_CANDIDATES' comment for the measured
    # false-activation rate when it was.
    activatable = (
        holdout_auc is not None
        and len(per_candidate) >= MIN_HOLDOUT_CANDIDATES
        and holdout_auc >= MIN_ACTIVATION_AUC
        and auc_lower_bound is not None
        and auc_lower_bound > 0.5
    )
    if activatable:
        # Master §39: a model that clears the bar is never put into production by
        # this job. It becomes a promotable challenger; only an operator promotes
        # it (registry.promote_challenger, audited), and its weight in decisions
        # is the contribution % the operator sets after validation (ml.governance).
        current = await registry.get_active_model_row(session, MODEL_NAME)
        current_auc = (current.metrics or {}).get("holdout_auc") if current is not None else None
        if current_auc is None or holdout_auc >= current_auc:
            # only the newest challenger is a candidate for promotion (kept, never deleted)
            await session.execute(update(ModelVersion).where(ModelVersion.name == MODEL_NAME,
                                                             ModelVersion.status == "challenger")
                                  .values(status="superseded"))
            model_version.status = "challenger"
            model_version.metrics = {**metrics, "promotable": True,
                                     "promotion": "operator only (ML Review); never automatic"}
            await session.commit()
            log.info(
                "train.challenger_ready",
                version=model_version.version,
                holdout_auc=holdout_auc,
                holdout_auc_lower_bound=auc_lower_bound,
                holdout_candidates=len(per_candidate),
            )
            return TrainingOutcome(status="challenger_ready", available_samples=len(features),
                                   model_version=model_version, metrics=model_version.metrics)

    await session.commit()
    log.info("train.registered_not_activated", version=model_version.version, holdout_auc=holdout_auc)
    return TrainingOutcome(status="registered", available_samples=len(features), model_version=model_version, metrics=metrics)
