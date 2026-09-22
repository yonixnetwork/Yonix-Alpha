import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Order
from yonixalpha_core.logging import get_logger

from app.client import BinanceApiError, BinanceFuturesClient

log = get_logger("engine-binance-futures.orders")

TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "not_found"}


class TradingNotEnabledError(Exception):
    pass


def generate_client_order_id() -> str:
    return f"yxa-{uuid.uuid4().hex[:28]}"


async def place_order_idempotent(
    session: AsyncSession,
    client: BinanceFuturesClient,
    *,
    trading_enabled: bool,
    live_trading_enabled: bool,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Decimal | None = None,
    stop_price: Decimal | None = None,
    reduce_only: bool = False,
) -> Order:
    """Per spec section 21: create client order ID, store intent, execute,
    store exchange order ID, reconcile, update status.

    The Order row is inserted and COMMITTED before the exchange is ever
    called — that commit is the crash-safety boundary. If this process
    dies between the commit and receiving Binance's response (network
    partition, OOM kill, deploy), the order is left in `pending_submit`
    and reconcile_pending_orders() discovers the truth on the next pass by
    querying Binance for this exact client_order_id — never by guessing,
    and never by submitting a second time.

    Refuses outright — before touching the database or the exchange — if
    trading isn't explicitly enabled on both flags. Phase 5 owns the real
    risk engine, but shipping an order-placement function with no safety
    gate at all would be irresponsible regardless of phase boundaries.
    """
    if not (trading_enabled and live_trading_enabled):
        log.warning("orders.blocked_trading_disabled", symbol=symbol, side=side, order_type=order_type)
        raise TradingNotEnabledError("TRADING_ENABLED and LIVE_TRADING_ENABLED must both be true to place a live order")

    client_order_id = generate_client_order_id()
    order = Order(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        stop_price=stop_price,
        reduce_only=reduce_only,
        status="pending_submit",
    )
    session.add(order)
    await session.commit()

    params: dict = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": str(quantity),
        "newClientOrderId": client_order_id,
    }
    if price is not None:
        params["price"] = str(price)
    if stop_price is not None:
        params["stopPrice"] = str(stop_price)
    if reduce_only:
        params["reduceOnly"] = "true"

    try:
        response = await client.place_order(**params)
    except Exception as exc:  # noqa: BLE001 - any failure here leaves the order for reconciliation, never re-submitted
        log.error("orders.submit_failed", client_order_id=client_order_id, error=str(exc))
        order.status = "submit_failed"
        await session.commit()
        return order

    order.exchange_order_id = str(response.get("orderId")) if response.get("orderId") is not None else None
    order.status = response.get("status", "NEW")
    order.raw_response = response
    await session.commit()

    log.info(
        "orders.submitted",
        client_order_id=client_order_id,
        exchange_order_id=order.exchange_order_id,
        status=order.status,
    )
    return order


async def reconcile_pending_orders(session: AsyncSession, client: BinanceFuturesClient) -> list[Order]:
    """Queries Binance for every locally non-terminal order, by
    client_order_id (the one thing guaranteed to have been sent even if
    the original response was lost). A 400-class response is treated as
    "Binance has no record of this order" (never actually submitted) and
    marked `not_found` — a terminal state, safe to stop retrying. Any
    other failure (network error, 5xx, rate limit) leaves the order's
    status untouched for the next reconciliation pass, since Binance's
    exact error code for "order does not exist" (historically -2013) is
    not live-verified in this environment — status-code-class is the more
    conservative signal to key off of here.
    """
    result = await session.execute(select(Order).where(Order.status.notin_(TERMINAL_ORDER_STATUSES)))
    pending = result.scalars().all()

    reconciled: list[Order] = []
    for order in pending:
        try:
            response = await client.get_order(order.symbol, orig_client_order_id=order.client_order_id)
        except BinanceApiError as exc:
            if exc.status_code == 400:
                log.warning("orders.reconcile_not_found", client_order_id=order.client_order_id, body=exc.body)
                order.status = "not_found"
                reconciled.append(order)
            else:
                log.warning("orders.reconcile_failed_will_retry", client_order_id=order.client_order_id, error=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("orders.reconcile_failed_will_retry", client_order_id=order.client_order_id, error=str(exc))
            continue

        order.exchange_order_id = str(response.get("orderId")) if response.get("orderId") is not None else order.exchange_order_id
        order.status = response.get("status", order.status)
        order.raw_response = response
        reconciled.append(order)

    await session.commit()
    return reconciled
