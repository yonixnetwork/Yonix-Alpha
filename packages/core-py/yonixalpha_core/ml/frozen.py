"""Frozen validation windows (master §38): "periodically freeze a validation
dataset that the model has never seen".

Once a week per sample family, the UTC day LAG days ago is frozen (its
labels are complete by then). From that moment every trainer excludes the
window (exclude()), so models trained afterwards never see it; a model
trained earlier on data ending before the window never saw it either. The
ml service scores each model only on the windows it never saw
(services/ml/app/validation.py). Rows are kept: freezing only changes who
may train on them.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, not_, or_, select

FREEZE_EVERY = timedelta(days=7)
LAG = timedelta(days=3)
FAMILIES = ("evm_entry", "wallet_entry", "evm_exit", "solana_opportunity", "solana_candidate")

# The verdict of a model on a frozen set (services/ml/app/validation.py).
MIN_N = 100
MIN_CLASS = 10
MIN_AUC = 0.55
MAX_ECE = 0.10
PASS_RULE = (f"PASS: at least {MIN_N} labelled samples the model never saw, {MIN_CLASS}+ of each class, AUC >= {MIN_AUC} "
             f"with its 95 % lower bound above 0.5, and calibration error (ECE) <= {MAX_ECE}")
NOT_AVAILABLE = {
    "copy_trades": "no model scores copy trades: copy entries and exits are rules only, and a wallet buying is never "
                   "permission to buy",
    "wallet_exits": "wallet exit labels (PREMATURE / LATE exit) have no model yet",
}


def family_of(name: str) -> str | None:
    """The sample family a model is trained (and validated) on."""
    for prefix, family in (("shadow_evm_", "evm_entry"), ("shadow_wallet_", "wallet_entry"), ("shadow_exit_", "evm_exit"),
                           ("shadow_", "solana_opportunity"), ("gate_solana_", "solana_candidate")):
        if name.startswith(prefix):
            return family
    return "solana_candidate" if name == "solana_candidate_momentum" else None


def family_source(family: str):
    """(time column, extra filters) of a family's labelled samples."""
    from yonixalpha_core.db.models import (EvmExitSample, EvmMlSample, MLFeatureSnapshot, OpportunityOutcome,
                                           WalletTradeLabel)

    if family == "evm_entry":
        return EvmMlSample.decided_at, [EvmMlSample.labels["unknown"].is_(None)]
    if family == "wallet_entry":
        return WalletTradeLabel.entry_at, [WalletTradeLabel.kind == "EPISODE"]
    if family == "evm_exit":
        return EvmExitSample.at, [EvmExitSample.labels.is_not(None)]
    if family == "solana_opportunity":
        return OpportunityOutcome.decided_at, [OpportunityOutcome.status == "COMPLETE",
                                               OpportunityOutcome.labels.is_not(None)]
    if family == "solana_candidate":
        return MLFeatureSnapshot.created_at, [MLFeatureSnapshot.label.is_not(None)]
    raise ValueError(f"unknown family {family}")


async def windows(session, family: str) -> list[tuple[datetime, datetime, int]]:
    from yonixalpha_core.db.models import MlValidationSet

    rows = (await session.execute(select(MlValidationSet.window_start, MlValidationSet.window_end, MlValidationSet.id)
                                  .where(MlValidationSet.family == family))).all()
    return [(a, b, i) for a, b, i in rows]


def exclude(col, wins: list[tuple[datetime, datetime, int]]):
    """A WHERE condition keeping only rows outside every frozen window."""
    if not wins:
        return and_(True)
    return not_(or_(*[and_(col >= a, col < b) for a, b, _ in wins]))


async def freeze_due(session, now: datetime) -> list[dict[str, Any]]:
    """Freezes, for every family whose last freeze is FREEZE_EVERY old (or
    that has none), the UTC day LAG ago. Caller commits."""
    from sqlalchemy.dialects.postgresql import insert

    from yonixalpha_core.db.models import MlValidationSet

    out = []
    day = (now - LAG).date()
    start = datetime.combine(day, time(0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    for family in FAMILIES:
        last = (await session.execute(select(func.max(MlValidationSet.frozen_at))
                                      .where(MlValidationSet.family == family))).scalar_one()
        if last is not None and now - last < FREEZE_EVERY:
            continue
        col, filters = family_source(family)
        n = (await session.execute(select(func.count()).where(col >= start, col < end, *filters))).scalar_one()
        r = await session.execute(insert(MlValidationSet).values(
            family=family, window_start=start, window_end=end, frozen_at=now, samples=n,
            note=None if n else "no labelled samples in this window")
            .on_conflict_do_nothing(index_elements=["family", "window_start"]))
        if r.rowcount:
            out.append({"family": family, "window": str(day), "samples": n})
    return out
