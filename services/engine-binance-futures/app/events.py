from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Fill, Order, Position
from yonixalpha_core.logging import get_logger

log = get_logger("engine-binance-futures.events")

# Field names below (Binance Futures user-data-stream ACCOUNT_UPDATE /
# ORDER_TRADE_UPDATE payloads) follow the long-stable, well-documented
# event schema, not live-verified in this environment — see
# ARCHITECTURE_AUDIT.md. Every handler is defensive (skip on missing
# fields, never raise past the caller) for the same reason as
# yonixalpha_core.solana.token_program: a shape mismatch should mean
# "missed this update," never a crashed stream processor.


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


async def handle_account_update(session: AsyncSession, message: dict) -> None:
    """ACCOUNT_UPDATE carries an embedded snapshot of every position the
    update touched (message["a"]["P"]) — upserted the same way
    positions.sync_positions() does, so a full REST resync and a
    stream-pushed update never disagree on shape.
    """
    positions = message.get("a", {}).get("P", [])
    for p in positions:
        symbol = p.get("s")
        position_amt = _to_decimal(p.get("pa"))
        if not symbol or position_amt is None:
            continue
        values = dict(
            symbol=symbol,
            position_amt=position_amt,
            entry_price=_to_decimal(p.get("ep")),
            unrealized_pnl=_to_decimal(p.get("up")),
            margin_type=p.get("mt"),
        )
        stmt = insert(Position).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["symbol"], set_=values)
        await session.execute(stmt)
    if positions:
        await session.commit()
        log.info("events.account_update", symbols=[p.get("s") for p in positions if p.get("s")])


async def handle_order_trade_update(session: AsyncSession, message: dict) -> None:
    """ORDER_TRADE_UPDATE fires on every order-state change, including
    each individual fill (execution type "TRADE"). Updates the matching
    local Order (found by client_order_id — the same id we generated in
    orders.place_order_idempotent) and, for a fill, inserts a Fill row
    idempotently (dedup via the unique exchange_trade_id).
    """
    o = message.get("o", {})
    client_order_id = o.get("c")
    if not client_order_id:
        return

    result = await session.execute(select(Order).where(Order.client_order_id == client_order_id))
    order = result.scalar_one_or_none()
    if order is None:
        log.warning("events.order_update_for_unknown_client_order_id", client_order_id=client_order_id)
        return

    order.status = o.get("X", order.status)
    if o.get("i") is not None:
        order.exchange_order_id = str(o["i"])

    execution_type = o.get("x")
    trade_id = o.get("t")
    if execution_type == "TRADE" and trade_id is not None:
        trade_time = o.get("T")
        occurred_at = (
            datetime.fromtimestamp(trade_time / 1000, tz=timezone.utc) if trade_time else datetime.now(timezone.utc)
        )
        fill_stmt = (
            insert(Fill)
            .values(
                order_id=order.id,
                exchange_trade_id=str(trade_id),
                symbol=o.get("s", order.symbol),
                side=o.get("S", order.side),
                price=_to_decimal(o.get("L")) or Decimal(0),
                quantity=_to_decimal(o.get("l")) or Decimal(0),
                commission=_to_decimal(o.get("n")),
                commission_asset=o.get("N"),
                realized_pnl=_to_decimal(o.get("rp")),
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["exchange_trade_id"])
        )
        await session.execute(fill_stmt)

    await session.commit()
    log.info("events.order_trade_update", client_order_id=client_order_id, status=order.status, execution_type=execution_type)


async def dispatch_user_stream_message(session: AsyncSession, message: dict) -> None:
    event_type = message.get("e")
    if event_type == "ACCOUNT_UPDATE":
        await handle_account_update(session, message)
    elif event_type == "ORDER_TRADE_UPDATE":
        await handle_order_trade_update(session, message)
    # Other event types (MARGIN_CALL, listenKeyExpired, ...) are
    # intentionally unhandled for now rather than guessed at.
