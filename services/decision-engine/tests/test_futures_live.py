"""LIVE futures lifecycle with the exchange boundary mocked.

`FakeExchange` stands in for a BinanceProvider/BybitProvider/... and
behaves like an exchange account: it fills market orders, holds a
position, keeps protective stops and reports fills. Everything above it —
the strategy signal, the safety gate, readiness, the entry order, fill
application, the exchange-side stop, exits via manage_step, realized PnL
and reconciliation — is production code. No request leaves the process.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core import external_bots, futures_live
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, PaperAccount, PaperPosition, ReconciliationEvent
from yonixalpha_core.execution.base import ExecutionError, Fill, InstrumentRules, OrderState, PositionInfo
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode

from app.futures_eval import run_strategy
from tests.test_futures_eval import NOW, FakeVenue

LIVE_ON = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, TELEGRAM_BOT_TOKEN=None,
                          TELEGRAM_CHAT_ID=None)


class FakeExchange:
    venue = "binance"
    quote_currency = "USDT"
    configured = True

    def __init__(self, balance=Decimal(1000), fill_price: Decimal | None = None, fee_bps=Decimal(5)):
        self.bal, self.fill_price, self.fee_bps = balance, fill_price, fee_bps
        self.size = Decimal(0)  # signed
        self.entry = None
        self.stops: dict[str, Decimal] = {}
        self.orders: dict[str, OrderState] = {}
        self.fills: list[Fill] = []
        self.calls: list[tuple] = []
        self.fail_stop = False
        self.mark = Decimal(3000)

    async def instrument(self, symbol):
        return InstrumentRules(symbol, Decimal("0.001"), Decimal("0.01"), Decimal("0.001"), Decimal(5))

    async def prepare(self, symbol, leverage):
        self.calls.append(("prepare", symbol, leverage))

    async def market_order(self, symbol, side, qty, reduce_only, client_id, ref_price=None):
        self.calls.append(("order", side, qty, reduce_only, client_id))
        if reduce_only and self.size == 0:
            st = OrderState(client_id, "REJECTED", error="ReduceOnly Order is rejected (-2022)")
            self.orders[client_id] = st
            return st
        px = self.fill_price or ref_price or self.mark
        fee = qty * px * self.fee_bps / 10_000
        self.size += qty if side == "BUY" else -qty
        self.entry = px if not reduce_only else self.entry
        self.fills.append(Fill(f"o{len(self.fills)}", client_id, side, qty, px, fee, None, int(NOW.timestamp() * 1000) + 1))
        st = OrderState(client_id, "FILLED", qty, px, fee, f"o{len(self.fills)}")
        self.orders[client_id] = st
        return st

    async def order_status(self, symbol, client_id):
        return self.orders.get(client_id, OrderState(client_id, "REJECTED", error="unknown"))

    async def set_stop(self, symbol, position_side, stop_price, qty, client_id):
        if self.fail_stop:
            raise ExecutionError("algo order rejected")
        sid = f"s{len(self.calls)}"
        self.calls.append(("set_stop", stop_price))
        self.stops[sid] = stop_price
        return sid

    async def cancel_stop(self, symbol, stop_id):
        self.calls.append(("cancel_stop", stop_id))
        self.stops.pop(stop_id, None)

    async def cancel_protection(self, symbol):
        self.stops.clear()

    async def open_protection(self, symbol):
        return [{"id": k, "trigger_price": str(v)} for k, v in self.stops.items()]

    async def position(self, symbol):
        return PositionInfo(symbol, self.size, self.entry, self.mark) if self.size else None

    async def balance(self):
        return self.bal

    async def fills_since(self, symbol, start_ms):
        return [f for f in self.fills if f.time_ms >= start_ms]

    def trigger_stop(self, price: Decimal):
        """The exchange's own stop fires while YonixAlpha is not looking."""
        side = "SELL" if self.size > 0 else "BUY"
        qty = abs(self.size)
        self.fills.append(Fill("stop", None, side, qty, price, qty * price * self.fee_bps / 10_000, None,
                               int(NOW.timestamp() * 1000) + 5))
        self.size = Decimal(0)
        self.stops.clear()


async def go_live(sf, redis, ex: FakeExchange):
    async with sf() as s:
        await store.set_global_mode(s, GlobalMode.LIVE, None)
        await store.set_strategy_mode(s, "meta_muse", StrategyMode.AUTO, None)
        await store.set_strategy_mode(s, "binance_futures", StrategyMode.AUTO, None)
        await s.commit()
    # First reconcile: exchange balance -> live book, readiness published.
    report = await futures_live.reconcile_venue(sf, redis, LIVE_ON, "binance", ex, NOW)
    assert report["status"] == "ready"


