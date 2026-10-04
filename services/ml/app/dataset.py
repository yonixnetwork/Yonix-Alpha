import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MLFeatureSnapshot
from yonixalpha_core.ml import frozen

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


@dataclass
class LabeledDataset:
    """Labeled training rows plus the two things that make an honest
    holdout possible: which candidate each row came from, and when.

    Both matter because decision-engine writes one row per candidate per
    evaluation cycle (every 15s), and paper-trading then stamps the SAME
    label on every row belonging to that candidate. Rows are therefore
    heavily duplicated within a candidate and are NOT independent samples.
    Splitting them randomly puts near-identical siblings on both sides of
    the holdout, which lets a model score its own training data: an audit
    probe measured holdout AUC 0.73 on a dataset with no signal at all,
    comfortably clearing the activation gate. `groups` exists so the
    splitter can keep a candidate wholly on one side, and `group_started_at`
    so the split can also run forward in time rather than shuffling the
    past and the future together.
    """

    features: list[list[float]] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    group_started_at: dict[str, datetime] = field(default_factory=dict)
    skipped_rows: int = 0
    # ids of the frozen validation windows left out (master §38): a model
    # trained on this dataset never saw them, so ml validation may score it there
    frozen_excluded: list[int] = field(default_factory=list)
    dataset_end: datetime | None = None  # the newest row used

    @property
    def candidate_count(self) -> int:
        """Independent outcomes — the number that actually bounds how much
        can be learned, unlike len(features), which mostly counts how long
        each candidate happened to be observed.
        """
        return len(self.group_started_at)


async def load_labeled_dataset(session: AsyncSession, feature_names: list[str] = FEATURE_NAMES) -> LabeledDataset:
    """Every row returned has a non-NULL label — set by paper-trading when
    it closes a position with a known outcome (see docs/ML.md). A row whose
    `features` payload is missing one of feature_names is skipped and
    counted rather than defaulted to 0.0 — that would mean decision-engine's
    feature space changed after the row was written, and training on a
    silently fabricated value would defeat the whole point of a labeled
    dataset.
    """
    wins = await frozen.windows(session, "solana_candidate")
    result = await session.execute(
        select(MLFeatureSnapshot).where(MLFeatureSnapshot.label.is_not(None),
                                        frozen.exclude(MLFeatureSnapshot.created_at, wins))
        .order_by(MLFeatureSnapshot.created_at)
    )
    rows = result.scalars().all()

    dataset = LabeledDataset(frozen_excluded=[i for *_, i in wins])
    for row in rows:
        if any(name not in row.features for name in feature_names):
            dataset.skipped_rows += 1
            continue
        # A row with no candidate link shares its outcome with nothing, so
        # it is its own group rather than being lumped in with every other
        # unlinked row.
        group = str(row.candidate_id) if row.candidate_id is not None else f"orphan:{uuid.uuid4()}"
        dataset.features.append([row.features[name] for name in feature_names])
        dataset.labels.append(row.label)
        dataset.groups.append(group)
        dataset.group_started_at.setdefault(group, row.created_at)
        dataset.dataset_end = row.created_at

    return dataset
