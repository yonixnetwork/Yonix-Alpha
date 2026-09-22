from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.risk import DataQuality

# Same window as engine-solana-momentum, kept as an independent constant
# rather than importing from that service — services are independent
# processes (spec: "Each must be independent"), so this is a deliberate,
# small duplication of a design choice, not a bug to dedupe later.
WINDOW_SECONDS = 300


@dataclass
class CandidateFeatures:
    token_age_seconds: float
    tx_count_current_window: int
    tx_count_prior_window: int
    tx_acceleration_ratio: float | None
    data_quality: DataQuality


async def compute_candidate_features(
    session: AsyncSession, candidate: TradingCandidate, now: datetime
) -> CandidateFeatures:
    """Computes what's honestly available for a Solana candidate today:
    transaction-count acceleration (from token_events, the same data
    engine-solana-momentum produces) and token age. `data_quality` is
    always at best DEGRADED, never HEALTHY — this codebase has no Solana
    price, liquidity, or wallet-concentration feed (see
    ARCHITECTURE_AUDIT.md), and a signal engine scoring "healthy" data it
    structurally doesn't have would be exactly the kind of fabrication the
    spec forbids (section 53). UNAVAILABLE only if the Token row itself
    can't be found (shouldn't happen in normal operation, since the
    caller already holds a valid candidate referencing it).
    """
    token = await session.get(Token, candidate.token_id)
    if token is None:
        return CandidateFeatures(
            token_age_seconds=0.0,
            tx_count_current_window=0,
            tx_count_prior_window=0,
            tx_acceleration_ratio=None,
            data_quality=DataQuality.UNAVAILABLE,
        )

    token_age_seconds = max((now - token.first_seen_at.replace(tzinfo=now.tzinfo)).total_seconds(), 0.0)

    window = timedelta(seconds=WINDOW_SECONDS)
    prior_start = now - 2 * window
    current_start = now - window

    bucket = case((TokenEvent.occurred_at >= current_start, "current"), else_="prior")
    stmt = (
        select(bucket.label("bucket"), func.count().label("count"))
        .where(
            TokenEvent.token_id == candidate.token_id,
            TokenEvent.event_type == "transfer",
            TokenEvent.occurred_at >= prior_start,
            TokenEvent.occurred_at <= now,
        )
        .group_by(bucket)
    )
    result = await session.execute(stmt)
    counts = {row.bucket: row.count for row in result}
    current_count = counts.get("current", 0)
    prior_count = counts.get("prior", 0)
    ratio = (current_count / prior_count) if prior_count > 0 else None

    return CandidateFeatures(
        token_age_seconds=token_age_seconds,
        tx_count_current_window=current_count,
        tx_count_prior_window=prior_count,
        tx_acceleration_ratio=ratio,
        data_quality=DataQuality.DEGRADED,
    )