async def open_live(sf, redis, ex):
    await go_live(sf, redis, ex)
    r = await run_strategy(sf, redis, LIVE_ON, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "LIVE entry submitted", r
    async with sf() as s:
        order = (await s.execute(select(ExecutionOrder))).scalar_one()
    status = await futures_live.process_order(sf, redis, LIVE_ON, {"binance": ex}, order.id, now_fn=lambda: NOW)
    assert status == "CONFIRMED"
    async with sf() as s:
        return (await s.execute(select(PaperPosition))).scalar_one()


async def test_live_entry_fills_from_the_exchange_and_places_an_exchange_stop(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    p = await open_live(sf, redis_client, ex)
    assert p.execution_mode == "LIVE" and p.status == "open" and p.side == "LONG" and p.execution_provider == "binance_futures"
    assert ex.size == p.quantity and p.quantity == p.quantity.quantize(Decimal("0.001"))  # rounded to the step
    orders = [c for c in ex.calls if c[0] == "order"]
    assert orders[0][1] == "BUY" and orders[0][3] is False and orders[0][4].startswith("yx")
    assert ("prepare", "ETHUSDT", 1) in ex.calls
    assert list(ex.stops.values()) == [p.stop_loss]  # exchange-side stop at the plan's stop
    assert p.plan["venue"]["exchange_stop"] == str(p.stop_loss)
    async with sf() as s:
        acct = await s.get(PaperAccount, p.account_id)
        assert acct.name == "live_binance" and acct.cash_balance < Decimal(1000)
        o = (await s.execute(select(ExecutionOrder))).scalar_one()
        assert o.signature.startswith("yx") and o.result["status"] == "FILLED"


async def test_take_profit_exit_is_a_reduce_only_order_and_books_real_pnl(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    p = await open_live(sf, redis_client, ex)
    tp = Decimal(p.take_profit[-1])
    price = tp * Decimal("1.001")

    async def price_fn(venue, symbol):
        return price

    ex.fill_price = price
    # Ticks until every take-profit level has been sold (one pending order at a time).
    for _ in range(4):
        await futures_live.manage(sf, {"binance": ex}, price_fn, NOW + timedelta(minutes=1))
        async with sf() as s:
            pending = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.status == "PENDING"))).scalars().all()
        for o in pending:
            await futures_live.process_order(sf, redis_client, LIVE_ON, {"binance": ex}, o.id, now_fn=lambda: NOW)
    async with sf() as s:
        p = await s.get(PaperPosition, p.id)
    assert p.status == "closed" and ex.size == 0 and not ex.stops
    assert all(c[3] for c in ex.calls if c[0] == "order" and c[1] == "SELL")  # every exit reduce-only
    fees = sum(f.fee for f in ex.fills)
    gross = sum((f.price * f.qty if f.side == "SELL" else -f.price * f.qty) for f in ex.fills)
    assert abs(p.realized_pnl - (gross - fees)) < Decimal("0.0001")  # exactly what the exchange filled


async def test_trailing_stop_tightens_the_exchange_stop_before_removing_the_old_one(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    p = await open_live(sf, redis_client, ex)
    async with sf() as s:
        p = await s.get(PaperPosition, p.id)
        p.trailing_stop = p.stop_loss + (p.entry_price - p.stop_loss) / 2  # the software stop moved up
        await s.commit()
    first = dict(ex.stops)

    async def price_fn(venue, symbol):
        return p.entry_price

    await futures_live.manage(sf, {"binance": ex}, price_fn, NOW + timedelta(minutes=1))
    set_i = max(i for i, c in enumerate(ex.calls) if c[0] == "set_stop")
    cancel_i = max(i for i, c in enumerate(ex.calls) if c[0] == "cancel_stop")
    assert set_i < cancel_i and list(first)[0] == ex.calls[cancel_i][1]  # new stop first, then the old one goes
    assert list(ex.stops.values())[0] > list(first.values())[0]


async def test_exchange_stop_fired_while_away_is_booked_from_exchange_fills(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    p = await open_live(sf, redis_client, ex)
    stop_px = p.stop_loss - Decimal("0.5")  # filled a little through the stop
    ex.trigger_stop(stop_px)
    report = await futures_live.reconcile_venue(sf, redis_client, LIVE_ON, "binance", ex, NOW + timedelta(minutes=2))
    assert report["closed_by_exchange"] == 1
    async with sf() as s:
        p = await s.get(PaperPosition, p.id)
        ev = (await s.execute(select(ReconciliationEvent).where(ReconciliationEvent.kind == "closed_by_exchange"))).scalar_one()
    assert p.status == "closed" and p.exit_reason == "exchange_stop" and p.exit_price == stop_px and p.realized_pnl < 0
    assert ev.detail["vwap"] == str(stop_px)


async def test_failed_stop_placement_closes_the_position(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    ex.fail_stop = True
    p = await open_live(sf, redis_client, ex)
    async with sf() as s:
        p = await s.get(PaperPosition, p.id)
        exit_order = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.reason == "protection_failed"))).scalar_one()
        crit = (await s.execute(select(ReconciliationEvent).where(ReconciliationEvent.kind == "protection_failed"))).scalar_one()
    assert p.exit_requested and exit_order.status == "PENDING" and crit.severity == "critical"
    await futures_live.process_order(sf, redis_client, LIVE_ON, {"binance": ex}, exit_order.id, now_fn=lambda: NOW)
    async with sf() as s:
        assert (await s.get(PaperPosition, p.id)).status == "closed" and ex.size == 0


