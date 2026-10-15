"""Recovery check tool (2026-10-15): read-only report that the recovery
changes R2-R6, R9 and R10 are in effect and what they did."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest_asyncio  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import rpc_usage  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import OpportunityOutcome, RiskAssessment, TradeTimelineEvent  # noqa: E402
from yonixalpha_core.safety import SafetySettings, assess, store  # noqa: E402
from yonixalpha_core.tools import recovery_check as rc  # noqa: E402

from tests.test_recovery_risk_rules import migration  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def redis():
    r = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def report(sf, redis) -> str:
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await rc.build(s, redis, NOW - timedelta(hours=24), NOW)
        await s.rollback()
    return "\n".join(lines)


async def test_an_empty_database_reports_the_defaults_and_none_yet(sf, redis):
    out = await report(sf, redis)
    assert "solana_migration  migration_max_entry_market_cap_sol=3000" in out
    assert "low_volatility_min_stop_pct=0.10" in out and "cooldown_scope=ACCOUNT" in out
    assert out.count("none yet") == 4
    assert "any losing exit pauses every strategy (unchanged default)" in out
    assert out.endswith("Nothing was written.")


async def test_each_change_is_counted_and_nothing_is_written(sf, redis):
    refused = assess(migration("250146"), SafetySettings())
    assert refused.decision.value == "REJECT"
    async with sf() as s:
        (await store.persist_assessment(s, refused, None, "r2"))[0].evaluated_at = NOW
        s.add(TradeTimelineEvent(event_type="paper_migration_crossing", occurred_at=NOW,
                                 detail={"unmanaged_seconds": 3.0, "source": "DEFAULT"}))
        s.add(TradeTimelineEvent(event_type="paper_entry_delay_fill", occurred_at=NOW,
                                 detail={"measured": True, "price_source": "pumpswap pool trades"}))
        s.add(OpportunityOutcome(key="k", mint="M", engine="solana_migration", stage="GATE", decision="REJECT",
                                 decided_at=NOW, migrated_at=NOW, reasons=[], snapshot={},
                                 horizons={"T+5s": {"source": "pumpswap pool trades (recorded)", "price_raw": "0.0005"},
                                           "T+30m": {"unavailable": "PumpSwap pool not read around this time"}}))
        # decided before the deploy: an old curve price after the migration (counted), a curve price
        # before it (not counted), and the old no-stream message that also mentions PumpSwap (not counted)
        s.add(OpportunityOutcome(key="old", mint="O", engine="solana_fresh", stage="GATE", decision="REJECT",
                                 decided_at=NOW - timedelta(minutes=50), migrated_at=NOW - timedelta(minutes=40),
                                 reasons=[], snapshot={},
                                 horizons={"T+30m": {"source": "pump_stream trades", "price_raw": "0.00041088"},
                                           "T+5m": {"source": "pump_stream trades", "price_raw": "0.0003"},
                                           "T+15m": {"unavailable": "no stream trades for this token (PumpSwap tokens "
                                                                    "and expired history are not in the stream)"}}))
        await s.commit()
    await rpc_usage.count_token_backoff(redis, "solana_fresh", ["AUTO_SL_NO_VOLATILITY"], NOW)

    out = await report(sf, redis)
    assert "refusals 1 (tokens 1)" in out
    assert "migration crossing (unmanaged for the LIVE sell latency): 1" in out
    assert "entry fill via pumpswap pool trades, measured=true: 1" in out
    assert "priced from the pool 1, unavailable (pool not read or no pool trade) 1" in out
    assert "curve price used after the migration 1 (" in out
    assert "AUTO_SL_NO_VOLATILITY" in out
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(RiskAssessment)) == 1


async def test_the_floor_is_reported_and_the_cli_runs_read_only(sf, redis, capsys):
    from dataclasses import replace

    from tests.test_sol_sizing import healthy

    quiet = replace(healthy(), market=replace(healthy().market, volatility=Decimal("0.002")))
    a = assess(quiet, SafetySettings())
    assert "low-volatility floor" in a.plan.stop_loss.method
    async with sf() as s:
        (await store.persist_assessment(s, a, None, "r3"))[0].evaluated_at = NOW
        await s.commit()
    assert await rc.main(["--hours", "24"]) == 0
    out = capsys.readouterr().out
    assert "plans with the floor 1" in out and "Nothing was written." in out
