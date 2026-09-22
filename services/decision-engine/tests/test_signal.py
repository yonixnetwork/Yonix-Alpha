import pytest

from app.features import CandidateFeatures
from app.signal import CONFIDENCE_CAP_DEGRADED_DATA, ENTRY_CONFIDENCE_THRESHOLD, score
from yonixalpha_core.risk import DataQuality


def _features(**overrides) -> CandidateFeatures:
    defaults = dict(
        token_age_seconds=100.0,
        tx_count_current_window=6,
        tx_count_prior_window=2,
        tx_acceleration_ratio=3.0,
        data_quality=DataQuality.DEGRADED,
    )
    defaults.update(overrides)
    return CandidateFeatures(**defaults)


def test_stale_data_scores_zero():
    confidence, reasons = score(_features(data_quality=DataQuality.STALE))
    assert confidence == 0.0
    assert "stale" in reasons[0]


def test_unavailable_data_scores_zero():
    confidence, reasons = score(_features(data_quality=DataQuality.UNAVAILABLE))
    assert confidence == 0.0
    assert "unavailable" in reasons[0]


def test_degraded_data_is_capped_below_entry_threshold():
    """The structural guarantee this whole module exists to enforce: today,
    every Solana candidate this codebase can produce features for is at
    best DEGRADED quality (no price/liquidity/wallet feed exists), so no
    combination of contributing signals should ever reach the entry
    threshold.
    """
    confidence, reasons = score(_features(tx_acceleration_ratio=100.0, token_age_seconds=1.0))
    assert confidence == CONFIDENCE_CAP_DEGRADED_DATA
    assert confidence < ENTRY_CONFIDENCE_THRESHOLD
    assert any("capped" in r for r in reasons)


def test_no_qualifying_signal_scores_zero_with_explanation():
    confidence, reasons = score(_features(tx_acceleration_ratio=None, token_age_seconds=999999.0))
    assert confidence == 0.0
    assert reasons == ["no qualifying signal in available features"]


def test_acceleration_and_youth_both_contribute():
    confidence, reasons = score(_features(tx_acceleration_ratio=3.0, token_age_seconds=10.0, data_quality=DataQuality.HEALTHY))
    assert confidence == pytest.approx(0.45)
    assert len(reasons) == 2


def test_below_acceleration_threshold_does_not_contribute():
    confidence, _ = score(_features(tx_acceleration_ratio=2.9, token_age_seconds=999999.0))
    assert confidence == 0.0
