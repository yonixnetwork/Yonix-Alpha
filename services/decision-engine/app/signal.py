from yonixalpha_core.risk import DataQuality

from app.features import CandidateFeatures

# Deliberately below ENTRY_CONFIDENCE_THRESHOLD: this codebase has no Solana
# price, liquidity, or wallet-concentration feed (see ARCHITECTURE_AUDIT.md),
# so a candidate's features are structurally never better than DEGRADED
# today. Capping DEGRADED-quality confidence below the entry threshold means
# a LONG decision is unreachable until a real data feed exists — the honest
# consequence of the spec's "never fabricate data" rule, not a bug to raise
# later.
CONFIDENCE_CAP_DEGRADED_DATA = 0.35
ENTRY_CONFIDENCE_THRESHOLD = 0.6

ACCELERATION_RATIO_THRESHOLD = 3.0
YOUNG_TOKEN_SECONDS = 3600


def score(features: CandidateFeatures) -> tuple[float, list[str]]:
    """Turns CandidateFeatures into a confidence score in [0, 1] plus the
    reasons behind it, never a bare number a caller has to trust blindly.
    Each contributing signal is small and additive; the DEGRADED-data cap
    dominates everything else by design (see module docstring above).
    """
    if features.data_quality in (DataQuality.STALE, DataQuality.UNAVAILABLE):
        return 0.0, [f"data_quality is {features.data_quality.value} — cannot score"]

    confidence = 0.0
    reasons: list[str] = []

    if features.tx_acceleration_ratio is not None and features.tx_acceleration_ratio >= ACCELERATION_RATIO_THRESHOLD:
        confidence += 0.3
        reasons.append(
            f"transaction acceleration ratio {features.tx_acceleration_ratio:.2f} >= {ACCELERATION_RATIO_THRESHOLD}"
        )

    if features.token_age_seconds < YOUNG_TOKEN_SECONDS:
        confidence += 0.15
        reasons.append(f"young token ({features.token_age_seconds:.0f}s old)")

    if not reasons:
        reasons.append("no qualifying signal in available features")

    if features.data_quality == DataQuality.DEGRADED and confidence > CONFIDENCE_CAP_DEGRADED_DATA:
        confidence = CONFIDENCE_CAP_DEGRADED_DATA
        reasons.append(
            f"confidence capped at {CONFIDENCE_CAP_DEGRADED_DATA} because data_quality is degraded "
            "(no Solana price/liquidity/wallet feed exists in this codebase)"
        )

    return confidence, reasons
