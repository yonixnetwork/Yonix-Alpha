from app.dataset import FEATURE_NAMES, load_labeled_dataset
from yonixalpha_core.db.models import MLFeatureSnapshot


def _feature_row(label, **overrides) -> MLFeatureSnapshot:
    features = {name: 1.0 for name in FEATURE_NAMES}
    features.update(overrides)
    return MLFeatureSnapshot(symbol="TEST", features=features, label=label)


async def test_unlabeled_rows_are_excluded(db_session):
    db_session.add(_feature_row(label=None))
    db_session.add(_feature_row(label=1))
    await db_session.commit()

    dataset = await load_labeled_dataset(db_session)
    assert len(dataset.features) == 1
    assert dataset.labels == [1]
    assert dataset.skipped_rows == 0


async def test_rows_missing_a_declared_feature_are_skipped_and_counted(db_session):
    row = _feature_row(label=1)
    del row.features[FEATURE_NAMES[0]]  # simulate a feature-space drift
    db_session.add(row)
    await db_session.commit()

    dataset = await load_labeled_dataset(db_session)
    assert dataset.features == []
    assert dataset.labels == []
    assert dataset.skipped_rows == 1


async def test_feature_vector_order_matches_feature_names(db_session):
    row = _feature_row(label=0, token_age_seconds=42.0, tx_count_current_window=6.0)
    db_session.add(row)
    await db_session.commit()

    dataset = await load_labeled_dataset(db_session)
    X = dataset.features
    assert len(X) == 1
    assert X[0][FEATURE_NAMES.index("token_age_seconds")] == 42.0
    assert X[0][FEATURE_NAMES.index("tx_count_current_window")] == 6.0
    assert dataset.labels == [0]


async def test_empty_dataset_returns_empty_lists(db_session):
    dataset = await load_labeled_dataset(db_session)
    assert dataset.features == []
    assert dataset.labels == []
    assert dataset.skipped_rows == 0
    assert dataset.candidate_count == 0
