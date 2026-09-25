from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


# --- LIVE path (provider boundaries mocked: no transaction is built here) ----

LIVE_ENV = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, TELEGRAM_BOT_TOKEN=None,
                           TELEGRAM_CHAT_ID=None)


async def _live_mode(db, redis, ready: bool, sol: str = "5"):
    import json

    from yonixalpha_core import live_trading

    await store.set_global_mode(db, GlobalMode.LIVE, None)
    await store.set_strategy_mode(db, "solana_fresh", StrategyMode.AUTO, None)
    acct = await live_trading.get_live_account(db)
    acct.cash_balance = Decimal(sol)
    await db.commit()
    if ready:
        await redis.set(live_trading.READY_KEY, json.dumps({"status": "ready", "min_sol_reserve": "0.05",
                                                            "wallet_max_age_seconds": "120"}))
        await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": sol, "at": datetime.now(timezone.utc).isoformat()}))


async def test_live_auto_creates_a_pending_buy_order_not_a_position(db_session, redis_client):
    from yonixalpha_core.db.models import ExecutionOrder

    curve = await seed_healthy_launch(redis_client, NOW)
    await _live_mode(db_session, redis_client, ready=True)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, LIVE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE" and a.execution_target.value == "LIVE", a.reasons
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    order = (await db_session.execute(select(ExecutionOrder))).scalar_one()
    assert pos.execution_mode == "LIVE" and pos.status == "pending_entry" and pos.quantity == 0
    assert (pos.source, pos.lifecycle, pos.execution_provider, pos.execution_route) == ("PUMPFUN", "FRESH", "pumpportal_local", "pump")
    assert order.side == "BUY" and order.status == "PENDING" and order.signature is None
    assert Decimal(order.amount) == a.plan.position_size.value and order.idempotency_key == f"entry:{pos.assessment_id}"
    assert order.limits["max_sol_in_lamports"] > 0 and order.limits["max_priority_fee_lamports"] > 0
    assert cand.state == CandidateState.ENTRY_PENDING.value
    # No simulated paper fill is booked for a live decision.
    assert (await db_session.execute(select(MLFeatureSnapshot))).scalar_one().label is None


async def test_live_without_a_ready_worker_is_no_trade(db_session, redis_client):
    from yonixalpha_core.db.models import ExecutionOrder

    curve = await seed_healthy_launch(redis_client, NOW)
    await _live_mode(db_session, redis_client, ready=False)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, LIVE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "NO_TRADE" and "LIVE_NOT_READY" in {f.code for f in a.findings}
    assert (await db_session.execute(select(ExecutionOrder))).scalars().all() == []
    assert (await db_session.execute(select(PaperPosition))).scalars().all() == []


async def test_live_mode_with_locks_closed_never_goes_live(db_session, redis_client):
    from yonixalpha_core.db.models import ExecutionOrder

    curve = await seed_healthy_launch(redis_client, NOW)
    await _live_mode(db_session, redis_client, ready=True)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.execution_target.value != "LIVE"
    assert (await db_session.execute(select(ExecutionOrder))).scalars().all() == []


async def test_operator_exit_plan_is_validated_and_used(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await store.save_strategy_config(db_session, "solana_fresh", {
        "manual_stop_loss_pct": "0.15", "manual_tp1_pct": "0.3", "manual_tp2_pct": "0.6", "manual_trailing_pct": "0.08"}, None)
    await db_session.commit()
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    p = a.plan
    assert p.stop_loss.provenance.value == "MANUAL" and p.stop_loss.value == p.entry_price * Decimal("0.85")
    assert [tp.price.value for tp in p.take_profits] == [p.entry_price * Decimal("1.3"), p.entry_price * Decimal("1.6")]
    assert p.trailing.provenance.value == "MANUAL" and p.trailing.distance_pct == Decimal("0.08")
    assert p.position_size.provenance.value == "AUTO"  # not set -> calculated


async def test_operator_stop_outside_risk_limits_is_refused(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await store.save_strategy_config(db_session, "solana_fresh", {"manual_stop_loss_pct": "0.6"}, None)  # max_stop_pct 0.30
    await db_session.commit()
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert not a.executable and "MANUAL_SL_TOO_WIDE" in {f.code for f in a.findings}
    assert (await db_session.execute(select(PaperPosition))).scalars().all() == []
