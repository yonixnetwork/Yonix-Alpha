"""End to end on synthetic but byte-exact pump.fun data: encoded event logs
-> Redis stream store -> assembler (fake RPC) -> safety gate -> persisted
assessment -> paper entry -> take-profit / trailing-stop management.

Needs the local test Postgres and Redis (db 9)."""

import os
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import paper_engine  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import MLFeatureSnapshot, Token, TradeTimelineEvent, TradingCandidate  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import FinalDecision  # noqa: E402
from yonixalpha_core.safety.rules import BlacklistRule, CustomRule  # noqa: E402
from yonixalpha_core.safety.settings import default_settings_for  # noqa: E402
from yonixalpha_core.solana import pump_stream  # noqa: E402
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh  # noqa: E402
from yonixalpha_core.solana.pumpfun import TRADE_EVENT  # noqa: E402
from yonixalpha_core.state_machine import CandidateState  # noqa: E402
from yonixalpha_core.testing.pump import (  # noqa: E402
    CREATOR,
    CURVE,
    MINT,
    Curve,
    FakeRpc,
    b,
    empty_account,
    i64,
    logs_of,
    pk,
    s,
    seed_healthy_launch,
    u64,
    wallet,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FRESH = default_settings_for("solana_fresh")


async def seed_stream(redis):
    return await seed_healthy_launch(redis, NOW)


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def controls(**kw) -> Controls:
    return Controls(settings=kw.pop("settings", FRESH), account=empty_account(), **kw)


async def test_stream_ingest_decodes_and_counts(redis):
    await seed_stream(redis)
    st = await pump_stream.stats(redis)
    assert st["create"] == 1 and st["trade"] == 40 and st["notifications"] == 2
    meta = await pump_stream.load_meta(redis, MINT)
    assert meta["symbol"] == "PIPE" and meta["bonding_curve"] == CURVE and meta["creator"] == CREATOR
    curve = await pump_stream.load_curve(redis, MINT)
    assert curve.fee_bps == 125 and not curve.complete  # protocol 95 + creator 30; buyback 5000 is a split
    trades = await pump_stream.load_trades(redis, MINT)
    assert len(trades) == 40 and trades[0].at < trades[-1].at
    assert await pump_stream.recent_unpromoted(redis, NOW, 3600) == [(MINT, int((NOW - timedelta(minutes=20)).timestamp()))]
    assert await pump_stream.mark_promoted(redis, MINT, NOW)
    assert not await pump_stream.mark_promoted(redis, MINT, NOW)
    assert await pump_stream.recent_unpromoted(redis, NOW, 3600) == []


async def test_a_zero_fee_event_never_makes_the_curve_look_free(redis):
    """A trade reporting 0 total fee keeps the last real rate; with no real
    rate ever seen the fee stays unknown, so the gate can't simulate (and
    blocks) instead of planning without fees."""
    c = Curve()
    await pump_stream.ingest_logs(redis, logs_of(c.trade(wallet(1), NOW, 10**8, True, protocol_bps=0, creator_bps=0)), None, NOW)
    assert (await pump_stream.load_curve(redis, MINT)).fee_bps is None
    await pump_stream.ingest_logs(redis, logs_of(c.trade(wallet(2), NOW, 10**8, True)), None, NOW)
    await pump_stream.ingest_logs(redis, logs_of(c.trade(wallet(3), NOW, 10**8, True, protocol_bps=0, creator_bps=0)), None, NOW)
    curve = await pump_stream.load_curve(redis, MINT)
    assert curve.fee_bps == 125 and curve.vsol == c.vsol  # reserves still follow every trade


async def test_non_sol_quoted_events_are_skipped(redis):
    body = pk(MINT) + u64(1) + u64(1) + b(True) + pk(CREATOR) + i64(int(NOW.timestamp()))
    body += u64(1) * 4 + pk(CREATOR) + u64(95) + u64(0) + pk(CREATOR) + u64(30) + u64(0)
    body += b(False) + u64(0) * 3 + i64(0) + s("buy") + b(False) + u64(0) * 4 + struct.pack("<I", 0) + pk(wallet(99))
    counts = await pump_stream.ingest_logs(redis, logs_of(TRADE_EVENT + body), None, NOW)
    assert counts == {"skipped_non_sol": 1}
    assert await pump_stream.load_trades(redis, MINT) == []


async def test_assembled_healthy_launch_executes_in_paper(redis):
    curve = await seed_stream(redis)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls())
    a = assess(inp, FRESH)
    assert not ev["errors"], ev["errors"]
    assert inp.holders.top1_share == Decimal("0.02") and inp.holders.excluded_pool_accounts == 1
    assert inp.flow.unique_buyers >= 10 and inp.signal.qualified, inp.signal.reasons
    assert a.decision == FinalDecision.EXECUTE, a.reasons
    assert a.execution_target.value == "PAPER" and a.plan.complete


