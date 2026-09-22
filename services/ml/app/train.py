from dataclasses import dataclass

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ModelVersion
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import registry

from app.dataset import FEATURE_NAMES, load_labeled_dataset

log = get_logger("ml.train")

# Must match services/decision-engine/app/evaluate.py's MODEL_NAME — that
# module is what actually loads and predicts against whatever this job
# activates under this name.
MODEL_NAME = "solana_candidate_momentum"

# Conservative gates for a system that has produced exactly zero labeled
# outcomes so far (see docs/ML.md): nothing in this codebase has ever
# closed a real or paper position, so ml_features.label is always NULL
# today and this job's dataset is empty. These thresholds are what keep it
# from ever training — let alone activating — a model on a handful of
# accidental rows once labels do start appearing.
MIN_TRAINING_SAMPLES = 50
MIN_ACTIVATION_AUC = 0.55
HOLDOUT_FRACTION = 0.2


@dataclass
class TrainingOutcome:
    status: str  # skipped_insufficient_samples | skipped_single_class | registered | activated
    available_samples: int
    model_version: ModelVersion | None = None
    metrics: dict | None = None


async def train_and_maybe_register(session: AsyncSession) -> TrainingOutcome:
    """The one entry point the periodic loop (app/main.py) and tests both
    call. Trains nothing and registers nothing unless there's a real,
    sufficiently large, genuinely two-class labeled dataset to learn from
    — the honest default is `skipped_insufficient_samples`, and per
    docs/ML.md that is expected to be the outcome of every run for a long
    time.
    """
    features, labels, skipped_rows = await load_labeled_dataset(session)
    if skipped_rows:
        log.warning("train.skipped_rows_missing_features", count=skipped_rows)

    if len(features) < MIN_TRAINING_SAMPLES:
        log.info("train.skipped_insufficient_samples", available=len(features), required=MIN_TRAINING_SAMPLES)
        return TrainingOutcome(status="skipped_insufficient_samples", available_samples=len(features))

    if len(set(labels)) < 2:
        log.info("train.skipped_single_class", available=len(features))
        return TrainingOutcome(status="skipped_single_class", available_samples=len(features))

    X_train, X_test, y_train, y_test = train_test_split(
        features, labels, test_size=HOLDOUT_FRACTION, stratify=labels, random_state=42
    )

    estimator = LogisticRegression(max_iter=1000)
    estimator.fit(X_train, y_train)

    probabilities = estimator.predict_proba(X_test)[:, 1]
    predictions = estimator.predict(X_test)
    holdout_auc = roc_auc_score(y_test, probabilities) if len(set(y_test)) > 1 else None
    metrics = {
        "holdout_auc": holdout_auc,
        "holdout_accuracy": accuracy_score(y_test, predictions),
        "holdout_size": len(X_test),
    }

    model_version = await registry.register_trained_model(
        session,
        name=MODEL_NAME,
        estimator=estimator,
        feature_names=FEATURE_NAMES,
        training_sample_count=len(features),
        metrics=metrics,
    )

    if holdout_auc is not None and holdout_auc >= MIN_ACTIVATION_AUC:
        current = await registry.get_active_model_row(session, MODEL_NAME)
        current_auc = (current.metrics or {}).get("holdout_auc") if current is not None else None
        if current_auc is None or holdout_auc >= current_auc:
            await registry.activate_model(session, model_version)
            await session.commit()
            log.info("train.activated", version=model_version.version, holdout_auc=holdout_auc)
            return TrainingOutcome(status="activated", available_samples=len(features), model_version=model_version, metrics=metrics)

    await session.commit()
    log.info("train.registered_not_activated", version=model_version.version, holdout_auc=holdout_auc)
    return TrainingOutcome(status="registered", available_samples=len(features), model_version=model_version, metrics=metrics)
