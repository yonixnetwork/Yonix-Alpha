"""Order execution, protection, reconciliation and recovery for one exchange
account connection (spot, LONG). The exchange is the authority: local rows
record intent and the last state the exchange confirmed.

Invariants:
- Write-ahead: a trade and each order are committed (PENDING_SEND) before
  the request is sent, under a client id the exchange stores too.
- No blind retries: a request with no answer (timeout, 5xx) becomes UNKNOWN
  and is resolved only by looking its client id up. A market order is never
  sent a second time; Binance accepts a reused client id once the first
  order is no longer open, so re-sending could double an entry.
- A stop is "in place" only when the exchange reports the order as open
  (VERIFIED). A sent request proves nothing.
- Anything uncertain (unknown order, missing protection, a balance below
  what the trade should hold) blocks NEW entries on the connection and
  raises an ERROR event; exits and protection continue. The block lifts
  when a reconciliation finds everything consistent again.
- Only one non-final trade per connection and symbol, and a signal
  (entry_key) can open at most one trade, also across restarts.
- LIVE connections pass livelock.require_live_orders() before an entry,
  which refuses while LIVE_ORDERS_UNLOCKED is False (the signing client
  checks again for every order request).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto.db import EXEC_ACTIVE, ApiConnection, Event, ExecGuard, ExecOrder, ExecTrade, TradeAction
from yonixalpha_crypto.exchanges.base import InstrumentSpec, split_symbol
from yonixalpha_crypto.private import livelock, management
from yonixalpha_crypto.private.adapters import (CANCELED, EXPIRED, FILLED, OPEN, PARTIAL, REJECTED, OrderState,
                                                ProtectionIds, SpotAccount)
from yonixalpha_crypto.private.connections import view as connection_view
from yonixalpha_crypto.private.environments import LIVE
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError
from yonixalpha_crypto.private.sizing import SizingRules, size_long

ACTIVE_TRADE = EXEC_ACTIVE
LIVE_ORDER = ("PENDING_SEND", "UNKNOWN", OPEN, PARTIAL)
UNKNOWN_GRACE_S = 60  # a request with no answer and no order on the exchange after this long never arrived
BPS = Decimal(10000)


class EntryRefused(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Executor:
    def __init__(self, account: SpotAccount, connection: ApiConnection, *, clock: Callable[[], datetime] = _now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, poll_attempts: int = 5,
                 poll_delay_s: float = 0.5, unknown_grace_s: float = UNKNOWN_GRACE_S):
        if account.exchange != connection.exchange or account.environment != connection.environment:
            raise ValueError("account and connection do not match")
        self.acct, self.conn = account, connection
        self.clock, self.sleep = clock, sleep
        self.poll_attempts, self.poll_delay_s, self.grace = poll_attempts, poll_delay_s, unknown_grace_s
        self._specs: dict[str, InstrumentSpec] = {}

    # ---- helpers -----------------------------------------------------------

    async def spec(self, symbol: str) -> InstrumentSpec:
        if symbol not in self._specs:
            self._specs[symbol] = await self.acct.instrument(symbol)
        return self._specs[symbol]

    @staticmethod
    def cid(trade: ExecTrade, code: str, n: int) -> str:
        return f"yx{trade.id.hex[:12]}{code}{n}"

    async def _action(self, s: AsyncSession, trade: ExecTrade, action: str, reason: str, detail: dict | None = None) -> None:
        s.add(TradeAction(trade_id=trade.id, at=self.clock(), action=action, reason=reason[:2000],
                          detail={k: (str(v) if isinstance(v, Decimal) else v) for k, v in (detail or {}).items()}))

    async def _event(self, s: AsyncSession, level: str, message: str, detail: dict | None = None) -> None:
        s.add(Event(at=self.clock(), level=level, source="execution", message=message[:2000],
                    detail={"connection": str(self.conn.id), "exchange": self.conn.exchange,
                            "environment": self.conn.environment, **(detail or {})}))

    async def guard(self, s: AsyncSession, lock: bool = False) -> ExecGuard:
        q = select(ExecGuard).where(ExecGuard.connection_id == self.conn.id)
        g = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
        if g is None:
            g = ExecGuard(connection_id=self.conn.id, entries_blocked=False, updated_at=self.clock())
            s.add(g)
            await s.flush()
        return g

    async def block(self, s: AsyncSession, reason: str) -> None:
        g = await self.guard(s)
        if not g.entries_blocked or g.reason != reason:
            await self._event(s, "ERROR", f"new entries blocked: {reason}")
        g.entries_blocked, g.reason, g.updated_at = True, reason, self.clock()
        g.since = g.since or self.clock()

    async def _new_order(self, s: AsyncSession, trade: ExecTrade, cid: str, role: str, side: str, kind: str,
                         qty: Decimal, price: Decimal | None = None, trigger: Decimal | None = None) -> ExecOrder:
        o = ExecOrder(client_id=cid, trade_id=trade.id, role=role, side=side, kind=kind, qty=qty, price=price,
                      trigger_price=trigger, status="PENDING_SEND", active=True, created_at=self.clock(),
                      updated_at=self.clock())
        s.add(o)
        return o

    def _apply(self, o: ExecOrder, st: OrderState | None) -> None:
        o.checked_at = o.updated_at = self.clock()
        if st is None:
            return
        o.status, o.filled_qty, o.avg_price = st.status, st.filled_qty, st.avg_price
        o.fee, o.fee_asset, o.exchange_id = st.fee, st.fee_asset, st.exchange_id or o.exchange_id

    async def _lookup(self, symbol: str, cid: str, until_final: bool = False) -> OrderState | None:
        st = None
        for i in range(self.poll_attempts):
            st = await self.acct.get_order(symbol, cid)
            if st is not None and (st.terminal or not until_final):
                return st
            if i + 1 < self.poll_attempts:
                await self.sleep(self.poll_delay_s)
        return st

    async def _send(self, s: AsyncSession, o: ExecOrder, symbol: str, fn: Callable[[], Awaitable[Any]]) -> bool:
        """Send one write-ahead order. False when the outcome is unknown (the
        order is then UNKNOWN and connection entries are blocked)."""
        try:
            await fn()
        except ExchangeRequestError as e:
            o.error, o.updated_at = str(e)[:500], self.clock()
            if e.ambiguous:
                o.status = "UNKNOWN"
                await self.block(s, f"order {o.client_id} sent with no answer ({e.status or 'timeout'}); looking it up")
                return False
            o.status = REJECTED
            return True
        return True

    # ---- entry -------------------------------------------------------------

    async def open_long(self, s: AsyncSession, *, strategy_id: str, symbol: str, entry_key: str | None,
                        rules: SizingRules, stop_price: Decimal, take_profit: Decimal | None,
                        plan: dict[str, Any] | None, thesis: dict[str, Any],
                        pre_trade_check: Callable[[Decimal, Decimal], Awaitable[list[str]]] | None = None,
                        ) -> ExecTrade:
        """Size, record, send and protect one LONG entry. Raises EntryRefused
        (nothing sent) when any check fails. pre_trade_check(planned_loss,
        notional) runs on the FINAL size, before anything is recorded or sent:
        the caller's portfolio and strategy limits see the real numbers."""
        if self.conn.environment == LIVE:
            livelock.require_live_orders(connection_view(self.conn))
        if not self.conn.enabled:
            raise EntryRefused(["the connection is disabled"])
        g = await self.guard(s, lock=True)
        if g.entries_blocked:
            raise EntryRefused([f"entries are blocked: {g.reason}"])
        busy = (await s.execute(select(ExecTrade.id).where(
            ExecTrade.connection_id == self.conn.id, ExecTrade.symbol == symbol,
            ExecTrade.status.in_(ACTIVE_TRADE)))).first()
        if busy:
            raise EntryRefused([f"a {symbol} trade is already active on this connection"])
        if entry_key and (await s.execute(select(ExecTrade.id).where(ExecTrade.entry_key == entry_key))).first():
            raise EntryRefused([f"signal {entry_key} already opened a trade"])
        try:
            plan = management.validate_plan(plan)
        except management.PlanError as e:
            raise EntryRefused([f"plan: {e}"]) from None

        spec = await self.spec(symbol)
        book = await self.acct.book(symbol)
        bal = await self.acct.balances()
        base, quote = split_symbol(symbol)
        qb, bb = bal.get(quote), bal.get(base)
        equity = (qb.total if qb else Decimal(0)) + (bb.total * book.bid if bb else Decimal(0))
        sized = size_long(rules, spec, equity_quote=equity, free_quote=qb.free if qb else Decimal(0),
                          entry_price=book.ask, stop_price=stop_price)
        if not sized.approved:
            raise EntryRefused(sized.reasons)
        if take_profit is not None:
            take_profit = spec.round_price(take_profit, up=True)
            if take_profit <= book.ask:
                raise EntryRefused([f"target {take_profit} is not above the ask {book.ask}"])
        if pre_trade_check is not None:
            refused = await pre_trade_check(sized.planned_loss, sized.notional)
            if refused:
                raise EntryRefused(refused)
        plan["engine_take_profit"] = take_profit is not None and not self.acct.exchange_take_profit
        plan["taker_fee_bps"] = str(rules.taker_fee_bps)

        now = self.clock()
        trade = ExecTrade(connection_id=self.conn.id, exchange=self.conn.exchange, environment=self.conn.environment,
                          strategy_id=strategy_id, symbol=symbol, entry_key=entry_key, status="PENDING_ENTRY",
                          protection="NONE", protection_seq=0, qty_planned=sized.qty, qty=Decimal(0),
                          initial_stop=sized.stop, stop=sized.stop, stop_limit=sized.stop_limit, take_profit=take_profit,
                          risk_budget=sized.budget, planned_loss=sized.planned_loss, plan=plan, thesis=thesis,
                          realized_quote=Decimal(0), fees_quote=Decimal(0), created_at=now, updated_at=now)
        s.add(trade)
        await s.flush()
        o = await self._new_order(s, trade, self.cid(trade, "e", 1), "ENTRY", "BUY", "MARKET", sized.qty)
        await self._action(s, trade, "ENTRY_PLANNED", f"{strategy_id}: {thesis.get('reason', 'entry signal')}", {
            "qty": sized.qty, "ask": book.ask, "stop": sized.stop, "stop_limit": sized.stop_limit,
            "target": take_profit, "equity": equity, "budget": sized.budget, "planned_loss": sized.planned_loss,
            "notes": sized.reasons, "exchange_take_profit": self.acct.exchange_take_profit})
        await s.commit()  # write-ahead: the intent survives a crash from here on

        st: OrderState | None = None

        async def send():
            nonlocal st
            st = await self.acct.market(symbol, "BUY", sized.qty, o.client_id)

        if await self._send(s, o, symbol, send):
            if o.status == REJECTED:
                trade.status, trade.close_reason, trade.updated_at = "FAILED", f"entry rejected: {o.error}", self.clock()
                await self._action(s, trade, "ENTRY_REJECTED", o.error or "rejected by the exchange")
            else:
                if st is None or not st.terminal:
                    st = await self._lookup(symbol, o.client_id, until_final=True) or st
                self._apply(o, st)
                if st is None:
                    o.status = "UNKNOWN"
                    await self.block(s, f"entry {o.client_id} accepted but not visible yet")
                await self._after_entry(s, trade, o)
        await s.commit()
        return trade

    async def _after_entry(self, s: AsyncSession, trade: ExecTrade, o: ExecOrder) -> None:
        if o.status in (FILLED, CANCELED, EXPIRED, REJECTED, "NOT_FOUND") and o.filled_qty > 0:
            base, _ = split_symbol(trade.symbol)
            held = o.filled_qty - (o.fee if o.fee_asset == base else Decimal(0))
            trade.qty, trade.entry_price = held, o.avg_price
            trade.highest_price = o.avg_price
            trade.status, trade.opened_at, trade.updated_at = "OPEN", self.clock(), self.clock()
            cost = (o.avg_price or Decimal(0)) * o.filled_qty
            trade.thesis = {**trade.thesis, "cost_per_unit": str(cost / held) if held > 0 else None}
            await self._action(s, trade, "ENTRY_FILLED", f"bought {o.filled_qty} at {o.avg_price}"
                               + (" (partial fill)" if o.filled_qty < o.qty else ""),
                               {"held_after_fee": held, "fee": o.fee, "fee_asset": o.fee_asset})
            await self.protect(s, trade)
        elif o.status in (CANCELED, EXPIRED, REJECTED, "NOT_FOUND"):
            trade.status, trade.updated_at = "FAILED", self.clock()
            trade.close_reason = f"entry {o.status.lower()} with nothing filled"
            await self._action(s, trade, "ENTRY_FAILED", trade.close_reason)

    # ---- protection --------------------------------------------------------

    def _ids(self, trade: ExecTrade) -> ProtectionIds:
        n = trade.protection_seq
        if self.acct.exchange_take_profit and trade.take_profit is not None:
            return ProtectionIds(self.cid(trade, "s", n), self.cid(trade, "t", n), self.cid(trade, "l", n))
        return ProtectionIds(self.cid(trade, "s", n))

    async def protect(self, s: AsyncSession, trade: ExecTrade) -> bool:
        """Place the protective order(s) for what the trade holds, then
        confirm them on the exchange. On failure: block entries and close."""
        spec = await self.spec(trade.symbol)
        base, _ = split_symbol(trade.symbol)
        free = (await self.acct.balances()).get(base)
        qty = spec.round_qty(min(trade.qty, free.free if free else Decimal(0)))
        if qty <= 0 or qty < spec.min_qty or qty * trade.stop_limit < spec.min_notional:
            await self._action(s, trade, "PROTECTION_IMPOSSIBLE",
                               f"held {qty} is below the exchange minimum: no stop can be placed", {"qty": qty})
            return await self._unprotected(s, trade, "position below the minimum order size")
        trade.protection_seq += 1
        ids = self._ids(trade)
        tp = trade.take_profit if ids.take_profit else None
        orders = [await self._new_order(s, trade, ids.stop, "STOP", "SELL", "STOP_LOSS_LIMIT", qty, trade.stop_limit, trade.stop)]
        if ids.take_profit:
            orders.append(await self._new_order(s, trade, ids.take_profit, "TP", "SELL", "LIMIT_MAKER", qty, tp))
        trade.protection, trade.updated_at = "PENDING", self.clock()
        await s.commit()
        ok = await self._send(s, orders[0], trade.symbol, lambda: self.acct.place_protection(
            trade.symbol, qty, trade.stop, trade.stop_limit, tp, ids))
        if len(orders) > 1:
            orders[1].status, orders[1].error = orders[0].status, orders[0].error
        if ok and orders[0].status != REJECTED:
            return await self.verify_protection(s, trade)
        if not ok:
            for o in orders[1:]:
                o.status = "UNKNOWN"
            return False  # resolved by the reconciler
        await self._action(s, trade, "PROTECTION_REJECTED", orders[0].error or "rejected")
        return await self._unprotected(s, trade, f"stop rejected: {orders[0].error}")

    async def verify_protection(self, s: AsyncSession, trade: ExecTrade) -> bool:
        active = (await s.execute(select(ExecOrder).where(ExecOrder.trade_id == trade.id, ExecOrder.active.is_(True),
                                                          ExecOrder.role.in_(("STOP", "TP"))))).scalars().all()
        ok = bool(active)
        for o in active:
            st = await self._lookup(trade.symbol, o.client_id)
            self._apply(o, st)
            if st is None or st.status not in (OPEN, PARTIAL):
                ok = False
        if ok:
            trade.protection, trade.updated_at = "VERIFIED", self.clock()
            await self._action(s, trade, "PROTECTION_VERIFIED", "the exchange reports the stop"
                               + (" and target" if len(active) > 1 else "") + " as open",
                               {o.role: o.client_id for o in active} | {"stop": trade.stop})
            return True
        return await self._unprotected(s, trade, "the exchange does not report the protective order as open",
                                       {o.client_id: o.status for o in active})

    async def _unprotected(self, s: AsyncSession, trade: ExecTrade, why: str, detail: dict | None = None) -> bool:
        trade.protection = "MISSING"
        await self.block(s, f"{trade.symbol} trade {trade.id.hex[:8]} unprotected: {why}")
        await self._action(s, trade, "PROTECTION_MISSING", why + " -> closing the position", detail)
        await self.close(s, trade, f"protection could not be confirmed: {why}")
        return False

    async def _cancel_protection(self, s: AsyncSession, trade: ExecTrade) -> bool:
        """Cancel the current protection and confirm it is gone (or filled)."""
        active = (await s.execute(select(ExecOrder).where(ExecOrder.trade_id == trade.id, ExecOrder.active.is_(True),
                                                          ExecOrder.role.in_(("STOP", "TP"))))).scalars().all()
        live = [o for o in active if o.status in LIVE_ORDER]
        if live:
            stop = next((o for o in active if o.role == "STOP"), None)
            ids = ProtectionIds(stop.client_id if stop else live[0].client_id,
                                next((o.client_id for o in active if o.role == "TP"), None),
                                self.cid(trade, "l", trade.protection_seq) if any(o.role == "TP" for o in active) else None)
            try:
                await self.acct.cancel_protection(trade.symbol, ids)
            except ExchangeRequestError as e:
                await self._action(s, trade, "CANCEL_FAILED", str(e))
                return False
        for o in active:
            st = await self._lookup(trade.symbol, o.client_id)
            self._apply(o, st)
            if o.status in (OPEN, PARTIAL):
                await self._action(s, trade, "CANCEL_UNCONFIRMED", f"{o.client_id} still open after cancel")
                return False
            o.active = False
        trade.protection = "NONE"
        return True

    # ---- exits -------------------------------------------------------------

    async def _sell(self, s: AsyncSession, trade: ExecTrade, qty: Decimal, code: str, role: str) -> ExecOrder | None:
        n = 1 + len((await s.execute(select(ExecOrder.client_id).where(ExecOrder.trade_id == trade.id,
                                                                       ExecOrder.role == role))).all())
        o = await self._new_order(s, trade, self.cid(trade, code, n), role, "SELL", "MARKET", qty)
        await s.commit()
        st: OrderState | None = None

        async def send():
            nonlocal st
            st = await self.acct.market(trade.symbol, "SELL", qty, o.client_id)

        if not await self._send(s, o, trade.symbol, send):
            return o
        if o.status == REJECTED:
            await self.block(s, f"exit {o.client_id} rejected: {o.error}")
            return o
        if st is None or not st.terminal:
            st = await self._lookup(trade.symbol, o.client_id, until_final=True) or st
        self._apply(o, st)
        if st is None:
            o.status = "UNKNOWN"
            await self.block(s, f"exit {o.client_id} accepted but not visible yet")
        return o

    def _book_exit(self, trade: ExecTrade, o: ExecOrder) -> Decimal:
        """Realized P&L of a filled sell (quote), against the entry cost per held unit."""
        _, quote = split_symbol(trade.symbol)
        cpu = Decimal(trade.thesis.get("cost_per_unit") or trade.entry_price or 0)
        fee_q = o.fee if o.fee_asset == quote else Decimal(0)
        pnl = (o.avg_price or Decimal(0)) * o.filled_qty - fee_q - cpu * o.filled_qty
        trade.realized_quote += pnl
        trade.fees_quote += fee_q
        trade.qty = max(Decimal(0), trade.qty - o.filled_qty)
        return pnl

    async def close(self, s: AsyncSession, trade: ExecTrade, reason: str) -> None:
        if trade.status in ("CLOSED", "FAILED"):
            return
        trade.status, trade.updated_at = "CLOSING", self.clock()
        await self._action(s, trade, "CLOSE", reason)
        await s.commit()
        if not await self._cancel_protection(s, trade):
            await self.block(s, f"{trade.symbol}: could not confirm the stop was cancelled before closing")
            await s.commit()
            return
        await self._finish_close(s, trade, reason)
        await s.commit()

    async def _finish_close(self, s: AsyncSession, trade: ExecTrade, reason: str) -> None:
        spec = await self.spec(trade.symbol)
        base, _ = split_symbol(trade.symbol)
        b = (await self.acct.balances()).get(base)
        qty = spec.round_qty(min(trade.qty, b.free if b else Decimal(0)))
        if qty <= 0 or qty < spec.min_qty:
            trade.status, trade.closed_at, trade.close_reason = "CLOSED", self.clock(), f"{reason} (remaining {trade.qty} below the minimum: dust)"
            await self._action(s, trade, "CLOSED", trade.close_reason)
            return
        o = await self._sell(s, trade, qty, "x", "EXIT")
        if o is not None and o.status == FILLED:
            pnl = self._book_exit(trade, o)
            trade.status, trade.closed_at, trade.close_reason = "CLOSED", self.clock(), reason
            await self._action(s, trade, "CLOSED", f"sold {o.filled_qty} at {o.avg_price}: {reason}",
                               {"pnl": pnl, "realized_total": trade.realized_quote})

    async def partial_exit(self, s: AsyncSession, trade: ExecTrade, index: int, fraction: Decimal, reason: str) -> None:
        spec = await self.spec(trade.symbol)
        qty = spec.round_qty(trade.qty * fraction)
        rest = trade.qty - qty
        if qty < spec.min_qty or rest < spec.min_qty or qty * trade.stop < spec.min_notional:
            await self._action(s, trade, "PARTIAL_SKIPPED", f"{reason}; skipped: part or remainder below the minimum size")
            self._mark_partial(trade, index)
            return
        await self._action(s, trade, "PARTIAL_EXIT", reason + " (the stop is replaced for the remainder; brief gap)")
        if not await self._cancel_protection(s, trade):
            await self.block(s, f"{trade.symbol}: could not confirm the stop was cancelled before a partial exit")
            return
        o = await self._sell(s, trade, qty, "p", "PARTIAL")
        if o is not None and o.status == FILLED:
            pnl = self._book_exit(trade, o)
            self._mark_partial(trade, index)
            await self._action(s, trade, "PARTIAL_FILLED", f"sold {o.filled_qty} at {o.avg_price}", {"pnl": pnl})
        await self.protect(s, trade)  # re-protect whatever is held, in every case

    @staticmethod
    def _mark_partial(trade: ExecTrade, index: int) -> None:
        plan = dict(trade.plan)
        parts = [dict(p) for p in plan.get("partials") or []]
        if 0 <= index < len(parts):
            parts[index]["done"] = True
        plan["partials"] = parts
        trade.plan = plan

    async def move_stop(self, s: AsyncSession, trade: ExecTrade, new_stop: Decimal, reason: str) -> None:
        if new_stop <= trade.stop:
            await self._action(s, trade, "STOP_NOT_MOVED", f"refused: {new_stop} would not tighten the stop {trade.stop}")
            return
        spec = await self.spec(trade.symbol)
        gap = Decimal(str(trade.plan.get("min_stop_gap_bps", "20")))
        limit_gap = (trade.stop - trade.stop_limit) / trade.stop if trade.stop > 0 else gap / BPS
        new_limit = (new_stop * (1 - limit_gap) / spec.tick_size).to_integral_value(rounding=ROUND_DOWN) * spec.tick_size
        old = trade.stop
        if self.acct.stop_amendable:
            stop_o = (await s.execute(select(ExecOrder).where(ExecOrder.trade_id == trade.id, ExecOrder.active.is_(True),
                                                              ExecOrder.role == "STOP"))).scalar_one_or_none()
            if stop_o is None:
                return
            try:
                await self.acct.amend_stop(trade.symbol, self._ids(trade), new_stop, new_limit)
            except ExchangeRequestError as e:
                await self._action(s, trade, "STOP_AMEND_FAILED", str(e))
                if e.ambiguous:
                    await self.block(s, f"stop amend for {trade.symbol} had no answer")
                return
            stop_o.trigger_price, stop_o.price = new_stop, new_limit
            trade.stop, trade.stop_limit = new_stop, new_limit
            ok = await self.verify_protection(s, trade)
        else:
            if not await self._cancel_protection(s, trade):
                await self.block(s, f"{trade.symbol}: could not confirm the old stop was cancelled")
                return
            trade.stop, trade.stop_limit = new_stop, new_limit
            ok = await self.protect(s, trade)
        await self._action(s, trade, "STOP_MOVED" if ok else "STOP_MOVE_FAILED", f"{reason}: {old} -> {new_stop}",
                           {"amended_in_place": self.acct.stop_amendable})

    # ---- management --------------------------------------------------------

    async def manage(self, s: AsyncSession, trade: ExecTrade, market: management.MarketSnapshot,
                     bars_held: int, fee_bps: Decimal, record_hold: bool = True) -> list[management.Decision]:
        if trade.status != "OPEN" or trade.entry_price is None:
            return []
        spec = await self.spec(trade.symbol)
        hi = max([trade.highest_price or trade.entry_price, market.bid] + [b.high for b in market.bars[-1:]])
        trade.highest_price = hi
        snap = management.TradeSnapshot(trade.entry_price, trade.initial_stop, trade.stop, trade.qty, trade.take_profit,
                                        hi, bars_held, trade.plan, fee_bps)
        decisions = management.decide(snap, market, spec.tick_size)
        for d in decisions:
            if d.action in (management.HOLD, management.ALERT):
                if d.action == management.ALERT or record_hold:
                    await self._action(s, trade, d.action, d.reason, d.detail)
                if d.action == management.ALERT:
                    await self._event(s, "WARN", f"{trade.symbol}: {d.reason}")
            elif d.action == management.CLOSE:
                await self.close(s, trade, d.reason)
                break
            elif d.action == management.PARTIAL_EXIT:
                await self.partial_exit(s, trade, int(d.detail["index"]), Decimal(d.detail["fraction"]), d.reason)
            elif d.action == management.MOVE_STOP and trade.status == "OPEN":
                await self.move_stop(s, trade, Decimal(d.detail["new_stop"]), d.reason)
        await s.commit()
        return decisions

    # ---- reconciliation ----------------------------------------------------

    async def reconcile(self, s: AsyncSession) -> dict[str, Any]:
        """Bring every non-final trade on this connection in line with the
        exchange. Safe to run at any time, repeatedly, and after a restart."""
        now = self.clock()
        trades = (await s.execute(select(ExecTrade).where(ExecTrade.connection_id == self.conn.id,
                                                          ExecTrade.status.in_(ACTIVE_TRADE)))).scalars().all()
        problems: list[str] = []
        for trade in trades:
            orders = (await s.execute(select(ExecOrder).where(ExecOrder.trade_id == trade.id))).scalars().all()
            for o in orders:
                if o.status not in LIVE_ORDER:
                    continue
                st = await self.acct.get_order(trade.symbol, o.client_id)
                if st is None:
                    age = (now - o.created_at).total_seconds()
                    if o.status in ("PENDING_SEND", "UNKNOWN") and age >= self.grace:
                        o.status, o.updated_at = "NOT_FOUND", now
                        await self._action(s, trade, "ORDER_NOT_FOUND", f"{o.role} {o.client_id} never reached the exchange")
                    elif o.status in (OPEN, PARTIAL):
                        o.status = "UNKNOWN"
                        problems.append(f"{o.client_id} was open and is no longer reported")
                    else:
                        problems.append(f"{o.client_id} not visible yet")
                    continue
                before = o.status
                self._apply(o, st)
                if before != o.status:
                    await self._action(s, trade, "ORDER_UPDATE", f"{o.role} {o.client_id}: {before} -> {o.status}",
                                       {"filled": o.filled_qty, "avg": o.avg_price})
            await self._reconcile_trade(s, trade, orders, problems)
        g = await self.guard(s)
        if problems:
            await self.block(s, "; ".join(problems)[:500])
        elif g.entries_blocked:
            g.entries_blocked, g.reason, g.since, g.updated_at = False, None, None, now
            await self._event(s, "INFO", "reconciliation consistent: new entries allowed again")
        await s.commit()
        return {"trades": len(trades), "problems": problems}

    async def _reconcile_trade(self, s: AsyncSession, trade: ExecTrade, orders: list[ExecOrder], problems: list[str]) -> None:
        by_role = {r: [o for o in orders if o.role == r] for r in ("ENTRY", "STOP", "TP", "EXIT", "PARTIAL")}
        if trade.status == "PENDING_ENTRY":
            e = by_role["ENTRY"][0] if by_role["ENTRY"] else None
            if e is None or e.status in ("UNKNOWN", "PENDING_SEND"):
                problems.append(f"entry of {trade.symbol} unresolved")
            elif e.status in (OPEN, PARTIAL):
                problems.append(f"entry of {trade.symbol} still working")
            else:
                await self._after_entry(s, trade, e)
            return
        if trade.status == "CLOSING":
            for o in by_role["EXIT"]:
                if o.status == FILLED and trade.qty > 0:
                    self._book_exit(trade, o)
            pending = [o for o in by_role["EXIT"] if o.status in LIVE_ORDER]
            if pending:
                problems.append(f"exit of {trade.symbol} unresolved")
            elif trade.qty > 0 and not any(o.status == FILLED for o in by_role["EXIT"]):
                await self._finish_close(s, trade, trade.close_reason or "closing (resumed after reconciliation)")
            else:
                trade.status, trade.closed_at = "CLOSED", trade.closed_at or self.clock()
            return
        # OPEN / UNCERTAIN: did a protective order fill?
        for role, label in (("STOP", "stop"), ("TP", "target")):
            filled = [o for o in by_role[role] if o.active and o.status == FILLED]
            if filled:
                pnl = self._book_exit(trade, filled[0])
                for o in by_role["STOP"] + by_role["TP"]:
                    o.active = False
                trade.status, trade.closed_at = "CLOSED", self.clock()
                trade.close_reason, trade.protection = f"{label} filled at {filled[0].avg_price}", "NONE"
                await self._action(s, trade, "CLOSED", trade.close_reason, {"pnl": pnl})
                return
        if any(o.active and o.status == PARTIAL for o in by_role["STOP"] + by_role["TP"]):
            problems.append(f"{trade.symbol} protective order partly filled")
            return
        active = [o for o in by_role["STOP"] + by_role["TP"] if o.active]
        if any(o.status in ("UNKNOWN", "PENDING_SEND") for o in active):
            problems.append(f"{trade.symbol} protection unresolved")
            return
        if not active or any(o.status not in (OPEN,) for o in active):
            await self._action(s, trade, "PROTECTION_LOST", "the exchange no longer reports the stop as open; replacing it",
                               {o.client_id: o.status for o in active})
            for o in active:
                o.active = False
            await self.protect(s, trade)
            return
        # The coins the trade should hold must be on the account.
        spec = await self.spec(trade.symbol)
        base, _ = split_symbol(trade.symbol)
        b = (await self.acct.balances()).get(base)
        have = b.total if b else Decimal(0)
        if have + spec.qty_step < trade.qty:
            trade.status = "UNCERTAIN"
            problems.append(f"{trade.symbol}: the account holds {have} {base}, the trade expects {trade.qty}")
            await self._action(s, trade, "BALANCE_MISMATCH", f"account holds {have}, trade expects {trade.qty}")
        elif trade.status == "UNCERTAIN":
            trade.status = "OPEN"
            await self._action(s, trade, "BALANCE_OK", "balance matches the trade again")
        if trade.protection != "VERIFIED" and trade.status == "OPEN":
            trade.protection = "VERIFIED"