async def test_missing_protection_is_replaced_on_reconcile(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    await open_live(sf, redis_client, ex)
    ex.stops.clear()  # someone cancelled it on the exchange
    report = await futures_live.reconcile_venue(sf, redis_client, LIVE_ON, "binance", ex, NOW + timedelta(minutes=1))
    assert report["protection_replaced"] == 1 and len(ex.stops) == 1


async def test_crash_after_submit_is_resolved_by_client_id(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange()
    await go_live(sf, redis_client, ex)
    await run_strategy(sf, redis_client, LIVE_ON, {"binance": FakeVenue()}, "meta_muse", NOW)
    async with sf() as s:
        o = (await s.execute(select(ExecutionOrder))).scalar_one()
        # Simulate: the worker persisted SUBMITTED + client id, sent the order, then died.
        o.status, o.signature, o.submitted_at = "SUBMITTED", futures_live.client_id(o), NOW
        cid, qty, ref = o.signature, Decimal(o.amount).quantize(Decimal("0.001")), Decimal(o.limits["ref_price"])
        await s.commit()
    await ex.market_order("ETHUSDT", "BUY", qty, False, cid, ref)
    await futures_live.reconcile_venue(sf, redis_client, LIVE_ON, "binance", ex, NOW + timedelta(minutes=1))
    async with sf() as s:
        o = (await s.execute(select(ExecutionOrder))).scalar_one()
        p = (await s.execute(select(PaperPosition))).scalar_one()
    assert o.status == "CONFIRMED" and p.status == "open" and p.quantity == qty and ex.stops


async def test_fill_far_from_plan_is_closed_at_once(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    ex = FakeExchange(fill_price=Decimal(2000))  # the market moved a lot between decision and fill
    p = await open_live(sf, redis_client, ex)
    async with sf() as s:
        p = await s.get(PaperPosition, p.id)
        o = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.reason == "protection_failed"))).scalar_one()
    assert p.exit_requested and o.side == "SELL" and o.status == "PENDING"


async def test_live_is_refused_without_readiness_or_with_a_running_external_bot(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        await store.set_global_mode(s, GlobalMode.LIVE, None)
        await store.set_strategy_mode(s, "meta_muse", StrategyMode.AUTO, None)
        await store.set_strategy_mode(s, "binance_futures", StrategyMode.AUTO, None)
        await s.commit()
    r = await run_strategy(sf, redis_client, LIVE_ON, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "NO_TRADE", r
    async with sf() as s:
        assert (await s.execute(select(PaperPosition))).first() is None

    ex = FakeExchange()
    await futures_live.reconcile_venue(sf, redis_client, LIVE_ON, "binance", ex, NOW)
    env = SimpleNamespace(**vars(LIVE_ON), META_MUSE_CONTROL_URL="http://127.0.0.1:8101", META_MUSE_TOKEN="t")
    assert "state is unknown" in await external_bots.conflict(redis_client, env, "meta_muse")
    r = await run_strategy(sf, redis_client, env, {"binance": FakeVenue()}, "meta_muse", NOW + timedelta(minutes=5))
    async with sf() as s:
        assert (await s.execute(select(ExecutionOrder))).first() is None


async def test_environment_locks_keep_everything_paper(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        await store.set_global_mode(s, GlobalMode.LIVE, None)
        await store.set_strategy_mode(s, "meta_muse", StrategyMode.AUTO, None)
        await s.commit()
    locked = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
    ok, why = await futures_live.readiness(redis_client, locked, "binance")
    assert not ok and "locks" in why
    await run_strategy(sf, redis_client, locked, {"binance": FakeVenue()}, "meta_muse", NOW)
    async with sf() as s:
        assert (await s.execute(select(ExecutionOrder))).first() is None
