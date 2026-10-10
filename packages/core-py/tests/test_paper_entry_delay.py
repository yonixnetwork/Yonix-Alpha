"""Paper buys land like LIVE ones (regression audit 2026-10-10): a pump-curve
paper buy is filled at the stream price measured LIVE latency after the
decision, both ways, and is not managed until then. No stream data: the
decision fill is kept and marked unmeasured, never estimated."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import paper_execution as pe  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PaperPosition, TradeTimelineEvent  # noqa: E402
from yonixalpha_core.solana import pump_stream  # noqa: E402

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
T = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def trade(redis, at: datetime, vsol: int, vtok: int) -> None:
    await redis.rpush(pump_stream.trades_key(MINT), json.dumps([int(at.timestamp()), "w", 1, 1, 1, vsol, vtok]))


def position() -> PaperPosition:
    p = PaperPosition(symbol="T", provider="paper", side="LONG", entry_price=Decimal("0.0000003"), quantity=Decimal(1000),
                      initial_quantity=Decimal(1000), remaining_quantity=Decimal(1000), take_profit=[], status="open",
                      entry_at=T, engine="solana_fresh", asset_id=MINT, execution_mode="PAPER",
                      entry_cost_quote=Decimal("0.0003"), highest_price=Decimal("0.0000003"),
                      lowest_price=Decimal("0.0000003"), last_price=Decimal("0.0000003"), plan={"venue": {"decimals": 6}})
    p.plan = pe.schedule_entry_delay(p.plan, T.timestamp(), 3.0, "measured LIVE")
    return p


async def test_price_up_during_the_delay_fills_fewer_tokens(sf, redis):
    await trade(redis, T - timedelta(seconds=1), 30_000_000_000, 1_000_000_000_000_000)
    await trade(redis, T + timedelta(seconds=2), 33_000_000_000, 1_000_000_000_000_000)  # +10%
    async with sf() as s:
        p = position()
        s.add(p)
        await s.flush()
        assert pe.entry_delay_pending(p, T + timedelta(seconds=1))
        assert await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=1)) is None  # not due yet
        out = await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=4))
        await s.commit()
        assert out["measured"] and Decimal(out["factor"]) == Decimal("1.1")
        assert abs(p.quantity - Decimal(1000) / Decimal("1.1")) < Decimal("1e-9")
        assert p.remaining_quantity == p.quantity and p.entry_cost_quote == Decimal("0.0003")  # same SOL spent
        assert p.entry_price == Decimal("0.0000003") * Decimal("1.1")
        assert p.plan["entry_delay"]["settled"] and not pe.entry_delay_pending(p, T + timedelta(seconds=5))
        assert await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=9)) is None  # once only
        ev = (await s.execute(select(TradeTimelineEvent).where(TradeTimelineEvent.event_type == "paper_entry_delay_fill"))).scalar_one()
        assert ev.detail["price_change_pct"] == "10.0000"


async def test_price_down_fills_more_tokens_and_no_stream_is_unmeasured(sf, redis):
    await trade(redis, T - timedelta(seconds=1), 30_000_000_000, 1_000_000_000_000_000)
    await trade(redis, T + timedelta(seconds=1), 27_000_000_000, 1_000_000_000_000_000)  # -10%
    async with sf() as s:
        p = position()
        s.add(p)
        await s.flush()
        out = await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=3))
        assert Decimal(out["factor"]) == Decimal("0.9") and p.quantity > Decimal(1000)
        await redis.delete(pump_stream.trades_key(MINT))
        q = position()
        s.add(q)
        await s.flush()
        out = await pe.settle_entry_delay(s, redis, q, T + timedelta(seconds=3))
        assert out["measured"] is False and q.quantity == Decimal(1000) and "unmeasured" in out["reason"]


def test_setting_parses_and_defaults_on():
    s, errors = pe.parse_settings({"simulate_entry_delay": False})
    assert not errors and s.simulate_entry_delay is False
    assert pe.PaperExecutionSettings().simulate_entry_delay is True
    assert pe.parse_settings({"simulate_entry_delay": "yes"})[1]
