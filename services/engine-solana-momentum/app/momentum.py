from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import TokenEvent

# Simplified, honestly-scoped proxy for the fuller "Volume Quality" feature
# set in the spec (unique buyers, wallet concentration, smart-wallet
# participation, price acceleration, ...): this engine only has
# transaction-COUNT data to work with (see app/ingest.py — no Solana price
# feed exists yet in this codebase), so it computes transaction-count
# acceleration only. A high ratio here means "a lot more transferChecked
# activity than before," not "genuinely bullish" — treat it as a trigger
# for the Feature/Risk/Decision Engine (Phase 5) to investigate further,
# never as a trading signal on its own.
WINDOW_SECONDS = 300
MIN_CURRENT_WINDOW_EVENTS = 5
ACCELERATION_RATIO_THRESHOLD = 3.0


@dataclass
class AccelerationResult:
    current_count: int
    prior_count: int

    @property
    def ratio(self) -> float | None:
        if self.prior_count == 0:
            return None  # undefined, not infinite — avoid the "0 -> 1 is infinite acceleration" false signal
        return self.current_count / self.prior_count

    @property
    def is_accelerating(self) -> bool:
        if self.current_count < MIN_CURRENT_WINDOW_EVENTS:
            return False
        ratio = self.ratio
        return ratio is not None and ratio >= ACCELERATION_RATIO_THRESHOLD


async def compute_transfer_acceleration(session: AsyncSession, token_id, now: datetime) -> AccelerationResult:
    """Counts `transfer` TokenEvents for this token in [now-2W, now-W) vs
    [now-W, now], where W = WINDOW_SECONDS. Both counts come from a single
    grouped query rather than two round-trips. The current window's upper
    bound is inclusive of `now` itself: callers evaluate acceleration using
    the just-inserted event's own occurred_at as `now`, so an exclusive
    bound would always undercount that triggering event by one.
    """
    window = timedelta(seconds=WINDOW_SECONDS)
    prior_start = now - 2 * window
    current_start = now - window

    bucket = case((TokenEvent.occurred_at >= current_start, "current"), else_="prior")
    stmt = (
        select(bucket.label("bucket"), func.count().label("count"))
        .where(
            TokenEvent.token_id == token_id,
            TokenEvent.event_type == "transfer",
            TokenEvent.occurred_at >= prior_start,
            TokenEvent.occurred_at <= now,
        )
        .group_by(bucket)
    )
    result = await session.execute(stmt)
    counts = {row.bucket: row.count for row in result}
    return AccelerationResult(current_count=counts.get("current", 0), prior_count=counts.get("prior", 0))
