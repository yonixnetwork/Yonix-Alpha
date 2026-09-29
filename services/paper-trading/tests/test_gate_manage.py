from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core import paper_engine
from yonixalpha_core.db.models import PaperAccount, PaperPosition, RiskAssessment, Token, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, empty_account, logs_of, seed_healthy_launch, wallet

from app.gate_manage import manage_gate_positions, track_outcomes

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRESH = default_settings_for("solana_fresh")


async def open_gate_position(session_factory, redis):
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    a.inputs_snapshot = ev
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="t")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state="observing", state_history=[])
        s.add(cand)
        await s.flush()
        account = await store.get_paper_account(s, "solana")
        row, _ = await store.persist_assessment(s, a, cand.id, "k")
        pos = await paper_engine.open_position(s, account, a, row.id, cand, inp.liquidity_model, None, None, NOW,
                                               venue={"type": "pump_curve", "decimals": 6})
        await s.commit()
        return curve, pos.id, a


async def push_trades(redis, curve, sells: int, at: datetime):
    events = [curve.trade(wallet(50 + i), at, 2_000_000_000, False) for i in range(sells)]
    await pump_stream.ingest_logs(redis, logs_of(*events), "sig-dump", at)


async def test_crash_through_stop_closes_at_curve_simulated_price(session_factory, redis_client):
    curve, pid, a = await open_gate_position(session_factory, redis_client)
    later = NOW + timedelta(seconds=30)
    await push_trades(redis_client, curve, sells=12, at=later)  # heavy selling drops the curve price
    counts = await manage_gate_positions(session_factory, redis_client, None, later)
    assert counts["closed"] == 1
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        acct = await s.get(PaperAccount, p.account_id)
        cand = await s.get(TradingCandidate, p.candidate_id)
    assert p.exit_reason == "stop_loss" and p.realized_pnl < 0
    # Exit simulated on the curve itself, so the realized loss includes the
    # fee and impact of selling into the post-crash reserves.
    assert p.proceeds_quote < p.initial_quantity * curve.price()
    assert acct.cash_balance == Decimal(10) - p.entry_cost_quote + p.proceeds_quote
    assert cand.state == CandidateState.CLOSED.value


async def test_stale_stream_leaves_position_untouched(session_factory, redis_client):
    curve, pid, _ = await open_gate_position(session_factory, redis_client)
    later = NOW + timedelta(minutes=5)
    await redis_client.set(pump_stream.HEARTBEAT, (later - timedelta(minutes=3)).isoformat())
    counts = await manage_gate_positions(session_factory, redis_client, None, later)
    assert counts == {"managed": 0, "closed": 0, "unpriced": 1}
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"


async def test_outcome_is_tracked_for_opportunities_not_taken(session_factory, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    inp, ev = await assemble_fresh(Sources(redis_client, FakeRpc(curve, mint_authority="X" * 32)), MINT, NOW,
                                   Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    a.inputs_snapshot = ev
    async with session_factory() as s:
        await store.persist_assessment(s, a, None, "k-old")
        a.evaluated_at = NOW + timedelta(seconds=30)
        await store.persist_assessment(s, a, None, "k-new")
        await s.commit()
    await push_trades(redis_client, curve, sells=3, at=NOW + timedelta(minutes=10))
    async with session_factory() as s:
        assert await track_outcomes(s, redis_client, NOW + timedelta(minutes=20)) == 1
        rows = {r.idempotency_key: r for r in (await s.execute(select(RiskAssessment))).scalars()}
    new, old = rows["k-new"].outcome, rows["k-old"].outcome
    assert Decimal(new["change_pct"]) < 0 and new["price_at_decision"] and not new["graduated"]
    assert old == {"superseded_by": str(rows["k-new"].id)}


async def test_evm_positions_are_left_to_data_evm(session_factory, redis_client):
    """BSC / Robinhood paper positions are priced by services/data-evm; the
    Solana manager must neither price them nor report them unpriced."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from yonixalpha_core.db.models import PaperPosition

    from app.gate_manage import manage_gate_positions

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add(PaperPosition(symbol="MOON", provider="paper", side="LONG", entry_price=Decimal("0.000001"),
                                  quantity=Decimal("20000"), stop_loss=Decimal("0.0000009"), take_profit=[], entry_at=now,
                                  status="open", engine="evm_bsc", asset_id="0x1111111111111111111111111111111111111111",
                                  plan={"venue": {"kind": "spot", "chain": "bsc", "launchpad": "fourmeme"}}))
        await session.commit()
    counts = await manage_gate_positions(session_factory, redis_client, None, now)
    assert counts["managed"] == 0 and counts["unpriced"] == 0 and not counts.get("failed")
