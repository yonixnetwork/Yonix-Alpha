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


# --- routing, reason counters and demotion (regression recovery, 2026-10-10) ------------------------

def broad(created: datetime) -> Curve:
    """Both EARLY_ACCELERATION and EARLY_DEMAND_CONFIRMATION qualify."""
    c = Curve(created)
    for i in range(8):
        c.buy(1 + i * 2, f"a{i}", 0.12)
    for i in range(12):
        c.buy(18 + i * 0.6, f"b{i}", 0.2 + 0.02 * i)
    return c


async def test_two_paper_strategies_on_one_token_open_one_candidate_and_never_re_enter(redis, session_factory):
    from yonixalpha_core import strategy_registry as reg

    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=26)
    c = broad(created)
    await put(redis, MINT, c, created)
    async with session_factory() as s:
        s.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value={"modes": {ei.EARLY_ACCELERATION: "PAPER",
                                                                             ei.EARLY_DEMAND_CONFIRMATION: "PAPER"}}))
        await s.commit()
    res = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now, create_strategy_candidate)
    assert res["paper_candidates"] == 1
    state = await entry_store.read_state(redis, MINT)
    assert state["category"] == ei.FRESH and state["paper_route"]["selected"] in (ei.EARLY_ACCELERATION,
                                                                                  ei.EARLY_DEMAND_CONFIRMATION)
    routed = state["paper_route"]["selected"]
    assert await redis.get(reg.ROUTE_CLAIM + MINT) == routed
    async with session_factory() as s:
        sigs = (await s.execute(select(EntrySignal).where(EntrySignal.mint == MINT))).scalars().all()
        cands = (await s.execute(select(TradingCandidate))).scalars().all()
    assert len(cands) == 1 and cands[0].detail["entry_strategy"] == routed
    by = {x.strategy: x for x in sigs}
    assert by[routed].candidate_id == cands[0].id and by[routed].evidence["strategy_version"].startswith("F")
    other = ({ei.EARLY_ACCELERATION, ei.EARLY_DEMAND_CONFIRMATION} - {routed}).pop()
    assert by[other].candidate_id is None  # recorded for measurement, never a second trade on the token
    # more trades later (or a loss on the first trade): the token is never routed again
    for i in range(6):
        c.buy(27 + i, f"z{i}", 0.4)
    await redis.delete(pump_stream.trades_key(MINT))
    await put(redis, MINT, c, created)
    async with session_factory() as s:  # the first paper trade closed (a loss, say): its candidate is CLOSED
        cand = (await s.execute(select(TradingCandidate))).scalar_one()
        cand.state = "closed"
        await s.commit()
    for k in await redis.keys(entry_store.RECORDED_KEY + "*"):  # so only the route claim can stop a second entry
        await redis.delete(k)
    again = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now + timedelta(seconds=8),
                                           create_strategy_candidate)
    assert again["evaluated"] == 1 and (await entry_store.read_state(redis, MINT))["paper_route"]["selected"]
    assert again["paper_candidates"] == 0
    async with session_factory() as s:
        assert len((await s.execute(select(TradingCandidate))).scalars().all()) == 1


async def test_shadow_strategies_never_trade_and_blocking_reasons_are_counted(redis, session_factory):
    now = datetime.now(timezone.utc)
    created = now - timedelta(seconds=26)
    await put(redis, MINT, broad(created), created)
    res = await entry_shadow.shadow_pass(redis, session_factory, entry_shadow.Shadow(), now, create_strategy_candidate)
    assert res["paper_candidates"] == 0  # all strategies in SHADOW (default)
    state = await entry_store.read_state(redis, MINT)
    assert state["route"]["selected"] is not None and state["paper_route"]["decision"] == ei.NO_TRADE
    top = await entry_shadow.top_reasons(redis, ei.MOMENTUM_CONTINUATION, now)
    assert top and top[0][0] == "younger than Ns: continuation needs a history" and top[0][1] == 1
    day = now.strftime("%Y%m%d")
    assert int(await redis.hget(entry_shadow.ROUTE_STATS + day, f"selected:{state['route']['selected']}")) == 1
    assert 0 < await redis.ttl(entry_shadow.ROUTE_STATS + day) <= entry_shadow.STATS_TTL


async def test_reliably_losing_paper_strategy_is_demoted_to_shadow_and_audited(redis, session_factory):
    from yonixalpha_core import strategy_registry as reg
    from yonixalpha_core.db.models import AuditLog

    now = datetime.now(timezone.utc)
    async with session_factory() as s:
        s.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value={"modes": {ei.EARLY_ACCELERATION: "PAPER",
                                                                             ei.BREAKOUT_RETEST: "PAPER"}}))
        for i in range(40):
            for name, ret in ((ei.EARLY_ACCELERATION, -5.0 + (i % 3)), (ei.BREAKOUT_RETEST, 2.0 - (i % 2) * 3)):
                await entry_store.record_signal(s, mint=f"m{i:03d}", strategy=name, lifecycle="FRESH", decision=ei.CANDIDATE,
                                                decided_at=now - timedelta(minutes=60 - i))
        await s.commit()
        for row in (await s.execute(select(EntrySignal))).scalars().all():
            i = int(row.mint[1:])
            row.outcome = {"executable_return_pct": -5.0 + (i % 3) if row.strategy == ei.EARLY_ACCELERATION
                           else 2.0 - (i % 2) * 3}
            row.outcome_at = now
        await s.commit()
    async with session_factory() as s:
        out = await reg.apply_demotions(s, now)
    assert out == {"checked": 2, "demoted": [ei.EARLY_ACCELERATION]}
    async with session_factory() as s:
        st = await entry_store.load_settings(s)
        audit = (await s.execute(select(AuditLog).where(AuditLog.event_type == "entry_intel.strategy_demoted"))).scalars().all()
    assert st["modes"][ei.EARLY_ACCELERATION] == "SHADOW" and st["modes"][ei.BREAKOUT_RETEST] == "PAPER"
    assert len(audit) == 1 and audit[0].detail["evidence"]["upper_95_pct"] < 0
    async with session_factory() as s:  # run again: nothing left to demote, nothing promoted
        assert (await reg.apply_demotions(s, now))["demoted"] == []
