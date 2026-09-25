from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core.db.models import MLFeatureSnapshot, PaperPosition, RiskAssessment, Token, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, seed_healthy_launch

from app.gate_eval import evaluate_with_gate, is_gate_candidate

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ENV = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)


async def make_candidate(db, engine="discovery") -> TradingCandidate:
    token = Token(mint_address=MINT, symbol="PIPE", first_seen_source="pump_stream")
    db.add(token)
    await db.flush()
    c = TradingCandidate(token_id=token.id, engine=engine, state=CandidateState.DISCOVERED.value,
                         state_history=[{"state": "discovered", "at": NOW.isoformat(), "reason": "t"}],
                         detail={"source": "pump_stream", "mint": MINT, "strategy": "solana_fresh"})
    db.add(c)
    await db.commit()
    return c


async def test_healthy_launch_opens_paper_position_from_gate_plan(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    assert is_gate_candidate(cand)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    assert cand.state == CandidateState.MANAGING.value
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    assert pos.assessment_id is not None and pos.engine == "solana_fresh"
    assert pos.entry_cost_quote == a.plan.position_size.value
    venue = pos.plan["venue"]
    assert venue["type"] == "pump_curve" and venue["decimals"] == 6 and venue["kind"] == "spot"
    assert venue["simulator"] and venue["transfer_fee_bps"] is None
    assert (await db_session.execute(select(MLFeatureSnapshot))).scalar_one().label is None
    # Pacing: an immediate second call is skipped, not re-assessed.
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW) is None


async def test_mint_authority_rejects_and_is_recorded(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve, mint_authority="X" * 32)),
                                 cand, NOW)
    assert a.decision.value == "REJECT" and cand.state == CandidateState.REJECTED.value
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert row.decision == "REJECT" and row.assessment["inputs_snapshot"]["features"]
    assert (await db_session.execute(select(PaperPosition))).first() is None


async def test_manual_mode_waits_for_approval_then_re_checks_everything(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await store.set_global_mode(db_session, GlobalMode.MANUAL, None)
    await db_session.commit()
    cand = await make_candidate(db_session)
    src = Sources(redis_client, FakeRpc(curve))
    a = await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW)
    assert a.decision.value == "REQUIRE_MANUAL_APPROVAL" and cand.state == CandidateState.WAITING_FOR_APPROVAL.value
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert row.approval_state == "PENDING"

    row.approval_state, row.approved_at = "APPROVED", NOW
    await db_session.commit()
    await redis_client.delete(f"yx:gate:pace:{cand.id}")
    # Approval granted, but the token turned unsafe meanwhile: still refused.
    later = NOW + timedelta(seconds=40)
    b = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve, mint_authority="X" * 32)),
                                 cand, later)
    assert b.decision.value == "REJECT"


async def test_strategy_off_rejects_without_assessing(db_session, redis_client):
    await store.set_strategy_mode(db_session, "solana_fresh", StrategyMode.OFF, None)
    await db_session.commit()
    cand = await make_candidate(db_session)
    rpc = FakeRpc(None)
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW) is None
    assert cand.state == CandidateState.REJECTED.value and rpc.calls == []


async def test_waiting_candidate_times_out(db_session, redis_client):
    await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    src = Sources(redis_client, FakeRpc(None, fail={"getAccountInfo", "getTokenLargestAccounts"}))
    a = await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW)
    assert a.decision.value == "NO_TRADE" and cand.state == CandidateState.ANALYZING.value
    await redis_client.delete(f"yx:gate:pace:{cand.id}")
    await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW + timedelta(minutes=31))
    assert cand.state == CandidateState.REJECTED.value


async def test_momentum_candidate_uses_momentum_strategy(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session, engine="momentum")
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.engine == "solana_momentum" and a.strategy == "solana_momentum"
    assert any("solana_momentum" in f.message for f in a.findings if f.code == "SIGNAL_NOT_QUALIFIED") or a.qualified
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert row.assessment["versions"]["feature_set"] and row.assessment["versions"]["rules"]
