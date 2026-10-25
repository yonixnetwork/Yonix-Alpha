"""Day comparison and market samples (2026-10-25): days follow the chosen
time zone, trades carry what the decision saw, a single big winner is shown
as such, changes around the days are listed (automatic demotions included),
large buys before 2026-10-24 are unknown rather than zero, and a market
sample is written once per slot."""

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import json  # noqa: E402

import pytest  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import market_samples  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import AuditLog, MarketSample, OpportunityOutcome, PaperPosition  # noqa: E402
from yonixalpha_core.solana import pump_stream, sol_price  # noqa: E402
from yonixalpha_core.tools import day_compare as dc  # noqa: E402

FRI = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def closed(pnl, exit_at, reason, engine="solana_fresh", mint=None):
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine=engine,
                         asset_id=mint or uuid.uuid4().hex, entry_price=Decimal("1.02"), quantity=Decimal(1),
                         take_profit=[], status="closed", execution_mode="PAPER", realized_pnl=Decimal(str(pnl)),
                         realized_pnl_pct=Decimal(str(pnl)) * 10, fees_paid_quote=Decimal("0.001"),
                         entry_cost_quote=Decimal("0.1"), proceeds_quote=Decimal(0), entry_at=exit_at - timedelta(minutes=3),
                         exit_at=exit_at, exit_reason=reason, highest_price=Decimal("1.3"), lowest_price=Decimal("0.9"),
                         plan={"entry_price": "1.0", "position_size": {"value": "0.1"}, "stop_distance_pct": "0.1"})