async def test_active_mint_authority_is_rejected_from_live_rpc_shape(redis):
    curve = await seed_stream(redis)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve, mint_authority=CREATOR)), MINT, NOW, controls())
    a = assess(inp, FRESH)
    assert a.decision == FinalDecision.REJECT


async def test_rpc_failures_become_missing_data_not_guesses(redis):
    curve = await seed_stream(redis)
    rpc = FakeRpc(curve, fail={"getTokenLargestAccounts"})
    inp, ev = await assemble_fresh(Sources(redis, rpc), MINT, NOW, controls())
    assert inp.holders is None and any("holders" in e for e in ev["errors"])
    a = assess(inp, FRESH)
    assert a.decision == FinalDecision.NO_TRADE and "HOLDERS_UNAVAILABLE" in {f.code for f in a.findings}


async def test_dead_stream_makes_flow_stale(redis):
    curve = await seed_stream(redis)
    await redis.set(pump_stream.HEARTBEAT, (NOW - timedelta(minutes=10)).isoformat())
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls())
    a = assess(inp, FRESH)
    assert a.decision == FinalDecision.NO_TRADE and "FLOW_STALE" in {f.code for f in a.findings}


async def test_blacklist_and_custom_rules_apply(redis):
    curve = await seed_stream(redis)
    bl = [BlacklistRule("1", "GLOBAL", "symbol", "pip*", "pattern")]
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls(blacklist=bl))
    assert inp.blacklisted_by and assess(inp, FRESH).decision == FinalDecision.REJECT

    rule = [CustomRule("2", "need 100 trades", "trade_count", "<", "100", "WAIT", "solana_fresh")]
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls(custom_rules=rule))
    assert assess(inp, FRESH).decision == FinalDecision.WAIT


async def test_paper_entry_partial_tp_and_trailing_exit(redis, db):
    curve = await seed_stream(redis)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls())
    a = assess(inp, FRESH)
    assert a.executable

    token = Token(mint_address=MINT, symbol="PIPE", first_seen_source="test")
    db.add(token)
    await db.flush()
    cand = TradingCandidate(token_id=token.id, engine="discovery", state=CandidateState.OBSERVING.value, state_history=[])
    db.add(cand)
    await db.flush()
    db.add(MLFeatureSnapshot(candidate_id=cand.id, symbol="PIPE", features={"x": 1}))
    account = await store.get_paper_account(db, "solana")
    row, _ = await store.persist_assessment(db, a, cand.id, "k")
    pos = await paper_engine.open_position(db, account, a, row.id, cand, inp.liquidity_model, None, None, NOW)
    await db.commit()

    size = a.plan.position_size.value
    assert account.cash_balance == Decimal(10) - size
    assert pos.entry_price > a.plan.entry_price  # fill includes fee + impact
    assert cand.state == CandidateState.MANAGING.value

    tp1 = Decimal(pos.plan["take_profits"][0]["price"]["value"])
    model = inp.liquidity_model
    # Price reaches TP1: 40% exits, trailing activates.
    r1 = await paper_engine.apply_step(db, pos, account, tp1 * Decimal("1.01"), None, None, NOW + timedelta(minutes=1))
    assert [x[1] for x in r1.exits] == ["take_profit_1"] and pos.trailing_stop is not None
    assert pos.remaining_quantity == pos.initial_quantity * Decimal("0.6")
    trail = pos.trailing_stop
    # Falls through the trailing stop: remainder exits, position closes.
    r2 = await paper_engine.apply_step(db, pos, account, trail * Decimal("0.99"), None, None, NOW + timedelta(minutes=2))
    await db.commit()
    assert r2.closed and pos.status == "closed" and pos.exit_reason == "trailing_stop"
    assert pos.realized_pnl == pos.proceeds_quote - pos.entry_cost_quote
    assert account.cash_balance == Decimal(10) - size + pos.proceeds_quote
    assert cand.state == CandidateState.CLOSED.value
    label = (await db.execute(select(MLFeatureSnapshot.label))).scalar_one()
    assert label == (1 if pos.realized_pnl > 0 else 0)
    kinds = [e.event_type for e in (await db.execute(select(TradeTimelineEvent).order_by(TradeTimelineEvent.occurred_at))).scalars()]
    assert kinds[0] == "assessed.execute" and "paper_entry" in kinds and kinds[-1] == "paper_closed"
    assert model is not None


