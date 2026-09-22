from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Position
from yonixalpha_core.logging import get_logger

from app.client import BinanceFuturesClient

log = get_logger("engine-binance-futures.positions")


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def sync_positions(session: AsyncSession, client: BinanceFuturesClient) -> list[str]:
    """Overwrites the `positions` table from GET /fapi/v2/positionRisk —
    the authoritative source (spec section 22: reconcile from live
    exchange state, never trust locally-derived numbers for entry price,
    liquidation price, unrealized PnL). Binance returns one row per
    symbol it lists (hundreds), most with positionAmt="0" and never
    opened; those are skipped unless we're already tracking that symbol
    (so a position that just closed to flat still gets its row updated
    to reflect that, rather than going stale).
    """
    raw_positions = await client.get_position_risk()

    tracked_result = await session.execute(select(Position.symbol))
    tracked_symbols = set(tracked_result.scalars().all())

    updated: list[str] = []
    for raw in raw_positions:
        symbol = raw.get("symbol")
        position_amt = _to_decimal(raw.get("positionAmt"))
        if not symbol or position_amt is None:
            continue
        if position_amt == 0 and symbol not in tracked_symbols:
            continue

        values = dict(
            symbol=symbol,
            position_amt=position_amt,
            entry_price=_to_decimal(raw.get("entryPrice")),
            mark_price=_to_decimal(raw.get("markPrice")),
            unrealized_pnl=_to_decimal(raw.get("unRealizedProfit")),
            leverage=_to_int(raw.get("leverage")),
            margin_type=raw.get("marginType"),
            liquidation_price=_to_decimal(raw.get("liquidationPrice")),
        )
        stmt = insert(Position).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["symbol"], set_=values)
        await session.execute(stmt)
        updated.append(symbol)

    await session.commit()
    log.info("positions.synced", symbols=updated)
    return updated
