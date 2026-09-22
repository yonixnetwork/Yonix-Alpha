from app.features import CandidateFeatures
from app.ml_features import FEATURE_NAMES, to_feature_vector
from yonixalpha_core.risk import DataQuality


def test_feature_vector_keys_match_declared_feature_names():
    features = CandidateFeatures(
        token_age_seconds=42.0,
        tx_count_current_window=6,
        tx_count_prior_window=2,
        tx_acceleration_ratio=3.0,
        data_quality=DataQuality.DEGRADED,
    )
    vector = to_feature_vector(features)
    assert set(vector.keys()) == set(FEATURE_NAMES)
    assert vector["tx_acceleration_ratio"] == 3.0
    assert vector["has_acceleration_ratio"] == 1.0


def test_missing_acceleration_ratio_is_encoded_as_zero_with_indicator():
    features = CandidateFeatures(
        token_age_seconds=42.0,
        tx_count_current_window=0,
        tx_count_prior_window=0,
        tx_acceleration_ratio=None,
        data_quality=DataQuality.DEGRADED,
    )
    vector = to_feature_vector(features)
    assert vector["tx_acceleration_ratio"] == 0.0
    assert vector["has_acceleration_ratio"] == 0.0
