from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MarketSnapshot


async def latest_price(session: AsyncSession, symbol: str) -> Decimal | None:
    """The most recent MarketSnapshot.price recorded for `symbol`, from
    whatever source has actually been ingesting it — real for a Binance
    ticker once BINANCE_SYMBOLS is configured (services/data-binance), but
    nothing in this codebase ever writes a Solana price snapshot (see
    docs/PAPER_TRADING.md), so this returns None for every Solana mint
    today. Never falls back to a stale, interpolated, or last-known-good
    guess — a caller with no real price genuinely cannot mark or exit a
    position, and must say so rather than act on a fabricated number.
    """
    result = await session.execute(
        select(MarketSnapshot.price)
        .where(MarketSnapshot.symbol == symbol, MarketSnapshot.price.is_not(None))
        .order_by(MarketSnapshot.occurred_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
