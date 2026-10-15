"""Simulated execution. Fills happen in this database only; there is no
code here that could reach an exchange.

Fill model (spot, market order):
- buy at the ask, sell at the bid, each moved against the order by
  `slippage_bps`, then rounded to the tick against the order;
- taker fee charged in the quote currency;
- an order larger than the displayed top-of-book size is REJECTED (a
  partial fill across book levels is not simulated, so it is not assumed);
- `client_id` is unique: the same decision never fills twice.
Every call checks require_demo() first."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto.db import DemoAccount, DemoOrder, DemoPosition
from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook
from yonixalpha_crypto.guard import require_demo
from yonixalpha_crypto.risk import BPS, Exposure

DEFAULT_DEMO_BALANCE = Decimal("1000")


@dataclass(frozen=True)
class Fill:
    price: Decimal
    qty: Decimal
    quote_amount: Decimal
    fee_quote: Decimal


def simulate(side: str, book: TopOfBook, spec: InstrumentSpec, qty: Decimal, fee_bps: Decimal,
             slippage_bps: Decimal, top_size: Decimal | None) -> tuple[Fill | None, str | None]:
    if qty <= 0:
        return None, "quantity is zero"
    if top_size is not None and qty > top_size:
        return None, f"order {qty} larger than the top-of-book size {top_size} (partial fills not simulated)"
    slip = slippage_bps / BPS
    if side == "BUY":
        raw = book.ask * (1 + slip)
        price = spec.round_price(raw, up=True) if spec.tick_size > 0 else raw
    else:
        raw = book.bid * (1 - slip)
        price = spec.round_price(raw, up=False) if spec.tick_size > 0 else raw
    quote = qty * price
    return Fill(price, qty, quote, quote * fee_bps / BPS), None


async def account(session: AsyncSession, exchange: str, quote: str) -> DemoAccount:
    acc = (await session.execute(select(DemoAccount).where(DemoAccount.exchange == exchange,
                                                           DemoAccount.quote == quote))).scalar_one_or_none()
    if acc is None:
        acc = DemoAccount(exchange=exchange, quote=quote, starting_balance=DEFAULT_DEMO_BALANCE,
                          cash=DEFAULT_DEMO_BALANCE)
        session.add(acc)
        await session.flush()
    return acc


async def exposure(session: AsyncSession, strategy_id: str, exchange: str, quote: str, now: datetime) -> Exposure:
    P = DemoPosition
    open_rows = (await session.execute(select(func.count(), func.coalesce(func.sum(P.entry_cost_quote), 0)).where(
        P.strategy_id == strategy_id, P.exchange == exchange, P.status == "OPEN"))).one()
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = (await session.execute(select(func.coalesce(func.sum(P.realized_pnl_quote), 0)).where(
        P.strategy_id == strategy_id, P.exchange == exchange, P.status == "CLOSED", P.closed_at >= day))).scalar_one()
    acc = await account(session, exchange, quote)
    return Exposure(int(open_rows[0]), Decimal(open_rows[1]), Decimal(realized), Decimal(acc.cash))


async def _existing(session: AsyncSession, client_id: str) -> DemoOrder | None:
    return (await session.execute(select(DemoOrder).where(DemoOrder.client_id == client_id))).scalar_one_or_none()


def _book(book: TopOfBook) -> dict:
    return {"bid": str(book.bid), "ask": str(book.ask), "at": book.at.isoformat()}


async def open_long(session: AsyncSession, *, mode: str, strategy_id: str, client_id: str, book: TopOfBook,
                    spec: InstrumentSpec, qty: Decimal, fee_bps: Decimal, slippage_bps: Decimal,
                    stop_price: Decimal | None, target_price: Decimal | None, top_size: Decimal | None,
                    signal_id=None, now: datetime | None = None, stop_pct: Decimal | None = None,
                    target_pct: Decimal | None = None) -> DemoOrder:
    """stop_pct / target_pct: re-anchor the stop and target on the simulated
    fill price (as a strategy that sets them from the entry price expects)."""
    require_demo(mode)
    prior = await _existing(session, client_id)
    if prior is not None:
        return prior
    now = now or datetime.now(timezone.utc)
    fill, why = simulate("BUY", book, spec, qty, fee_bps, slippage_bps, top_size)
    acc = await account(session, spec.exchange, spec.quote)
    if fill is not None and fill.quote_amount + fill.fee_quote > acc.cash:
        fill, why = None, f"demo cash {acc.cash} below the order cost"
    order = DemoOrder(client_id=client_id, strategy_id=strategy_id, exchange=spec.exchange, symbol=spec.symbol,
                      side="BUY", status="FILLED" if fill else "REJECTED", reason=why, signal_id=signal_id,
                      slippage_bps=slippage_bps, spread_bps=book.spread_bps, book=_book(book), created_at=now)
    if fill is not None:
        order.qty, order.price, order.quote_amount, order.fee_quote = fill.qty, fill.price, fill.quote_amount, fill.fee_quote
        acc.cash = Decimal(acc.cash) - fill.quote_amount - fill.fee_quote
        if stop_pct is not None:
            stop_price = fill.price * (1 - stop_pct)
        if target_pct is not None:
            target_price = fill.price * (1 + target_pct)
        session.add(DemoPosition(strategy_id=strategy_id, exchange=spec.exchange, symbol=spec.symbol, qty=fill.qty,
                                 entry_price=fill.price, entry_cost_quote=fill.quote_amount + fill.fee_quote,
                                 stop_price=stop_price, target_price=target_price, fees_quote=fill.fee_quote,
                                 last_price=fill.price, opened_at=now))
    session.add(order)
    await session.flush()
    return order


async def close_long(session: AsyncSession, *, mode: str, position: DemoPosition, client_id: str, book: TopOfBook,
                     spec: InstrumentSpec, fee_bps: Decimal, slippage_bps: Decimal, reason: str,
                     now: datetime | None = None) -> DemoOrder:
    """Exits are never refused for size: a demo exit at the bid (minus
    slippage) is always recorded, so a position cannot be left unprotected."""
    require_demo(mode)
    prior = await _existing(session, client_id)
    if prior is not None:
        return prior
    now = now or datetime.now(timezone.utc)
    fill, why = simulate("SELL", book, spec, Decimal(position.qty), fee_bps, slippage_bps, None)
    if fill is None:
        raise ValueError(f"cannot close position {position.id}: {why}")
    acc = await account(session, spec.exchange, spec.quote)
    proceeds = fill.quote_amount - fill.fee_quote
    acc.cash = Decimal(acc.cash) + proceeds
    position.status, position.closed_at, position.exit_price, position.exit_reason = "CLOSED", now, fill.price, reason
    position.proceeds_quote = proceeds
    position.fees_quote = Decimal(position.fees_quote) + fill.fee_quote
    position.realized_pnl_quote = proceeds - Decimal(position.entry_cost_quote)
    position.last_price = fill.price
    order = DemoOrder(client_id=client_id, strategy_id=position.strategy_id, exchange=spec.exchange,
                      symbol=spec.symbol, side="SELL", status="FILLED", reason=reason, qty=fill.qty, price=fill.price,
                      quote_amount=fill.quote_amount, fee_quote=fill.fee_quote, slippage_bps=slippage_bps,
                      spread_bps=book.spread_bps, book=_book(book), created_at=now)
    session.add(order)
    await session.flush()
    return order
