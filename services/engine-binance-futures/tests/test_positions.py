from decimal import Decimal

from sqlalchemy import select

from app.positions import sync_positions
from yonixalpha_core.db.models import Position


class FakePositionRiskClient:
    def __init__(self, positions: list[dict]):
        self._positions = positions

    async def get_position_risk(self):
        return self._positions


def _raw(symbol, position_amt, **overrides):
    base = {
        "symbol": symbol,
        "positionAmt": position_amt,
        "entryPrice": "50000.0",
        "markPrice": "50100.0",
        "unRealizedProfit": "10.5",
        "leverage": "10",
        "marginType": "isolated",
        "liquidationPrice": "45000.0",
    }
    base.update(overrides)
    return base


async def test_sync_skips_untracked_zero_position_symbols(db_session):
    client = FakePositionRiskClient([_raw("BTCUSDT", "0"), _raw("ETHUSDT", "0")])
    updated = await sync_positions(db_session, client)

    assert updated == []
    result = await db_session.execute(select(Position))
    assert result.scalars().all() == []


async def test_sync_creates_row_for_nonzero_position(db_session):
    client = FakePositionRiskClient([_raw("BTCUSDT", "0.5")])
    updated = await sync_positions(db_session, client)

    assert updated == ["BTCUSDT"]
    result = await db_session.execute(select(Position).where(Position.symbol == "BTCUSDT"))
    position = result.scalar_one()
    assert position.position_amt == Decimal("0.5")
    assert position.entry_price == Decimal("50000.0")
    assert position.leverage == 10
    assert position.margin_type == "isolated"


async def test_sync_updates_tracked_symbol_when_closed(db_session):
    """A position that closes to zero must still get its row updated (not
    skipped), so the DB reflects "now flat" rather than a stale non-zero
    snapshot.
    """
    db_session.add(Position(symbol="BTCUSDT", position_amt=Decimal("0.5")))
    await db_session.commit()

    client = FakePositionRiskClient([_raw("BTCUSDT", "0")])
    updated = await sync_positions(db_session, client)

    assert updated == ["BTCUSDT"]
    result = await db_session.execute(select(Position).where(Position.symbol == "BTCUSDT"))
    assert result.scalar_one().position_amt == Decimal("0")


async def test_sync_is_upsert_not_duplicate(db_session):
    client = FakePositionRiskClient([_raw("BTCUSDT", "0.5")])
    await sync_positions(db_session, client)
    await sync_positions(db_session, client)  # second sync, same symbol

    result = await db_session.execute(select(Position).where(Position.symbol == "BTCUSDT"))
    assert len(result.scalars().all()) == 1


async def test_sync_handles_missing_symbol_gracefully(db_session):
    client = FakePositionRiskClient([{"positionAmt": "0.5"}])  # no "symbol" key
    updated = await sync_positions(db_session, client)
    assert updated == []
