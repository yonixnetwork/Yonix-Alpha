from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MLFeatureSnapshot

# Must match services/decision-engine/app/ml_features.py's FEATURE_NAMES —
# duplicated rather than imported across the service boundary (services
# are independent processes, each with their own dependency closure; see
# that module's own comment on WINDOW_SECONDS for the same design choice
# made elsewhere in this codebase). If decision-engine's feature space
# changes, this list must be updated to match or training silently skips
# every row via the missing-feature-key guard below.
FEATURE_NAMES = [
    "token_age_seconds",
    "tx_count_current_window",
    "tx_count_prior_window",
    "tx_acceleration_ratio",
    "has_acceleration_ratio",
]


async def load_labeled_dataset(
    session: AsyncSession, feature_names: list[str] = FEATURE_NAMES
) -> tuple[list[list[float]], list[int], int]:
    """Every row returned has a non-NULL label — set by whatever future
    phase first closes a real or paper position with a known outcome (see
    docs/ML.md; nothing in this codebase sets one today, so this returns
    empty until then). A row whose `features` payload is missing one of
    feature_names is skipped and counted rather than defaulted to 0.0 —
    that would mean decision-engine's feature space changed after the row
    was written, and training on a silently fabricated value would defeat
    the whole point of a labeled dataset.
    """
    result = await session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.label.is_not(None)))
    rows = result.scalars().all()

    features: list[list[float]] = []
    labels: list[int] = []
    skipped = 0
    for row in rows:
        if any(name not in row.features for name in feature_names):
            skipped += 1
            continue
        features.append([row.features[name] for name in feature_names])
        labels.append(row.label)

    return features, labels, skipped
