"""Holder monitoring after entry and simulated paper execution failures,
through the real management loop."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core import paper_engine, paper_execution
from yonixalpha_core.db.models import PaperPosition, PlatformSetting, Token, TradeTimelineEvent, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.testing.pump import CREATOR, CURVE, MINT, SUPPLY, FakeRpc, empty_account, logs_of, seed_healthy_launch, wallet

from app.gate_manage import manage_gate_positions

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRESH = default_settings_for("solana_fresh")
# At entry the creator held 6%; FakeRpc's current top holders do not include the creator.
ENTRY_HOLDERS = {"top1_share": "0.02", "top10_share": "0.20", "creator_share": "0.06"}


async def open_position(session_factory, redis, holders_at_entry=ENTRY_HOLDERS):
    curve = await seed_healthy_launch(redis, NOW)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="t")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state="observing", state_history=[])
        s.add(cand)
        await s.flush()
        account = await store.get_paper_account(s, "solana")
        row, _ = await store.persist_assessment(s, a, cand.id, "k")
        venue = {"type": "pump_curve", "decimals": 6, "creator": CREATOR,
                 "real_liquidity_at_entry": str(inp.market.liquidity_quote),
                 "holders_at_entry": holders_at_entry, "supply_raw": str(SUPPLY), "holder_excluded": [CURVE]}
        pos = await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW, venue=venue)
        await s.commit()
        return curve, pos.id


async def one_sell(redis, curve, at):
    ev = curve.trade(wallet(60), at, 300_000_000, False)  # sell pressure, a single seller
    await pump_stream.ingest_logs(redis, logs_of(ev), f"sig-{at.timestamp()}", at)


async def kinds(session_factory, pid):
    async with session_factory() as s:
        return [e.event_type for e in (await s.execute(
            select(TradeTimelineEvent).where(TradeTimelineEvent.position_id == pid)
            .order_by(TradeTimelineEvent.occurred_at))).scalars()]


async def test_holder_change_plus_sell_pressure_exits_through_the_loop(session_factory, redis_client):
    curve, pid = await open_position(session_factory, redis_client)
    later = NOW + timedelta(minutes=5)
    await one_sell(redis_client, curve, later - timedelta(seconds=5))
    rpc = FakeRpc(curve)
    counts = await manage_gate_positions(session_factory, redis_client, None, later, None, None, rpc)
    assert counts["closed"] == 1
    assert "getTokenLargestAccounts" in rpc.calls
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        ev = (await s.execute(select(TradeTimelineEvent).where(
            TradeTimelineEvent.position_id == pid, TradeTimelineEvent.event_type == "exit_intelligence.exit"))).scalar_one()
    assert p.exit_reason == "exit_intel_exit"
    assert any("creator holdings fell" in r for r in ev.detail["reasons"])
    snap = json.loads(await redis_client.get(f"yx:holders:{pid}"))  # cached until the next minute
    assert snap["creator_share"] is not None


async def test_same_market_without_a_holder_change_holds(session_factory, redis_client):
    curve, pid = await open_position(session_factory, redis_client, holders_at_entry={**ENTRY_HOLDERS, "creator_share": "0"})
    later = NOW + timedelta(minutes=5)
    await one_sell(redis_client, curve, later - timedelta(seconds=5))
    counts = await manage_gate_positions(session_factory, redis_client, None, later, None, None, FakeRpc(curve))
    assert counts["closed"] == 0
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"


async def test_without_rpc_holders_are_not_assumed(session_factory, redis_client):
    curve, pid = await open_position(session_factory, redis_client)
    later = NOW + timedelta(minutes=5)
    await one_sell(redis_client, curve, later - timedelta(seconds=5))
    counts = await manage_gate_positions(session_factory, redis_client, None, later)
    assert counts["closed"] == 0  # one metric (sell pressure) alone never sells


async def _set_exit_failure(session_factory, pct: str):
    async with session_factory() as s:
        s.add(PlatformSetting(key=paper_execution.SETTINGS_KEY, value={"exit_failure_pct": pct}))
        await s.commit()


async def test_simulated_exit_failure_keeps_position_open_then_retries(session_factory, redis_client, monkeypatch):
    curve, pid = await open_position(session_factory, redis_client)
    await _set_exit_failure(session_factory, "30")
    later = NOW + timedelta(seconds=30)
    dump = [curve.trade(wallet(50 + i), later, 2_000_000_000, False) for i in range(12)]
    await pump_stream.ingest_logs(redis_client, logs_of(*dump), "sig-dump", later)

    monkeypatch.setattr(paper_execution, "draw", lambda key: Decimal(0))  # this attempt fails
    c1 = await manage_gate_positions(session_factory, redis_client, None, later)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
    assert c1.get("exit_failed") == 1 and c1["closed"] == 0
    assert p.status == "open" and p.exit_failures == 1 and p.remaining_quantity == p.initial_quantity
    assert p.last_price is not None and p.lowest_price <= p.last_price

    monkeypatch.setattr(paper_execution, "draw", lambda key: Decimal("0.99"))  # the retry lands
    later2 = later + timedelta(seconds=15)
    await pump_stream.ingest_logs(redis_client, logs_of(curve.trade(wallet(90), later2, 100_000_000, False)), "sig-2", later2)
    c2 = await manage_gate_positions(session_factory, redis_client, None, later2)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
    assert c2["closed"] == 1 and p.status == "closed" and p.exit_failures == 0
    assert p.realized_pnl == p.proceeds_quote - p.entry_cost_quote
    k = await kinds(session_factory, pid)
    assert k.index("paper_exit_failed") < k.index("paper_closed")


async def test_simulated_failures_cannot_block_an_exit_forever(session_factory, redis_client, monkeypatch):
    curve, pid = await open_position(session_factory, redis_client)
    await _set_exit_failure(session_factory, "50")
    monkeypatch.setattr(paper_execution, "draw", lambda key: Decimal(0))  # every draw fails
    t = NOW + timedelta(seconds=30)
    dump = [curve.trade(wallet(50 + i), t, 2_000_000_000, False) for i in range(12)]
    await pump_stream.ingest_logs(redis_client, logs_of(*dump), "sig-dump", t)
    closed = False
    for i in range(8):
        t = t + timedelta(seconds=15)
        await pump_stream.ingest_logs(redis_client, logs_of(curve.trade(wallet(91 + i), t, 10_000_000, False)), f"s{i}", t)
        c = await manage_gate_positions(session_factory, redis_client, None, t)
        if c["closed"]:
            closed = True
            break
    assert closed and i == 5  # 5 simulated failures, then the exit is filled
