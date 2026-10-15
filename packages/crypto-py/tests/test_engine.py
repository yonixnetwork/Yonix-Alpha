"""The worker end to end with a fake exchange and a test strategy: states,
one order per candle, restart safety, protection while disabled, SHORT and
stale handling, kill switch."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from yonixalpha_crypto import config_store, registry
from yonixalpha_crypto.config import CryptoSettings
from yonixalpha_crypto.db import DemoAccount, DemoOrder, DemoPosition, Signal, StrategyConfig
from yonixalpha_crypto.engine import KILL_SWITCH_KEY, Engine
from yonixalpha_crypto.strategies import IMPLEMENTATIONS, Decision

from tests.conftest import NOW, FakeMarket, book, candles

TEST_ID = "test_always"


class AlwaysLong:
    """Test strategy: wants LONG every candle (SHORT when params say so)."""
    spec = registry.StrategySpec(
        id=TEST_ID, name="test", version="t1", category="test", status=registry.IMPLEMENTED, status_reason="",
        provenance={}, exchanges=("bybit",), intervals=("5m",), default_exchange="bybit",
        default_symbols=("BTC/USDT",), default_interval="5m", params={"side": ("LONG", "choice:LONG|SHORT", None, None)})

    def required_candles(self, params):
        return 3

    def traded_symbol(self, symbols):
        return symbols[-1]

    def evaluate(self, candles, params, position_open):
        last = candles["BTC/USDT"][-1].close
        if position_open:
            return Decision("LONG", "HOLD", reasons=["holding"])
        return Decision(params.get("side", "LONG"), "ENTER", stop_price=last * Decimal("0.99"),
                        target_price=last * Decimal("1.02"), reasons=["test entry"], detail={"close": last})


@pytest.fixture
def strategy():
    registry.register(AlwaysLong.spec)
    IMPLEMENTATIONS[TEST_ID] = AlwaysLong()
    yield
    IMPLEMENTATIONS.pop(TEST_ID, None)
    registry._REGISTRY.pop(TEST_ID, None)


def market():
    md = FakeMarket("bybit")
    md.series["BTC/USDT"] = candles(["100", "100", "100", "100"])
    md.books["BTC/USDT"] = book("99.98", "100.02")
    return md


async def engine(sf, redis, md):
    return Engine(CryptoSettings(database_url="unused", redis_url="unused"), markets={md.exchange: md}, redis=redis,
                  session_factory=sf)


async def enable(sf, *, engine_on=True, side="LONG"):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        await s.commit()
        await config_store.save_strategy(s, TEST_ID, {"enabled": True, "mode": "DEMO", "params": {"side": side}}, 1, "t")
        await config_store.set_engine(s, enabled=engine_on, emergency_stop=None, note=None, actor="t")
        await s.commit()


async def counts(sf):
    async with sf() as s:
        return (await s.scalar(select(func.count()).select_from(DemoOrder)),
                await s.scalar(select(func.count()).select_from(DemoPosition)),
                await s.scalar(select(func.count()).select_from(Signal)))


async def test_engine_off_by_default_evaluates_nothing(sf, redis, strategy):
    md = market()
    e = await engine(sf, redis, md)
    out = await e.tick(NOW)
    assert out["state"] == "DISABLED" and out["evaluated"] == {}
    assert not [c for c in md.calls if c.startswith("candles")]
    assert await counts(sf) == (0, 0, 0)


async def test_one_demo_entry_per_closed_candle_and_restart_is_safe(sf, redis, strategy):
    await enable(sf)
    md = market()
    e = await engine(sf, redis, md)
    out = await e.tick(NOW)
    assert out["state"] == "RUNNING" and out["evaluated"][TEST_ID] == "ENTER"
    async with sf() as s:
        order = (await s.execute(select(DemoOrder))).scalar_one()
        pos = (await s.execute(select(DemoPosition))).scalar_one()
        acc = (await s.execute(select(DemoAccount))).scalar_one()
    assert order.status == "FILLED" and order.side == "BUY" and order.price >= Decimal("100.02")
    assert pos.status == "OPEN" and pos.stop_price is not None
    assert acc.cash == Decimal(1000) - order.quote_amount - order.fee_quote  # demo cash only
    # A restart (new Engine) on the same candle: the signal already exists, nothing more is filled.
    e2 = await engine(sf, redis, md)
    out = await e2.tick(NOW + timedelta(seconds=10))
    assert out["evaluated"].get(TEST_ID) in (None, "duplicate")
    assert (await counts(sf))[:2] == (1, 1)


async def test_protection_keeps_working_when_the_strategy_and_engine_are_off(sf, redis, strategy):
    await enable(sf)
    md = market()
    e = await engine(sf, redis, md)
    await e.tick(NOW)
    async with sf() as s:
        cfg = await s.get(StrategyConfig, TEST_ID)
        await config_store.save_strategy(s, TEST_ID, {"enabled": False, "mode": "DISABLED"}, cfg.version, "t")
        await config_store.set_engine(s, enabled=False, emergency_stop=None, note=None, actor="t")
        await s.commit()
    md.books["BTC/USDT"] = book("98", "98.05")  # below the 1% stop
    out = await e.tick(NOW + timedelta(minutes=1))
    assert out["state"] == "DISABLED" and out["closed_by_protection"] == 1
    async with sf() as s:
        pos = (await s.execute(select(DemoPosition))).scalar_one()
    assert pos.status == "CLOSED" and pos.exit_reason == "stop_loss" and pos.realized_pnl_quote < 0


async def test_short_signals_are_recorded_not_simulated_on_spot(sf, redis, strategy):
    await enable(sf, side="SHORT")
    e = await engine(sf, redis, market())
    out = await e.tick(NOW)
    assert out["evaluated"][TEST_ID] == "REJECTED"
    async with sf() as s:
        sig = (await s.execute(select(Signal))).scalar_one()
    assert "long-only" in sig.reasons[-1]
    assert (await counts(sf))[:2] == (0, 0)


async def test_stale_data_is_rejected(sf, redis, strategy):
    await enable(sf)
    md = market()
    md.series["BTC/USDT"] = candles(["100"] * 4, end=NOW - timedelta(hours=1))
    e = await engine(sf, redis, md)
    out = await e.tick(NOW)
    assert out["evaluated"][TEST_ID] == "rejected"
    async with sf() as s:
        sig = (await s.execute(select(Signal))).scalar_one()
    assert sig.action == "REJECTED" and "stale" in sig.reasons[0]


async def test_kill_switch_and_emergency_stop_block_new_entries(sf, redis, strategy):
    await enable(sf)
    e = await engine(sf, redis, market())
    await redis.set(KILL_SWITCH_KEY, "1")
    assert (await e.tick(NOW))["state"] == "KILL_SWITCH"
    await redis.delete(KILL_SWITCH_KEY)
    async with sf() as s:
        await config_store.set_engine(s, enabled=None, emergency_stop=True, note=None, actor="t")
        await s.commit()
    assert (await e.tick(NOW))["state"] == "EMERGENCY_STOP"
    assert await counts(sf) == (0, 0, 0)


async def test_no_schema_means_waiting_not_failing(redis):
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
    e = await engine(async_sessionmaker(eng, expire_on_commit=False), redis, market())
    assert (await e.tick(NOW))["state"] == "WAITING_FOR_SCHEMA"
    await eng.dispose()


async def test_meta_muse_end_to_end_long_eth_with_stop_anchored_on_the_fill(sf, redis):
    from tests.test_meta_muse import series

    md = FakeMarket("binance_us")  # the default exchange (reachable from the server)
    md.series["BTC/USDT"] = candles(series(60000, -0.002))
    md.series["ETH/USDT"] = candles(series(3000, 0.002))
    last = Decimal(series(3000, 0.002)[-1])
    md.books["ETH/USDT"] = book(str(last - Decimal("0.05")), str(last + Decimal("0.05")), symbol="ETH/USDT",
                                  exchange="binance_us")
    async with sf() as s:
        await config_store.ensure_defaults(s)
        await s.commit()
        await config_store.save_strategy(s, "meta_muse", {"enabled": True, "mode": "DEMO"}, 1, "t")
        await config_store.set_engine(s, enabled=True, emergency_stop=None, note=None, actor="t")
        await s.commit()
    e = await engine(sf, redis, md)
    out = await e.tick(NOW)
    assert out["evaluated"]["meta_muse"] == "ENTER"
    async with sf() as s:
        order = (await s.execute(select(DemoOrder))).scalar_one()
        pos = (await s.execute(select(DemoPosition))).scalar_one()
        sig = (await s.execute(select(Signal))).scalar_one()
    assert order.symbol == "ETH/USDT" and order.status == "FILLED" and order.exchange == "binance_us"
    assert pos.stop_price == order.price * Decimal("0.99") and pos.target_price == order.price * Decimal("1.02")
    assert sig.detail["lead"]["direction"] == "down" and sig.detail["traded"]["direction"] == "up"
    # the loss at the stop stays within the 5 USDT default risk budget
    assert Decimal(sig.detail["risk"]["planned_loss_quote"]) <= Decimal(5)