def test_day_boundaries_follow_the_time_zone():
    s, e = dc.day_window(date(2026, 10, 23), ZoneInfo("UTC"))
    assert (s, e) == (datetime(2026, 10, 23, tzinfo=timezone.utc), datetime(2026, 10, 24, tzinfo=timezone.utc))
    s, _ = dc.day_window(date(2026, 10, 23), ZoneInfo("Asia/Manila"))  # UTC+8: Friday starts Thursday 16:00 UTC
    assert s == datetime(2026, 10, 22, 16, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_days_side_by_side_with_decision_context_concentration_and_changes(sf):
    async with sf() as s:
        big = closed("0.2", FRI, "take_profit_3", mint="BIGMINT")
        big.plan = {**big.plan, "entry_delay": {"latency_s": 2.5, "price_change_pct": "3.1", "measured": True},
                    "exit_steps": [{"reason": "exit_intel_reduce"}, {"reason": "take_profit_3"}]}
        s.add_all([big, closed("-0.01", FRI + timedelta(hours=1), "stop_loss"),
                   closed("-0.05", FRI + timedelta(days=1), "stop_loss")])
        await s.flush()
        s.add(OpportunityOutcome(key="gate:big", mint="BIGMINT", engine="solana_fresh", stage="GATE", decision="EXECUTE",
                                 traded=True, position_id=big.id, decided_at=big.entry_at - timedelta(seconds=4),
                                 snapshot={"age_seconds": 400, "liquidity_sol": "12.5", "buyers": 30, "sellers": 10,
                                           "buy_sell_ratio": "3.00", "deterioration_indicators": [],
                                           "intel": {"momentum": {"return_1m": 0.05, "return_5m": 0.2}}},
                                 regime={"launches_last_hour_seen": 900}))
        s.add(OpportunityOutcome(key="gate:no", mint="NOMINT", engine="solana_fresh", stage="GATE", decision="REJECT",
                                 traded=False, decided_at=FRI, reasons=["TOP HOLDER: 40% (max 30%)"], snapshot={}))
        s.add(AuditLog(event_type="entry_intel.strategy_demoted", detail={"strategy": "fresh_x", "to": "SHADOW"},
                       created_at=FRI + timedelta(hours=20)))
        s.add(MarketSample(at=FRI, sol_usd=Decimal("180"), launches_last_hour=900))
        s.add(MarketSample(at=FRI + timedelta(hours=6), sol_usd=Decimal("171"), launches_last_hour=1100))
        await s.commit()
        r = await dc.build(s, [date(2026, 10, 23), date(2026, 10, 24)], ZoneInfo("UTC"), ("solana_fresh",), 60)
        fri, sat = r["days"]
        assert len(fri["trades"]) == 2 and len(sat["trades"]) == 1
        t = next(x for x in fri["trades"] if x["mint"] == "BIGMINT")
        assert t["entry_delay_s"] == 4.0 and t["return_5m_before_pct"] == 20.0 and t["liquidity_sol"] == 12.5
        assert t["max_up_pct"] == 27.5 and t["slippage_estimate_pct"] == 2.0 and t["stage"] == "FRESH"
        assert t["large_buys_5m_before"] is None  # no large-buy data that day: unknown, never 0
        assert t["fill_latency_s"] == 2.5 and t["fill_move_pct"] == 3.1  # the simulated fill, after the order
        assert t["exit_path"] == ["exit_intel_reduce", "take_profit_3"]
        c = fri["results"]["solana_fresh"]["concentration"]["PAPER"]
        assert c["best_trade"] == 0.2 and c["net_without_best"] == -0.01 and c["best_share_of_wins_pct"] == 100.0
        assert fri["market"]["sol_usd_change_pct"] == -5.0 and fri["market"]["samples"] == 2
        assert fri["top_refusals"][0]["reason"] == "TOP HOLDER"
        assert any(x["what"] == "entry_intel.strategy_demoted" for x in r["changes"]["audit log"])
        summary = await dc.build(s, [date(2026, 10, 23)], ZoneInfo("UTC"), ("solana_fresh",), 0)
        assert summary["days"][0]["trades"] == [] and summary["days"][0]["trades_truncated"]  # --trades 0: summary only
        assert summary["days"][0]["results"]["solana_fresh"]["modes"]["PAPER"]["overall"]["trades"] == 2
        assert summary["days"][0]["results"]["solana_fresh"]["concentration"]["PAPER"]["best_trade"] == 0.2
        text = dc.render(r)
        assert "SIDE BY SIDE" in text and "BIGMINT" in text and "Nothing was written" in text
        assert "net without best" in text and "strategy_demoted" in text
        assert "exit path: exit_intel_reduce > take_profit_3" in text and "simulated fill after 2.5 s" in text


@pytest.mark.asyncio
async def test_a_market_sample_is_written_once_per_slot_and_never_with_a_stale_price(sf):
    redis = from_url(os.environ["REDIS_URL"], decode_responses=True)
    await redis.flushdb()
    try:
        now = datetime.now(timezone.utc)
        await redis.zadd(pump_stream.RECENT, {"a": now.timestamp() - 60, "b": now.timestamp() - 7200})
        await redis.set(pump_stream.HEARTBEAT, now.isoformat())
        await redis.set(sol_price.CACHE_KEY, json.dumps({"price": "175.5", "source": "test", "at": now.isoformat()}))
        async with sf() as s:
            assert await market_samples.record(s, redis, now) is True
            await s.commit()
            assert await market_samples.record(s, redis, now) is False  # same slot
            row = (await s.execute(select(MarketSample))).scalar_one()
            assert row.sol_usd == Decimal("175.5") and row.launches_last_hour == 1 and row.stream_heartbeat_age_s == 0
            await redis.set(sol_price.CACHE_KEY, json.dumps({"price": "1", "source": "old",
                                                             "at": (now - timedelta(minutes=10)).isoformat()}))
            later = now + timedelta(seconds=market_samples.SAMPLE_SECONDS)
            assert await market_samples.record(s, redis, later) is True
            await s.commit()
            rows = (await s.execute(select(MarketSample).order_by(MarketSample.at))).scalars().all()
            assert rows[-1].sol_usd is None  # a 10-minute-old price is not this slot's price
    finally:
        await redis.flushdb()
        await redis.aclose()
