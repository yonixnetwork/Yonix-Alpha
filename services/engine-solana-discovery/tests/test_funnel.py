import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core.db.models import TokenObservation, TradingCandidate
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream

from app.funnel import FUNNEL, run_funnel

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
SETTINGS = default_settings_for("solana_fresh")
SOL = 10**9


async def launch(redis, mint: str, age_s: float, creator: str = "CREATOR") -> int:
    created = int((NOW - timedelta(seconds=age_s)).timestamp())
    await redis.hset(pump_stream.meta_key(mint), mapping={"symbol": mint[:4], "name": mint, "creator": creator, "created_at": created,
                                                          "bonding_curve": "BC", "signature": f"sig-{mint}"})
    await redis.zadd(pump_stream.RECENT, {mint: created})
    await redis.hset(pump_stream.curve_key(mint), mapping={"vsol": 30 * SOL, "vtok": 10**15, "rsol": 1 * SOL,
                                                           "rtok": 700_000_000_000_000, "updated_at": created})
    return created


async def trade(redis, mint: str, at: int, who: str, buy: bool, sol: float, vsol: float) -> None:
    row = [at, who, 1 if buy else 0, int(sol * SOL), 10**12, int(vsol * SOL), 10**15]
    await redis.rpush(pump_stream.trades_key(mint), json.dumps(row))


async def candidates(session_factory):
    async with session_factory() as s:
        return (await s.execute(select(TradingCandidate))).scalars().all()


async def observations(session_factory):
    async with session_factory() as s:
        return {o.mint: o for o in (await s.execute(select(TokenObservation))).scalars().all()}


