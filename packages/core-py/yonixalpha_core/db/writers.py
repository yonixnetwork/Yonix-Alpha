from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MarketSnapshot
from yonixalpha_core.schemas.market import NormalizedMarketEvent


async def write_market_snapshot(session: AsyncSession, event: NormalizedMarketEvent) -> None:
    """Insert a normalized event, silently deduplicating on
    (source, symbol, snapshot_type, sequence) via ON CONFLICT DO NOTHING.
    `sequence=None` rows never collide with each other (Postgres treats
    NULLs as distinct in a unique constraint) — expected for event types
    with no source-native sequence id; see NormalizedMarketEvent.
    """
    stmt = insert(MarketSnapshot).values(
        source=event.source,
        symbol=event.symbol,
        snapshot_type=event.snapshot_type,
        occurred_at=event.occurred_at,
        price=event.price,
        volume=event.volume,
        sequence=event.sequence,
        payload=event.payload,
    )
    stmt = stmt.on_conflict_do_nothing(constraint="uq_market_snapshots_dedup")
    await session.execute(stmt)
    await session.commit()