def test_stop_is_checked_before_take_profit_on_a_gap():
    s = paper_engine.PositionState(Decimal(100), Decimal(100), Decimal("0.9"), [(Decimal("1.2"), Decimal("0.5"))], [],
                                   True, Decimal("0.1"), Decimal("1.2"), None, None, None)
    r = paper_engine.manage_step(s, Decimal("0.85"))
    assert r.closed and r.exits == [(Decimal(100), "stop_loss")]


def test_trailing_stop_never_loosens_across_steps():
    s = paper_engine.PositionState(Decimal(100), Decimal(100), Decimal("0.9"), [(Decimal("1.1"), Decimal("0.4"))], [],
                                   True, Decimal("0.1"), Decimal("1.1"), None, None, None)
    paper_engine.manage_step(s, Decimal("1.2"))
    high = s.trailing_stop
    paper_engine.manage_step(s, Decimal("1.15"))
    assert s.trailing_stop == high == Decimal("1.2") * Decimal("0.9")
    assert s.remaining_quantity == Decimal(60)


def test_exit_fill_on_curve_model_charges_fee_and_transfer_fee():
    from yonixalpha_core.safety.liquidity import ConstantProductModel

    m = ConstantProductModel(Decimal(60), Decimal(500_000_000), Decimal(100), Decimal(30))
    plain = paper_engine.exit_fill(Decimal(1_000_000), m, None, None, None, "x")
    taxed = paper_engine.exit_fill(Decimal(1_000_000), m, None, None, 100, "x")
    assert plain.quote_amount < Decimal(1_000_000) * m.marginal_price
    assert taxed.quote_amount < plain.quote_amount


def test_entry_without_any_venue_is_refused():
    with pytest.raises(paper_engine.FillError):
        paper_engine.entry_fill(Decimal(1), None, None, Decimal(1), None, None)


def test_token_text_is_cleaned_of_nul_and_control_characters():
    from yonixalpha_core.solana.codec import clean_text, db_safe

    assert clean_text("spaceX链游\x00") == "spaceX链游"
    assert clean_text("A\x00B\x07C\n", 2) == "AB"
    assert clean_text("🚀 Moon 👨‍👩‍👧") == "🚀 Moon 👨‍👩‍👧"  # emoji joiners are not control characters
    assert clean_text(None) == ""
    assert db_safe({"n\x00": ["a\x00", {"b": "c\x00"}], "k": 5}) == {"n": ["a", {"b": "c"}], "k": 5}


async def test_create_event_name_with_nul_bytes_is_cleaned_at_ingestion(redis):
    from yonixalpha_core.solana.pumpfun import CREATE_EVENT
    from yonixalpha_core.testing.pump import CREATOR, CURVE, i64

    body = s("spaceX链游\x00\x00") + s("SPX\x00") + s("https://x/m.json\x00") + pk(MINT) + pk(CURVE) + pk(CREATOR) + pk(CREATOR)
    await pump_stream.ingest_logs(redis, logs_of(CREATE_EVENT + body + i64(int(NOW.timestamp()))), "sig", NOW)
    raw = await redis.hgetall(pump_stream.meta_key(MINT))
    assert raw["name"] == "spaceX链游" and raw["symbol"] == "SPX" and raw["uri"] == "https://x/m.json"


async def test_assembled_launch_carries_causal_intelligence_without_changing_the_decision(redis):
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, controls())
    intel = ev["intel"]
    assert intel is inp.intel and intel["stage"] == "FRESH" and intel["feature_version"].startswith("launch-")
    assert intel["regime"]["mayhem"] is False and intel["regime"]["curve_math"]["valid"] is True
    assert intel["regime"]["data_regime"] == "post_boost"
    assert intel["snapshots"] and intel["snapshots"][0]["offset_seconds"] == 0
    assert intel["manipulation"]["level"] in ("NONE", "LOW", "UNKNOWN") and intel["flow_state"]["state"]
    assert intel["buyer_breadth"]["score"] is None or 0 <= intel["buyer_breadth"]["score"] <= 1
    a = assess(inp, default_settings_for("solana_fresh"))
    assert a.decision == FinalDecision.EXECUTE, a.reasons  # the healthy launch still executes
