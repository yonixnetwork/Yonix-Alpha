"""Early-entry shadow pass and the fast promotion pass (2026-10-10): the
pass evaluates only tokens with new trades, records the first CANDIDATE
of each strategy once (also across a worker restart), writes the state the
dashboard and the gate's event trigger read, records the latency timeline,
and in PAPER mode creates a PAPER-only gate candidate; the fast pass
promotes a launch right after its observation window. No network."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_store, entry_timing, gate_events
from yonixalpha_core.db.models import EntrySignal, PlatformSetting, TradingCandidate
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.testing.curve_sim import Curve

from app import entry_shadow
from app.funnel import FUNNEL, create_strategy_candidate, fast_pass, matured

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


async def put(redis, mint: str, c: Curve, created: datetime) -> None:
    for t in c.trades:
        await redis.rpush(pump_stream.trades_key(mint), json.dumps([int(t.at.timestamp()), t.trader, 1 if t.is_buy else 0,
                                                                    t.sol_lamports, t.token_raw, t.virtual_sol, t.virtual_token]))
    last = c.trades[-1]
    await redis.hset(pump_stream.curve_key(mint), mapping={"vsol": last.virtual_sol, "vtok": last.virtual_token,
                                                           "updated_at": int(last.at.timestamp()), "fee_bps": 125})
    await redis.hset(pump_stream.meta_key(mint), mapping={"created_at": int(created.timestamp()), "symbol": "EE", "name": "Early",
                                                          "creator": "creator", "received_at": f"{created.timestamp() + 0.8:.3f}"})
    await redis.zadd(pump_stream.RECENT, {mint: int(created.timestamp())})
    await redis.zadd(pump_stream.ACTIVE, {mint: int(last.at.timestamp())})
    await redis.set(pump_stream.HEARTBEAT, datetime.now(timezone.utc).isoformat())


def accelerating(created: datetime) -> Curve:
    c = Curve(created)
    for i in range(6):
        c.buy(1 + 2 * i, f"a{i}", 0.12)
    for i in range(10):
        c.buy(14 + i * 0.6, f"b{i}", 0.25 + 0.02 * i)
    return c


async def test_shadow_pass_records_once_and_skips_unchanged_tokens(redis, session_factory):
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=20)
    await put(redis, MINT, accelerating(created), created)
    shadow = entry_shadow.Shadow()
    first = await entry_shadow.shadow_pass(redis, session_factory, shadow, now)
    assert first["evaluated"] == 1 and first["recorded"] >= 1
    second = await entry_shadow.shadow_pass(redis, session_factory, shadow, now + timedelta(seconds=1))
    assert second["evaluated"] == 0  # no new trade: nothing recomputed
    # a restarted worker (new in-memory state) evaluates again but records nothing twice
    third = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now + timedelta(seconds=2))
    assert third["evaluated"] == 1 and third["recorded"] == 0
    async with session_factory() as s:
        rows = (await s.execute(select(EntrySignal).where(EntrySignal.strategy == ei.EARLY_ACCELERATION))).scalars().all()
    assert len(rows) == 1 and rows[0].features["trades_total"] == 16 and rows[0].evidence["mode"] == "SHADOW"
    state = await entry_store.read_state(redis, MINT)
    assert state["strategies"][ei.EARLY_ACCELERATION]["decision"] == ei.CANDIDATE
    assert state["market_cap_sol"] > 0 and state["phase"] == ei.EARLY_ACCEL_PHASE
    pts = await entry_timing.redis_points(redis, MINT)
    assert pts["event_received_at"] == round(created.timestamp() + 0.8, 3)
    assert pts["launch_observed_at"] == int(created.timestamp()) and "features_ready_at" in pts
    assert any(k.startswith("first_candidate_at:") for k in pts)
    async with session_factory() as s:
        cands = (await s.execute(select(TradingCandidate))).scalars().all()
    assert cands == []  # SHADOW mode never creates a candidate


async def test_paper_mode_creates_a_paper_only_gate_candidate_and_wakes_the_gate(redis, session_factory):
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=20)
    await put(redis, MINT, accelerating(created), created)
    async with session_factory() as s:
        s.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value={"modes": {ei.EARLY_ACCELERATION: "PAPER"}}))
        await s.commit()
    res = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now, create_strategy_candidate)
    assert res["paper_candidates"] == 1
    async with session_factory() as s:
        cand = (await s.execute(select(TradingCandidate))).scalar_one()
        sig = (await s.execute(select(EntrySignal).where(EntrySignal.strategy == ei.EARLY_ACCELERATION))).scalar_one()
    assert cand.detail["paper_only"] is True and cand.detail["entry_strategy"] == ei.EARLY_ACCELERATION
    assert sig.candidate_id == cand.id
    assert await redis.lrange(gate_events.GATE_WAKE, 0, -1) == [str(cand.id)]


async def test_paused_strategy_records_nothing(redis, session_factory):
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=20)
    await put(redis, MINT, accelerating(created), created)
    async with session_factory() as s:
        s.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value={"modes": {ei.EARLY_ACCELERATION: "PAUSED"}}))
        await s.commit()
    await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now)
    async with session_factory() as s:
        n = (await s.execute(select(EntrySignal).where(EntrySignal.strategy == ei.EARLY_ACCELERATION))).scalars().all()
    assert n == []


async def test_migrated_token_is_left_to_the_migrated_variants(redis, session_factory):
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=20)
    await put(redis, MINT, accelerating(created), created)
    await redis.hset(pump_stream.curve_key(MINT), mapping={"complete": 1, "pool": "POOL"})
    res = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now)
    assert res["evaluated"] == 0 and await entry_store.read_state(redis, MINT) is None


def test_matured_selects_launches_whose_window_just_ended():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    t = int(now.timestamp())
    assert matured([("a", t - 9), ("b", t - 11), ("c", t - 30)], now, 10) == {"b"}


async def test_fast_pass_promotes_right_after_the_window_and_records_the_baseline(redis, session_factory):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = now - timedelta(seconds=12)
    c = Curve(created)
    for i in range(3):
        c.buy(1 + i, f"a{i}", 0.1)
    for i in range(7):
        c.buy(6 + i * 0.5, f"b{i}", 0.12)
    await put(redis, MINT, c, created)
    await redis.hset(pump_stream.meta_key(MINT), mapping={"bonding_curve": "BC", "signature": "sigx"})
    counts = await fast_pass(redis, session_factory, default_settings_for("solana_fresh"), now)
    assert counts["promoted"] == 1
    async with session_factory() as s:
        cand = (await s.execute(select(TradingCandidate))).scalar_one()
        base = (await s.execute(select(EntrySignal).where(EntrySignal.strategy == ei.CURRENT_PROMOTE))).scalar_one()
    assert base.mint == MINT and base.price_raw is not None
    assert await redis.lrange(gate_events.GATE_WAKE, 0, -1) == [str(cand.id)]
    assert int(await redis.hget(FUNNEL, "fast_pass_runs")) == 1
