from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import train as train_module
from app.dataset import FEATURE_NAMES, load_labeled_dataset
from app.train import MIN_TRAINING_CANDIDATES, _temporal_group_split, train_and_maybe_register
from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion, Token, TradingCandidate
from yonixalpha_core.ml import registry

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _make_candidate(db_session, mint: str) -> TradingCandidate:
    token = Token(mint_address=mint, symbol=mint[:8], first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = TradingCandidate(token_id=token.id, engine="discovery", state="discovered")
    db_session.add(candidate)
    await db_session.flush()
    return candidate


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
    for i in range(MIN_TRAINING_CANDIDATES - 1):
        db_session.add(_row(i % 2, i))
    await db_session.commit()

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "skipped_insufficient_samples"
    assert outcome.model_version is None
    rows = (await db_session.execute(select(ModelVersion))).scalars().all()
    assert rows == []


async def test_skips_when_only_one_class_present(db_session):
    for i in range(MIN_TRAINING_CANDIDATES + 5):
        db_session.add(_row(1, i))
    await db_session.commit()

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "skipped_single_class"
    rows = (await db_session.execute(select(ModelVersion))).scalars().all()
    assert rows == []


async def test_separable_dataset_trains_a_challenger_that_only_an_operator_promotes(db_session):
    """Master §39: clearing the bar never puts a model into production by
    itself; it becomes a promotable challenger and waits for the operator."""
    from yonixalpha_core.ml.model import NullModel

    await _seed_separable_dataset(db_session, count_per_class=30)

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "challenger_ready"
    assert outcome.model_version is not None
    assert outcome.model_version.status == "challenger" and outcome.model_version.activated_at is None
    assert outcome.model_version.metrics["promotable"] is True
    assert outcome.model_version.training_sample_count == 60
    assert set(outcome.model_version.feature_names) == set(FEATURE_NAMES)
    assert outcome.metrics["holdout_auc"] > train_module.MIN_ACTIVATION_AUC
    assert isinstance(await registry.get_active_model(db_session, train_module.MODEL_NAME), NullModel)

    await registry.promote_challenger(db_session, outcome.model_version, note="test")  # the operator's action
    await db_session.commit()
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


async def test_promoting_a_second_challenger_retires_the_first_champion(db_session):
    await _seed_separable_dataset(db_session, count_per_class=30)
    first = await train_and_maybe_register(db_session)
    assert first.status == "challenger_ready"
    await registry.promote_challenger(db_session, first.model_version, note="test")
    await db_session.commit()

    # A second, larger round of the same clearly-separable pattern registers
    # a new challenger; the operator promotes it, and registry.activate_model
    # retires the champion before it (history kept, never deleted).
    await _seed_separable_dataset(db_session, count_per_class=30)
    second = await train_and_maybe_register(db_session)
    assert second.status == "challenger_ready"
    assert second.model_version.version == first.model_version.version + 1
    await registry.promote_challenger(db_session, second.model_version, note="test")
    await db_session.commit()

    await db_session.refresh(first.model_version)
    assert first.model_version.status == "retired"
    assert second.model_version.status == "active"


async def test_a_newer_challenger_supersedes_the_unpromoted_one(db_session):
    await _seed_separable_dataset(db_session, count_per_class=30)
    first = await train_and_maybe_register(db_session)
    await _seed_separable_dataset(db_session, count_per_class=30)
    second = await train_and_maybe_register(db_session)
    assert (first.status, second.status) == ("challenger_ready", "challenger_ready")
    await db_session.refresh(first.model_version)
    # only the newest is a candidate for promotion; the older one is kept
    assert first.model_version.status == "superseded" and second.model_version.status == "challenger"
    assert await registry.get_active_model_row(db_session, "solana_candidate_momentum") is None


# ---------------------------------------------------------------------------
# Audit regression: holdout integrity.
#
# decision-engine writes one ml_features row per candidate per 15s cycle,
# and paper-trading stamps the SAME label on all of them. Splitting those
# rows randomly (the pre-audit behaviour) scattered near-identical siblings
# across train and test, letting a model score its own training data. An
# audit probe measured holdout AUC 0.730 on a dataset built from pure coin
# flips — comfortably above MIN_ACTIVATION_AUC, i.e. a worthless model
# would have been activated and allowed to influence live decisions.
# ---------------------------------------------------------------------------


async def _seed_grouped(db_session, n_candidates: int, rows_each: int = 6) -> list[str]:
    """n candidates, alternating label, each contributing several rows, with
    explicit increasing timestamps so the temporal ordering is deterministic.
    """
    group_ids = []
    for c in range(n_candidates):
        candidate = await _make_candidate(db_session, f"MINT{c:04d}")
        label = c % 2
        for r in range(rows_each):
            row = _row(label, r)
            row.candidate_id = candidate.id
            row.created_at = BASE_TIME + timedelta(minutes=c, seconds=r)
            db_session.add(row)
        group_ids.append(str(candidate.id))
    await db_session.commit()
    return group_ids


async def test_no_candidate_is_split_across_the_holdout_boundary(db_session):
    """The core anti-leakage invariant."""
    await _seed_grouped(db_session, n_candidates=10)
    dataset = await load_labeled_dataset(db_session)

    train_idx, test_idx = _temporal_group_split(dataset, 0.2)
    train_groups = {dataset.groups[i] for i in train_idx}
    test_groups = {dataset.groups[i] for i in test_idx}

    assert train_groups and test_groups
    assert train_groups.isdisjoint(test_groups), "a candidate appeared on both sides of the split"


async def test_holdout_is_strictly_later_in_time_than_training(db_session):
    await _seed_grouped(db_session, n_candidates=10)
    dataset = await load_labeled_dataset(db_session)

    train_idx, test_idx = _temporal_group_split(dataset, 0.2)
    newest_train = max(dataset.group_started_at[dataset.groups[i]] for i in train_idx)
    oldest_test = min(dataset.group_started_at[dataset.groups[i]] for i in test_idx)

    assert oldest_test >= newest_train, "holdout must be in the future relative to training data"


async def test_sample_gate_counts_candidates_not_rows(db_session):
    """49 candidates x 6 rows = 294 rows, which would sail past a row-count
    gate of 50 while representing only 49 independent outcomes.
    """
    await _seed_grouped(db_session, n_candidates=MIN_TRAINING_CANDIDATES - 1, rows_each=6)
    dataset = await load_labeled_dataset(db_session)
    assert len(dataset.features) > MIN_TRAINING_CANDIDATES  # plenty of rows

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status == "skipped_insufficient_samples"
    assert (await db_session.execute(select(ModelVersion))).scalars().all() == []


async def test_rows_without_a_candidate_are_independent_groups(db_session):
    """An unlinked row shares its outcome with nothing, so it must not be
    lumped together with every other unlinked row (which would collapse the
    whole dataset into one group and make a split impossible).
    """
    for i in range(6):
        db_session.add(_row(i % 2, i))
    await db_session.commit()

    dataset = await load_labeled_dataset(db_session)

    assert dataset.candidate_count == 6
    assert len(set(dataset.groups)) == 6


async def test_noise_dataset_does_not_activate_a_model(db_session):
    """End-to-end anti-false-positive check through the real training path.

    Labels are assigned per candidate with no relationship to the features,
    so there is nothing to learn. Before the audit this combination
    activated a model roughly 42% of the time (grouping leak + a bare 0.55
    point-estimate gate). It must now register at most, never activate.
    """
    import random

    rng = random.Random(1234)
    for c in range(MIN_TRAINING_CANDIDATES + 10):
        candidate = await _make_candidate(db_session, f"NOISE{c:04d}")
        label = rng.randint(0, 1)
        for r in range(8):
            # Feature pattern chosen independently of the label.
            row = _row(rng.randint(0, 1), r)
            row.label = label
            row.candidate_id = candidate.id
            row.created_at = BASE_TIME + timedelta(minutes=c, seconds=r)
            db_session.add(row)
    await db_session.commit()

    outcome = await train_and_maybe_register(db_session)

    assert outcome.status not in ("activated", "challenger_ready"), (
        f"a model trained on noise became promotable: metrics={outcome.metrics}"
    )


async def test_activation_requires_a_lower_confidence_bound_above_chance(db_session):
    """Even a flattering point estimate must clear a significance bar."""
    await _seed_grouped(db_session, n_candidates=MIN_TRAINING_CANDIDATES + 5, rows_each=4)
    outcome = await train_and_maybe_register(db_session)

    assert outcome.metrics is not None
    assert "holdout_auc_lower_bound" in outcome.metrics
    assert outcome.metrics["scored_per"] == "candidate"
    assert outcome.metrics["split"] == "temporal_grouped_by_candidate"
    if outcome.status == "challenger_ready":
        assert outcome.metrics["holdout_auc_lower_bound"] > 0.5
        assert outcome.metrics["holdout_candidates"] >= train_module.MIN_HOLDOUT_CANDIDATES
