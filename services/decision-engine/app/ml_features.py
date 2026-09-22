from app.features import CandidateFeatures

# The exact, ordered feature space this service persists and predicts
# against. A ModelVersion trained by services/ml stores its own
# feature_names list and SklearnModel.predict() maps by name (not
# position), so this list only has to match what's actually computed here
# — not any particular training run's column order.
FEATURE_NAMES = [
    "token_age_seconds",
    "tx_count_current_window",
    "tx_count_prior_window",
    "tx_acceleration_ratio",
    "has_acceleration_ratio",
]


def to_feature_vector(features: CandidateFeatures) -> dict[str, float]:
    """CandidateFeatures.tx_acceleration_ratio is None whenever there's no
    prior-window activity to divide by (see app/features.py) — a real
    "no data" state, not a zero. Encoding it as 0.0 plus a separate
    has_acceleration_ratio indicator is standard missing-value handling
    for a numeric model input: it represents the absence honestly rather
    than presenting a fabricated ratio of zero as if it were observed.
    """
    return {
        "token_age_seconds": features.token_age_seconds,
        "tx_count_current_window": float(features.tx_count_current_window),
        "tx_count_prior_window": float(features.tx_count_prior_window),
        "tx_acceleration_ratio": features.tx_acceleration_ratio if features.tx_acceleration_ratio is not None else 0.0,
        "has_acceleration_ratio": 1.0 if features.tx_acceleration_ratio is not None else 0.0,
    }
