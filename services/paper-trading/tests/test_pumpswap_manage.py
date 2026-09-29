"""Migrated (PumpSwap) positions are priced from the canonical pool on chain
and exited by the same manage_step as curve positions."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core import paper_engine
from yonixalpha_core.db.models import PaperPosition, Token, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.solana import pumpswap
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_migrated
from yonixalpha_core.testing.pump import MINT, FakeRpc, empty_account
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.testing.pumpswap import FakePoolRpc, seed_sol_usd, trade_history

from app.gate_manage import manage_gate_positions

NOW = datetime.now(timezone.utc).replace(microsecond=0)
MIGRATED = default_settings_for("solana_migration")

BASE, QUOTE = 700_000_000_000_000, 95 * 10**9


def pool_rpc(quote_reserve: int) -> FakePoolRpc:
    pool = pumpswap.canonical_pool(MINT)
    return FakePoolRpc(MINT, BASE, quote_reserve, trade_history(pool, 25, 0, NOW - timedelta(seconds=500), every=20),
                       inner=FakeRpc(None))


async def test_migrated_position_is_priced_from_the_pool_and_stopped_out(session_factory, redis_client):
    await seed_sol_usd(redis_client, NOW)  # 95 SOL x $150: above the $10,000 usable minimum
    inp, ev = await assemble_migrated(Sources(redis_client, pool_rpc(QUOTE)), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    assert a.executable, a.reasons
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="pump_stream")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="migration", state="analyzing", state_history=[])
        s.add(cand)
        await s.flush()
        row, _ = await store.persist_assessment(s, a, cand.id, "m")
        account = await store.get_paper_account(s, "solana")
        pos = await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW,
                                               venue={"type": "pumpswap_pool", "kind": "spot", "decimals": 6})
        await s.commit()
        pid = pos.id

    # Unchanged pool: priced from chain, nothing triggers.
    c = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=30), None, None,
                                    pool_rpc(QUOTE))
    assert c["managed"] == 1 and c["closed"] == 0 and c["unpriced"] == 0

    # Quote reserve falls 40%: the pool price falls through the stop.
    c = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=60), None, None,
                                    pool_rpc(QUOTE * 60 // 100))
    assert c["closed"] == 1
    async with session_factory() as s:
        p = (await s.execute(select(PaperPosition).where(PaperPosition.id == pid))).scalar_one()
    assert p.exit_reason in ("stop_loss", "exit_intel_exit") and p.realized_pnl < 0
    assert p.realized_pnl == p.proceeds_quote - p.entry_cost_quote


async def test_pumpswap_position_without_rpc_is_unpriced_not_guessed(session_factory, redis_client):
    await seed_sol_usd(redis_client, NOW)
    inp, _ = await assemble_migrated(Sources(redis_client, pool_rpc(QUOTE)), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="pump_stream")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="migration", state="analyzing", state_history=[])
        s.add(cand)
        await s.flush()
        row, _ = await store.persist_assessment(s, a, cand.id, "m")
        account = await store.get_paper_account(s, "solana")
        await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW,
                                         venue={"type": "pumpswap_pool", "kind": "spot", "decimals": 6})
        await s.commit()
    c = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(seconds=30))
    assert c == {"managed": 0, "closed": 0, "unpriced": 1}


async def test_fast_loop_reprices_pool_positions_at_most_every_rpc_interval(session_factory, redis_client):
    """The position loop runs every 2 s; a pool-priced position is re-read
    from chain at most every rpc_min_interval_seconds, and a stop is still
    taken on the next due pass."""
    await seed_sol_usd(redis_client, NOW)
    inp, _ = await assemble_migrated(Sources(redis_client, pool_rpc(QUOTE)), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="pump_stream")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="migration", state="analyzing", state_history=[])
        s.add(cand)
        await s.flush()
        row, _ = await store.persist_assessment(s, a, cand.id, "m")
        account = await store.get_paper_account(s, "solana")
        await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW,
                                         venue={"type": "pumpswap_pool", "kind": "spot", "decimals": 6})
        await s.commit()
    t1 = NOW + timedelta(seconds=30)
    c = await manage_gate_positions(session_factory, redis_client, None, t1, None, None, pool_rpc(QUOTE), rpc_min_interval_seconds=5)
    assert c["managed"] == 1
    crashed = pool_rpc(QUOTE * 60 // 100)
    c = await manage_gate_positions(session_factory, redis_client, None, t1 + timedelta(seconds=2), None, None, crashed,
                                    rpc_min_interval_seconds=5)
    assert c["skipped_not_due"] == 1 and c["managed"] == 0  # 2 s after the last read: not re-read yet
    c = await manage_gate_positions(session_factory, redis_client, None, t1 + timedelta(seconds=5), None, None, crashed,
                                    rpc_min_interval_seconds=5)
    assert c["closed"] == 1  # due again: the stop is taken
