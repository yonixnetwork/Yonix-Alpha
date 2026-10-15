"""R4 (2026-10-15): a paper position bought on the curve whose token migrates
is not managed for the measured LIVE sell latency (a LIVE sell has to be
re-routed to the pool first), then sells one take-profit level per tick at
each tick's pool price, instead of every level at once at the jump price."""

from datetime import timedelta

from sqlalchemy import select

from yonixalpha_core import paper_engine
from yonixalpha_core import paper_execution as pe
from yonixalpha_core.db.models import PaperPosition, Token, TradeTimelineEvent, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_migrated
from yonixalpha_core.testing.pump import MINT, empty_account
from yonixalpha_core.testing.pumpswap import seed_sol_usd

from app.gate_manage import manage_gate_positions
from tests.test_pumpswap_manage import MIGRATED, NOW, QUOTE, pool_rpc


async def curve_position(session_factory, redis_client):
    await seed_sol_usd(redis_client, NOW)
    inp, _ = await assemble_migrated(Sources(redis_client, pool_rpc(QUOTE)), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    assert a.executable, a.reasons
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="pump_stream")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state="analyzing", state_history=[])
        s.add(cand)
        await s.flush()
        row, _ = await store.persist_assessment(s, a, cand.id, "c")
        account = await store.get_paper_account(s, "solana")
        # bought on the bonding curve (the venue it had at entry)
        pos = await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW,
                                               venue={"type": "pump_curve", "kind": "spot", "decimals": 6})
        pos.lifecycle, pos.engine = "FRESH", "solana_fresh"
        await s.commit()
        pid = pos.id
    # the stream says the curve completed: the position is priced from the pool from now on
    await redis_client.set(pump_stream.HEARTBEAT, (NOW + timedelta(seconds=60)).isoformat())
    await redis_client.hset(pump_stream.curve_key(MINT), mapping={"vsol": 1, "vtok": 1, "updated_at": int(NOW.timestamp()),
                                                                  "complete": 1})
    return pid


async def test_the_migration_jump_is_not_sold_at_once(session_factory, redis_client):
    pid = await curve_position(session_factory, redis_client)
    jump = pool_rpc(QUOTE * 3)  # the pool opens far above every take-profit level

    c = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=30), None, None, jump)
    assert c.get("crossing_pending") == 1 and c["closed"] == 0
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert p.lifecycle == "MIGRATED" and not p.tp_hits and p.remaining_quantity == p.initial_quantity
        assert p.plan[pe.CROSSING_KEY]["latency_s"] == 3.0  # the stated default: no LIVE sells measured
        ev = (await s.execute(select(TradeTimelineEvent).where(
            TradeTimelineEvent.event_type == "paper_migration_crossing"))).scalar_one()
        assert ev.detail["unmanaged_seconds"] == 3.0

    # still inside the LIVE sell latency: not managed
    c = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=32), None, None, jump)
    assert c.get("crossing_pending") == 1

    # after it: one take-profit level per tick
    for i, at in enumerate((40, 50), start=1):
        await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=at), None, None, jump)
        async with session_factory() as s:
            p = await s.get(PaperPosition, pid)
            assert len(p.tp_hits) == i, p.tp_hits
