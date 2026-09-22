from decimal import Decimal

import pytest
from sqlalchemy import select

from app.client import BinanceApiError
from app.orders import TradingNotEnabledError, place_order_idempotent, reconcile_pending_orders
from yonixalpha_core.db.models import Order


class FakeClient:
    """Test double standing in for BinanceFuturesClient — no network, no
    httpx, just controllable return values/exceptions per call.
    """

    def __init__(self):
        self.place_order_calls: list[dict] = []
        self.get_order_calls: list[dict] = []
        self._place_order_response = {"orderId": 555, "status": "NEW"}
        self._place_order_exception: Exception | None = None
        self._get_order_response = None
        self._get_order_exception: Exception | None = None

    async def place_order(self, **params):
        self.place_order_calls.append(params)
        if self._place_order_exception:
            raise self._place_order_exception
        return self._place_order_response

    async def get_order(self, symbol, orig_client_order_id=None, order_id=None):
        self.get_order_calls.append({"symbol": symbol, "orig_client_order_id": orig_client_order_id})
        if self._get_order_exception:
            raise self._get_order_exception
        return self._get_order_response


async def test_refuses_when_trading_disabled_no_db_row_created(db_session):
    client = FakeClient()
    with pytest.raises(TradingNotEnabledError):
        await place_order_idempotent(
            db_session,
            client,
            trading_enabled=False,
            live_trading_enabled=True,
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.01"),
        )

    assert client.place_order_calls == []
    result = await db_session.execute(select(Order))
    assert result.scalars().all() == []


async def test_refuses_when_live_trading_disabled(db_session):
    client = FakeClient()
    with pytest.raises(TradingNotEnabledError):
        await place_order_idempotent(
            db_session,
            client,
            trading_enabled=True,
            live_trading_enabled=False,
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.01"),
        )
    assert client.place_order_calls == []


async def test_successful_order_persists_exchange_order_id_and_status(db_session):
    client = FakeClient()
    order = await place_order_idempotent(
        db_session,
        client,
        trading_enabled=True,
        live_trading_enabled=True,
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
    )

    assert order.exchange_order_id == "555"
    assert order.status == "NEW"
    assert order.raw_response == {"orderId": 555, "status": "NEW"}
    assert len(client.place_order_calls) == 1
    assert client.place_order_calls[0]["newClientOrderId"] == order.client_order_id


async def test_client_order_id_sent_to_exchange_matches_persisted_row(db_session):
    client = FakeClient()
    order = await place_order_idempotent(
        db_session,
        client,
        trading_enabled=True,
        live_trading_enabled=True,
        symbol="ETHUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity=Decimal("1.5"),
        price=Decimal("2500.50"),
    )

    assert client.place_order_calls[0]["price"] == "2500.50"
    assert client.place_order_calls[0]["symbol"] == "ETHUSDT"

    # The exact client_order_id we sent must be independently recoverable
    # from the DB row alone -- this is what a restart-time reconciliation
    # pass depends on.
    result = await db_session.execute(select(Order).where(Order.client_order_id == order.client_order_id))
    row = result.scalar_one()
    assert row.id == order.id


async def test_exchange_exception_leaves_order_row_for_reconciliation(db_session):
    """The core crash-safety property: if the exchange call fails for any
    reason, the Order row still exists (intent was already committed
    before the call) — the caller never loses track of what was attempted.
    """
    client = FakeClient()
    client._place_order_exception = TimeoutError("network timeout")

    order = await place_order_idempotent(
        db_session,
        client,
        trading_enabled=True,
        live_trading_enabled=True,
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
    )

    assert order.status == "submit_failed"
    result = await db_session.execute(select(Order).where(Order.client_order_id == order.client_order_id))
    assert result.scalar_one().status == "submit_failed"


async def test_reconcile_updates_pending_order_from_exchange_state(db_session):
    order = Order(
        client_order_id="yxa-test-1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
        status="pending_submit",
    )
    db_session.add(order)
    await db_session.commit()

    client = FakeClient()
    client._get_order_response = {"orderId": 999, "status": "FILLED"}

    reconciled = await reconcile_pending_orders(db_session, client)

    assert len(reconciled) == 1
    assert reconciled[0].status == "FILLED"
    assert reconciled[0].exchange_order_id == "999"
    assert client.get_order_calls[0]["orig_client_order_id"] == "yxa-test-1"


async def test_reconcile_marks_400_response_as_not_found(db_session):
    order = Order(
        client_order_id="yxa-test-2", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="submit_failed",
    )
    db_session.add(order)
    await db_session.commit()

    client = FakeClient()
    client._get_order_exception = BinanceApiError(400, '{"code":-2013,"msg":"Order does not exist."}')

    reconciled = await reconcile_pending_orders(db_session, client)

    assert len(reconciled) == 1
    assert reconciled[0].status == "not_found"


async def test_reconcile_leaves_status_unchanged_on_transient_error(db_session):
    order = Order(
        client_order_id="yxa-test-3", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="pending_submit",
    )
    db_session.add(order)
    await db_session.commit()

    client = FakeClient()
    client._get_order_exception = BinanceApiError(503, "Service unavailable")

    reconciled = await reconcile_pending_orders(db_session, client)

    assert reconciled == []
    result = await db_session.execute(select(Order).where(Order.client_order_id == "yxa-test-3"))
    assert result.scalar_one().status == "pending_submit"  # unchanged, will retry next pass


async def test_reconcile_skips_terminal_orders(db_session):
    filled = Order(
        client_order_id="yxa-filled", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=Decimal("0.01"), status="FILLED",
    )
    db_session.add(filled)
    await db_session.commit()

    client = FakeClient()
    reconciled = await reconcile_pending_orders(db_session, client)

    assert reconciled == []
    assert client.get_order_calls == []
