from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MarketSnapshot

# A price older than this is not a price, it's a memory. Matches the
# 300s "recent activity" horizon services/decision-engine/app/features.py
# and engine-solana-momentum both already use, so "recent enough to act
# on" means the same thing everywhere in this codebase.
MAX_PRICE_AGE_SECONDS = 300


async def latest_price(session: AsyncSession, symbol: str, now: datetime | None = None) -> Decimal | None:
    """The most recent MarketSnapshot.price recorded for `symbol`, from
    whatever source has actually been ingesting it — real for a Binance
    ticker once BINANCE_SYMBOLS is configured (services/data-binance), but
    nothing in this codebase ever writes a Solana price snapshot (see
    docs/PAPER_TRADING.md), so this returns None for every Solana mint
    today.

    Returns None rather than a stale quote once the newest snapshot is
    older than MAX_PRICE_AGE_SECONDS: marking or exiting a position
    against a price from an arbitrarily dead feed is worse than declining
    to act, because the caller cannot tell the difference between "the
    price is still 105" and "nothing has reported a price since Tuesday".
    Never falls back to an interpolated or last-known-good guess either —
    a caller with no *current* price genuinely cannot mark or exit a
    position, and must say so rather than act on a fabricated number.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=MAX_PRICE_AGE_SECONDS)
    result = await session.execute(
        select(MarketSnapshot.price)
        .where(
            MarketSnapshot.symbol == symbol,
            MarketSnapshot.price.is_not(None),
            MarketSnapshot.occurred_at >= cutoff,
        )
        .order_by(MarketSnapshot.occurred_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
