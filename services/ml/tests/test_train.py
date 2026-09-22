from sqlalchemy import select

from app import train as train_module
from app.dataset import FEATURE_NAMES
from app.train import MIN_TRAINING_SAMPLES, train_and_maybe_register
from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion
from yonixalpha_core.ml import registry


def _row(label: int, i: int) -> MLFeatureSnapshot:
    # A cleanly linearly-separable synthetic pattern (accelerating tokens
    # -> label 1, not -> label 0), with small per-row jitter so rows in
    # train/test splits aren't bit-identical duplicates of each other.
    if label == 1:
        features = {
            "token_age_seconds": 100.0 + i,
            "tx_count_current_window": 10.0 + i * 0.01,
            "tx_count_prior_window": 2.0,
            "tx_acceleration_ratio": 5.0 + i * 0.01,
            "has_acceleration_ratio": 1.0,
        }
    else:
        features = {
            "token_age_seconds": 100.0 + i,
            "tx_count_current_window": 1.0,
            "tx_count_prior_window": 10.0 + i * 0.01,
            "tx_acceleration_ratio": 0.1,
            "has_acceleration_ratio": 1.0,
        }
    return MLFeatureSnapshot(symbol="TEST", features=features, label=label)


async def _seed_separable_dataset(db_session, count_per_class: int = 30) -> None:
    for i in range(count_per_class):
        db_session.add(_row(1, i))
        db_session.add(_row(0, i))
    await db_session.commit()


async def test_skips_when_below_min_training_samples(db_session):
    for i in range(MIN_TRAINING_SAMPLES - 1):
        db_session.add(_row(i % 2, i))
    await db_session.commit()

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "skipped_insufficient_samples"
    assert outcome.model_version is None
    rows = (await db_session.execute(select(ModelVersion))).scalars().all()
    assert rows == []


async def test_skips_when_only_one_class_present(db_session):
    for i in range(MIN_TRAINING_SAMPLES + 5):
        db_session.add(_row(1, i))
    await db_session.commit()

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "skipped_single_class"
    rows = (await db_session.execute(select(ModelVersion))).scalars().all()
    assert rows == []


async def test_separable_dataset_trains_and_activates(db_session):
    await _seed_separable_dataset(db_session, count_per_class=30)

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "activated"
    assert outcome.model_version is not None
    assert outcome.model_version.status == "active"
    assert outcome.model_version.training_sample_count == 60
    assert set(outcome.model_version.feature_names) == set(FEATURE_NAMES)
    assert outcome.metrics["holdout_auc"] > train_module.MIN_ACTIVATION_AUC

    model = await registry.get_active_model(db_session, train_module.MODEL_NAME)
    prediction = model.predict({name: 5.0 for name in FEATURE_NAMES} | {"tx_acceleration_ratio": 8.0})
    assert 0.0 <= prediction.score <= 1.0


async def test_registers_but_does_not_activate_below_threshold(db_session, monkeypatch):
    monkeypatch.setattr(train_module, "MIN_ACTIVATION_AUC", 2.0)  # unreachable -> forces the non-activation branch
    await _seed_separable_dataset(db_session, count_per_class=30)

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "registered"
    assert outcome.model_version.status == "trained"
    assert outcome.model_version.activated_at is None

    model = await registry.get_active_model(db_session, train_module.MODEL_NAME)
    from yonixalpha_core.ml.model import NullModel

    assert isinstance(model, NullModel)  # registered, but never promoted -> still no active model


async def test_second_better_training_run_retires_the_first(db_session):
    await _seed_separable_dataset(db_session, count_per_class=30)
    first = await train_and_maybe_register(db_session)
    assert first.status == "activated"

    # A second, larger round of the same clearly-separable pattern trains
    # and activates again — real behavior, not mocked: this exercises
    # registry.activate_model's demotion of whatever was active before.
    await _seed_separable_dataset(db_session, count_per_class=30)
    second = await train_and_maybe_register(db_session)
    assert second.status == "activated"
    assert second.model_version.version == first.model_version.version + 1

    await db_session.refresh(first.model_version)
    assert first.model_version.status == "retired"
    assert second.model_version.status == "active"