async def growing_launch(redis, mint: str, age_s: float = 12) -> None:
    """Activity increasing through the 10 s window: 3 buyers, then 7."""
    created = await launch(redis, mint, age_s)
    for i in range(3):
        await trade(redis, mint, created + 1 + i, f"{mint}-a{i}", True, 0.1, 30 + i)
    for i in range(7):
        await trade(redis, mint, created + 6 + i // 2, f"{mint}-b{i}", True, 0.1, 33 + i)


async def test_token_inside_its_window_is_fresh_observing(redis, session_factory):
    await launch(redis, "YOUNG", 5)
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["observing"] == 1 and counts["promoted"] == 0
    assert await redis.zscore(pump_stream.OBS_LIVE, "YOUNG") is not None
    report = json.loads(await redis.get(pump_stream.obs_report_key("YOUNG")))
    assert report["outcome"] == "OBSERVING" and "FRESH_OBSERVING" in report["reasons"][0]
    assert await observations(session_factory) == {}


async def test_growing_fresh_token_without_dex_liquidity_is_promoted_after_the_window(redis, session_factory):
    await growing_launch(redis, "GROW")
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["promoted"] == 1
    [c] = await candidates(session_factory)
    obs = c.detail["observation"]
    assert c.engine == "discovery" and c.detail["strategy"] == "solana_fresh"
    assert obs["trend"] == "INCREASING" and [cp["label"] for cp in obs["checkpoints"]] == ["T0", "T+5s", "T+10s"]
    assert obs["metrics"]["liquidity_state"] == "NO DEX POOL YET — bonding curve market"
    assert c.detail["prefilter"]["unique_buyers"] == 10
    row = (await observations(session_factory))["GROW"]
    assert row.outcome == "PROMOTE" and row.candidate_id == c.id and row.report["halves"][1]["new_buyers"] == 7
    # Final: a second run neither re-promotes nor re-records it.
    again = await run_funnel(redis, session_factory, SETTINGS, NOW + timedelta(seconds=10))
    assert again["promoted"] == 0 and len(await candidates(session_factory)) == 1
    assert int(await redis.hget(FUNNEL, "promoted")) == 1


async def test_thin_token_keeps_being_monitored_then_expires_with_reasons(redis, session_factory):
    created = await launch(redis, "THIN", 12)
    for i in range(3):
        await trade(redis, "THIN", created + 2 + i, f"t{i}", True, 0.05, 30 + i)
    first = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert first["continue_monitoring"] == 1 and first["promoted"] == 0
    report = json.loads(await redis.get(pump_stream.obs_report_key("THIN")))
    assert report["outcome"] == "CONTINUE_MONITORING" and "3 trades since launch (need 8)" in report["reasons"][0]
    later = await run_funnel(redis, session_factory, SETTINGS, NOW + timedelta(seconds=200))
    assert later["expired"] == 1
    row = (await observations(session_factory))["THIN"]
    assert row.outcome == "NO_TRADE" and "inactive" in row.reasons[-1] and "3 unique buyers (need 6)" in row.reasons[-1]
    assert row.report["metrics"]["unique_buyers_total"] == 3


async def test_dumping_launch_is_rejected_with_the_reason_recorded(redis, session_factory):
    created = await launch(redis, "DUMP", 12)
    for i in range(6):
        await trade(redis, "DUMP", created + 1 + i // 3, f"d{i}", True, 0.2, 30 + 2 * i)
    for i in range(4):  # price back down 40%+ from its peak
        await trade(redis, "DUMP", created + 7 + i // 2, f"d{i}", False, 0.3, 40 - 6 * i)
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["rejected"] == 1 and counts["promoted"] == 0
    row = (await observations(session_factory))["DUMP"]
    assert row.outcome == "REJECT" and "below its observed peak" in row.reasons[-1] and row.trend == "DETERIORATING"


async def test_gate_budget_keeps_qualified_tokens_monitored_not_dropped(redis, session_factory):
    for i in range(4):
        await growing_launch(redis, f"Q{i}")
    counts = await run_funnel(redis, session_factory, replace(SETTINGS, max_active_candidates=2), NOW)
    assert counts["promoted"] == 2 and counts["budget_full"] == 2 and counts["continue_monitoring"] == 2
    waiting = [json.loads(await redis.get(pump_stream.obs_report_key(f"Q{i}"))) for i in range(4)]
    assert sum("gate budget is full" in r["reasons"][-1] for r in waiting if r["outcome"] == "CONTINUE_MONITORING") == 2


async def test_monitoring_capacity_drops_the_least_active_token(redis, session_factory):
    for mint, n in (("BUSY", 5), ("QUIET", 3)):
        created = await launch(redis, mint, 12)
        for i in range(n):
            await trade(redis, mint, created + 2 + i, f"{mint}{i}", True, 0.05, 30 + i)
    counts = await run_funnel(redis, session_factory, replace(SETTINGS, fresh_max_monitored_tokens=1), NOW)
    assert counts["continue_monitoring"] == 1 and counts["expired"] == 1
    row = (await observations(session_factory))["QUIET"]
    assert row.outcome == "NO_TRADE" and "monitoring capacity reached" in row.reasons[-1]


async def test_observed_token_that_migrates_is_handed_to_the_migration_engine(redis, session_factory):
    created = await launch(redis, "MIGR", 60)
    await trade(redis, "MIGR", created + 2, "m0", True, 0.1, 30)
    await redis.hset(pump_stream.curve_key("MIGR"), mapping={"complete": 1, "pool": "POOLX", "migrated_at": int(NOW.timestamp()) - 5})
    await redis.zadd(pump_stream.MIGRATED, {"MIGR": int(NOW.timestamp()) - 5})
    counts = await run_funnel(redis, session_factory, SETTINGS, NOW)
    assert counts["migration_detected"] == 1 and counts["migrations"] == 1
    row = (await observations(session_factory))["MIGR"]
    assert row.outcome == "MIGRATION_DETECTED" and "MIGRATED_ANALYSIS" in row.reasons[0]
    [c] = await candidates(session_factory)
    assert c.engine == "migration" and c.detail["pool"] == "POOLX"


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
