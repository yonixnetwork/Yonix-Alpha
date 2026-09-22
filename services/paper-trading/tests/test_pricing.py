from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.pricing import latest_price
from yonixalpha_core.db.models import MarketSnapshot

SYMBOL = "BTCUSDT"


async def test_returns_none_when_nothing_recorded(db_session):
    assert await latest_price(db_session, SYMBOL) is None


async def test_returns_the_most_recent_price(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(
        MarketSnapshot(
            source="binance", symbol=SYMBOL, snapshot_type="kline_1m", occurred_at=now - timedelta(minutes=1), price=Decimal("100"), payload={}
        )
    )
    db_session.add(
        MarketSnapshot(source="binance", symbol=SYMBOL, snapshot_type="kline_1m", occurred_at=now, price=Decimal("105"), payload={})
    )
    await db_session.commit()

    assert await latest_price(db_session, SYMBOL) == Decimal("105")


async def test_ignores_snapshots_with_no_price(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(MarketSnapshot(source="binance", symbol=SYMBOL, snapshot_type="depth", occurred_at=now, price=None, payload={}))
    await db_session.commit()

    assert await latest_price(db_session, SYMBOL) is None


async def test_scoped_to_the_requested_symbol(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(MarketSnapshot(source="binance", symbol="ETHUSDT", snapshot_type="kline_1m", occurred_at=now, price=Decimal("2000"), payload={}))
    await db_session.commit()

    assert await latest_price(db_session, SYMBOL) is None
