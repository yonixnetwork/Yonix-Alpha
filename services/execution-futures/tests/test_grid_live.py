"""Live Hyperliquid grid against a fake exchange account (resting orders,
fills only when the fake says so, positions, trigger stops)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core import futures_live, grid_live
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, StrategyState
from yonixalpha_core.execution.base import InstrumentRules, OrderState, PositionInfo
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
LIVE_ON = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, TELEGRAM_BOT_TOKEN=None,
                          TELEGRAM_CHAT_ID=None)


class FakeHL:
    configured = True
    quote_currency = "USDC"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.size = Decimal(0)
        self.stops: dict[str, Decimal] = {}
        self.log = []

    async def instrument(self, coin):
        return InstrumentRules(coin, Decimal("0.00001"), Decimal(1), Decimal("0.00001"), Decimal(10), 5)

    async def prepare(self, coin, lev):
        self.log.append(("prepare", lev))

    async def balance(self):
        return Decimal(1000)

    async def limit_order(self, coin, side, qty, price, cid, post_only=True, reduce_only=False):
        self.orders[cid] = {"side": side, "qty": qty, "price": price, "filled": Decimal(0), "status": "NEW"}
        return OrderState(cid, "NEW")

    def fill(self, cid, qty=None):
        o = self.orders[cid]
        q = qty or o["qty"]
        o["filled"] += q
        o["status"] = "FILLED" if o["filled"] >= o["qty"] else "NEW"
        self.size += q if o["side"] == "BUY" else -q

    async def order_status(self, coin, cid):
        o = self.orders[cid]
        return OrderState(cid, o["status"], o["filled"], o["price"] if o["filled"] else None,
                          o["filled"] * o["price"] * Decimal("0.00015"))

    async def cancel_order(self, coin, cid):
        if self.orders[cid]["status"] == "NEW":
            self.orders[cid]["status"] = "CANCELED"

    async def market_order(self, coin, side, qty, reduce_only, cid, ref_price=None):
        self.log.append(("market", side, qty, reduce_only))
        self.size += qty if side == "BUY" else -qty
        return OrderState(cid, "FILLED", qty, ref_price, qty * ref_price * Decimal("0.00045"))

    async def set_stop(self, coin, side, price, qty, cid):
        sid = f"s{len(self.stops) + len(self.log)}"
        self.stops[sid] = price
        self.log.append(("stop", side, price, qty))
        return sid

    async def cancel_stop(self, coin, sid):
        self.stops.pop(sid, None)

    async def position(self, coin):
        return PositionInfo(coin, self.size, None) if self.size else None


async def setup(sf, redis, mode=StrategyMode.AUTO):
    async with sf() as s:
        await store.set_global_mode(s, GlobalMode.LIVE, None)
        await store.set_strategy_mode(s, "hyperliquid_grid", mode, None)
        await store.set_strategy_mode(s, "hyperliquid_perps", mode, None)
        await s.commit()
    await futures_live.publish_readiness(redis, "hyperliquid", "ready", None, NOW, futures_live.FuturesLiveSettings(),
                                         Decimal(1000), "USDC")


async def mid_at(value):
    async def f(coin):
        return Decimal(value)
    return f


async def test_live_grid_places_post_only_orders_and_books_only_confirmed_fills(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await setup(sf, redis_client)
    hl = FakeHL()
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW)
    assert r["status"] == "running", r
    buys = sorted((cid for cid, o in hl.orders.items() if o["side"] == "BUY"), key=lambda c: hl.orders[c]["price"])
    assert buys and all(o["status"] == "NEW" for o in hl.orders.values())
    # Price moves through a buy level but the exchange has NOT filled it: nothing is booked.
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(59600), NOW + timedelta(seconds=5))
    async with sf() as s:
        row = (await s.execute(select(StrategyState).where(StrategyState.key == "live:BTC"))).scalar_one()
        assert Decimal(row.state["grid"]["net_position"]) == 0 and r["fills"] == 0
    # The exchange fills the top buy: booked at its price, replaced by a sell one level up, stop placed.
    top = buys[-1]
    hl.fill(top)
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(59600), NOW + timedelta(seconds=10))
    assert r["fills"] == 1
    async with sf() as s:
        row = (await s.execute(select(StrategyState).where(StrategyState.key == "live:BTC"))).scalar_one()
        g = row.state["grid"]
        assert Decimal(g["net_position"]) == hl.orders[top]["qty"] and Decimal(g["fees"]) > 0
        replacement = [o for o in row.state["orders"] if not o["is_buy"]]
        assert any(Decimal(o["price"]) > hl.orders[top]["price"] for o in replacement)
        done = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.idempotency_key == top))).scalar_one()
        assert done.status == "CONFIRMED" and done.provider == "hyperliquid_grid"
    stop = [x for x in hl.log if x[0] == "stop"][-1]
    assert stop[1] == "LONG" and stop[2] < Decimal(59000)  # below the range-break level


async def test_range_break_cancels_orders_and_flattens(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await setup(sf, redis_client)
    hl = FakeHL()
    await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW)
    buy = next(c for c, o in hl.orders.items() if o["side"] == "BUY")
    hl.fill(buy)
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(55000), NOW + timedelta(seconds=5))
    assert r.get("reason") == "pause_range_break"
    assert hl.size == 0 and ("market", "SELL", hl.orders[buy]["qty"], True) in hl.log
    assert all(o["status"] != "NEW" for o in hl.orders.values())  # nothing left resting
    async with sf() as s:
        row = (await s.execute(select(StrategyState).where(StrategyState.key == "live:BTC"))).scalar_one()
        assert row.status == "stopped"


async def test_exchange_position_mismatch_pauses_for_review(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await setup(sf, redis_client)
    hl = FakeHL()
    await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW)
    hl.size = Decimal("0.5")  # someone traded on the account by hand
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW + timedelta(seconds=5))
    assert r["status"] == "needs_review"
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW + timedelta(seconds=10))
    assert r["status"] == "needs_review"  # never restarts on its own


async def test_no_live_grid_without_readiness_or_in_manual_without_start(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await setup(sf, redis_client, StrategyMode.MANUAL)
    hl = FakeHL()
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW)
    assert r["status"] == "not started" and not hl.orders
    await redis_client.delete(futures_live.READY_KEY.format(venue="hyperliquid"))
    await redis_client.set("yx:grid:cmd", "start")
    r = await grid_live.run(sf, redis_client, LIVE_ON, hl, await mid_at(60000), NOW)
    assert r["status"] == "not started" and "not reported" in r["reason"] and not hl.orders
    locked = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
    assert (await grid_live.run(sf, redis_client, locked, hl, await mid_at(60000), NOW))["status"] == "not live"
