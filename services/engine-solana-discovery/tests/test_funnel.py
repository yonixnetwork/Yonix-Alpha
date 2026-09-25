import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream

from app.funnel import FUNNEL, MAX_ACTIVE_CANDIDATES, run_funnel

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
SETTINGS = default_settings_for("solana_fresh")


async def add_mint(redis, mint: str, age_s: int, buyers: int, trades: int) -> None:
    created = int((NOW - timedelta(seconds=age_s)).timestamp())
    await redis.hset(pump_stream.meta_key(mint), mapping={"symbol": mint[:4], "name": mint, "creator": "C", "created_at": created,
                                                          "bonding_curve": "BC", "signature": f"sig-{mint}"})
    await redis.zadd(pump_stream.RECENT, {mint: created})
    for i in range(trades):
        row = [int((NOW - timedelta(seconds=250 - i)).timestamp()), f"w{i % buyers}", 1, 10**8, 10**12, 3 * 10**10, 10**15]
        await redis.rpush(pump_stream.trades_key(mint), json.dumps(row))


async def candidates(session_factory):
    async with session_factory() as s:
        return (await s.execute(select(TradingCandidate))).scalars().all()


async def test_only_active_launches_are_promoted(redis, session_factory):
    await add_mint(redis, "ACTIVE", 600, buyers=12, trades=20)
    await add_mint(redis, "THIN", 600, buyers=3, trades=20)
    await add_mint(redis, "TOONEW", 30, buyers=12, trades=20)
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["promoted"] == 1 and counts["prefilter_failed"] == 2
    [c] = await candidates(session_factory)
    assert c.engine == "discovery" and c.detail["source"] == "pump_stream" and c.detail["strategy"] == "solana_fresh"
    assert c.detail["prefilter"]["unique_buyers"] == 12
    # Already promoted: a second run does nothing.
    again = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert again["promoted"] == 0 and len(await candidates(session_factory)) == 1
    assert int(await redis.hget(FUNNEL, "promoted")) == 1


async def test_budget_caps_active_candidates(redis, session_factory):
    for i in range(MAX_ACTIVE_CANDIDATES + 3):
        await add_mint(redis, f"M{i:03d}", 600, buyers=12, trades=20)
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["promoted"] == MAX_ACTIVE_CANDIDATES and counts["budget_full"] == 3
    # Deferred mints stay unpromoted, so they get another chance later.
    assert len(await pump_stream.recent_unpromoted(redis, NOW, 3600)) == 3


async def test_migration_event_creates_one_migration_candidate(redis, session_factory):
    await redis.hset(pump_stream.meta_key("MIG"), mapping={"symbol": "MIG", "created_at": 1})
    await redis.hset(pump_stream.curve_key("MIG"), mapping={"vsol": 1, "vtok": 1, "updated_at": 1, "complete": 1, "pool": "POOL"})
    await redis.zadd(pump_stream.MIGRATED, {"MIG": int(NOW.timestamp()) - 60})
    assert (await run_funnel(redis, session_factory, SETTINGS, NOW))["migrations"] == 1
    assert (await run_funnel(redis, session_factory, SETTINGS, NOW))["migrations"] == 0
    [c] = await candidates(session_factory)
    assert c.engine == "migration" and c.detail["pool"] == "POOL" and c.detail["strategy"] == "solana_migration"


async def test_momentum_promotes_established_accelerating_tokens_only(redis, session_factory):
    async def add_established(mint, recent, prior):
        created = int((NOW - timedelta(hours=2)).timestamp())
        await redis.hset(pump_stream.meta_key(mint), mapping={"symbol": mint, "created_at": created, "bonding_curve": "BC",
                                                              "creator": "C"})
        await redis.hset(pump_stream.curve_key(mint), mapping={"vsol": 1, "vtok": 1, "updated_at": 1})
        for i in range(prior):
            row = [int((NOW - timedelta(seconds=590 - i * 10)).timestamp()), f"p{i}", 1, 10**8, 10**12, 3 * 10**10, 10**15]
            await redis.rpush(pump_stream.trades_key(mint), json.dumps(row))
        for i in range(recent):
            row = [int((NOW - timedelta(seconds=290 - i * 5)).timestamp()), f"r{i}", 1, 10**8, 10**12, 3 * 10**10, 10**15]
            await redis.rpush(pump_stream.trades_key(mint), json.dumps(row))
        await redis.zadd(pump_stream.ACTIVE, {mint: int(NOW.timestamp()) - 10})

    await add_established("HOT", recent=30, prior=10)
    await add_established("STEADY", recent=12, prior=12)
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["momentum_considered"] == 2 and counts["momentum_promoted"] == 1
    [c] = await candidates(session_factory)
    assert c.engine == "momentum" and c.detail["mint"] == "HOT" and c.detail["strategy"] == "solana_momentum"
    assert (await run_funnel(redis, session_factory, SETTINGS, NOW))["momentum_promoted"] == 0
