from decimal import Decimal

from sqlalchemy import select

from app.events import dispatch_user_stream_message, handle_account_update, handle_order_trade_update
from yonixalpha_core.db.models import Fill, Order, Position


async def test_account_update_upserts_position(db_session):
    message = {
        "e": "ACCOUNT_UPDATE",
        "a": {
            "P": [
                {"s": "BTCUSDT", "pa": "0.75", "ep": "50000.0", "up": "12.5", "mt": "isolated"},
            ]
        },
    }
    await handle_account_update(db_session, message)

    result = await db_session.execute(select(Position).where(Position.symbol == "BTCUSDT"))
    position = result.scalar_one()
    assert position.position_amt == Decimal("0.75")
    assert position.entry_price == Decimal("50000.0")
    assert position.unrealized_pnl == Decimal("12.5")


async def test_account_update_ignores_entries_missing_symbol(db_session):
    message = {"e": "ACCOUNT_UPDATE", "a": {"P": [{"pa": "0.5"}]}}
    await handle_account_update(db_session, message)
    result = await db_session.execute(select(Position))
    assert result.scalars().all() == []


async def test_order_trade_update_updates_status_for_known_order(db_session):
    order = Order(
        client_order_id="yxa-abc123", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="NEW",
    )
    db_session.add(order)
    await db_session.commit()

    message = {
        "e": "ORDER_TRADE_UPDATE",
        "o": {"s": "BTCUSDT", "c": "yxa-abc123", "S": "BUY", "x": "NEW", "X": "PARTIALLY_FILLED", "i": 777},
    }
    await handle_order_trade_update(db_session, message)

    result = await db_session.execute(select(Order).where(Order.client_order_id == "yxa-abc123"))
    updated = result.scalar_one()
    assert updated.status == "PARTIALLY_FILLED"
    assert updated.exchange_order_id == "777"


async def test_order_trade_update_inserts_fill_on_trade_execution(db_session):
    order = Order(
        client_order_id="yxa-abc123", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="NEW",
    )
    db_session.add(order)
    await db_session.commit()

    message = {
        "e": "ORDER_TRADE_UPDATE",
        "o": {
            "s": "BTCUSDT", "c": "yxa-abc123", "S": "BUY", "x": "TRADE", "X": "FILLED",
            "i": 777, "t": 999, "L": "50000.5", "l": "0.01", "n": "0.02", "N": "USDT",
            "rp": "1.5", "T": 1700000000000,
        },
    }
    await handle_order_trade_update(db_session, message)

    fill_result = await db_session.execute(select(Fill).where(Fill.exchange_trade_id == "999"))
    fill = fill_result.scalar_one()
    assert fill.order_id == order.id
    assert fill.price == Decimal("50000.5")
    assert fill.realized_pnl == Decimal("1.5")


async def test_order_trade_update_fill_is_idempotent(db_session):
    order = Order(
        client_order_id="yxa-abc123", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="NEW",
    )
    db_session.add(order)
    await db_session.commit()

    message = {
        "e": "ORDER_TRADE_UPDATE",
        "o": {"s": "BTCUSDT", "c": "yxa-abc123", "S": "BUY", "x": "TRADE", "X": "FILLED", "i": 777, "t": 999, "L": "50000.5", "l": "0.01"},
    }
    await handle_order_trade_update(db_session, message)
    await handle_order_trade_update(db_session, message)  # duplicate delivery (WS redelivery on reconnect)

    fill_result = await db_session.execute(select(Fill).where(Fill.exchange_trade_id == "999"))
    assert len(fill_result.scalars().all()) == 1


async def test_order_trade_update_ignores_unknown_client_order_id(db_session):
    message = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "c": "unknown-id", "X": "NEW"}}
    await handle_order_trade_update(db_session, message)  # must not raise
    result = await db_session.execute(select(Order))
    assert result.scalars().all() == []


async def test_dispatch_routes_to_correct_handler(db_session):
    order = Order(
        client_order_id="yxa-dispatch", symbol="ETHUSDT", side="SELL", order_type="MARKET",
        quantity=Decimal("1"), status="NEW",
    )
    db_session.add(order)
    await db_session.commit()

    await dispatch_user_stream_message(db_session, {"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "c": "yxa-dispatch", "X": "CANCELED"}})

    result = await db_session.execute(select(Order).where(Order.client_order_id == "yxa-dispatch"))
    assert result.scalar_one().status == "CANCELED"


async def test_dispatch_ignores_unknown_event_type(db_session):
    await dispatch_user_stream_message(db_session, {"e": "MARGIN_CALL", "data": "whatever"})  # must not raise
