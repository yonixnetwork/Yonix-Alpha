from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core.db.models import MLFeatureSnapshot, PaperPosition, RiskAssessment, Token, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, seed_fading_flow, seed_healthy_launch

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
    # paper is charged the LIVE buy transaction's fee (paper_execution.charge_live_fixed_costs, default on)
    buy_fee = Decimal(a.plan.fixed_cost_detail["buy_network_fee"])
    assert a.plan.fixed_cost_quote and pos.entry_cost_quote == a.plan.position_size.value + buy_fee
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
    # The transition stamps the wall clock, not NOW: measure 31 minutes from
    # the recorded analysis start so a slow test run can't shorten the wait.
    started = datetime.fromisoformat(next(e["at"] for e in cand.state_history if e["state"] == CandidateState.ANALYZING.value))
    await evaluate_with_gate(db_session, redis_client, ENV, src, cand, started + timedelta(minutes=31))
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


async def test_live_buy_is_committed_before_the_entry_notification(db_session, redis_client, monkeypatch):
    """The order worker only sees committed orders: the entry notification
    (a Telegram HTTP call) must not hold the BUY back."""
    from yonixalpha_core.db.models import ExecutionOrder, Notification
    from yonixalpha_core.safety import pipeline

    seen = {}
    original = pipeline.after_entry

    async def spy(session, *args):
        seen["in_transaction"] = session.in_transaction()
        await original(session, *args)

    monkeypatch.setattr(pipeline, "after_entry", spy)
    curve = await seed_healthy_launch(redis_client, NOW)
    await _live_mode(db_session, redis_client, ready=True)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, LIVE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.execution_target.value == "LIVE" and seen == {"in_transaction": False}
    assert (await db_session.execute(select(ExecutionOrder))).scalar_one().status == "PENDING"
    titles = (await db_session.execute(select(Notification.title))).scalars().all()
    assert any(t.startswith("LIVE BUY submitted") for t in titles)


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


async def test_simulated_paper_entry_failure_opens_nothing(db_session, redis_client, monkeypatch):
    from yonixalpha_core import paper_execution
    from yonixalpha_core.db.models import PlatformSetting, TradeTimelineEvent

    curve = await seed_healthy_launch(redis_client, NOW)
    db_session.add(PlatformSetting(key=paper_execution.SETTINGS_KEY, value={"entry_failure_pct": "25"}))
    await db_session.commit()
    monkeypatch.setattr(paper_execution, "draw", lambda key: Decimal(0))
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE"  # the decision stands; the (simulated) execution failed
    assert (await db_session.execute(select(PaperPosition))).scalars().all() == []
    assert cand.state == CandidateState.REJECTED.value
    ev = (await db_session.execute(select(TradeTimelineEvent).where(
        TradeTimelineEvent.event_type == "paper_entry_failed"))).scalar_one()
    assert ev.detail["simulated"] is True and ev.detail["source"] == "operator setting"


async def test_entry_records_the_holder_snapshot_for_exit_monitoring(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    venue = (await db_session.execute(select(PaperPosition))).scalar_one().plan["venue"]
    assert set(venue["holders_at_entry"]) == {"top1_share", "top10_share", "creator_share"}
    assert int(venue["supply_raw"]) > 0 and len(venue["holder_excluded"]) == 2


async def test_exit_signal_at_entry_waits_and_opens_nothing(db_session, redis_client):
    """Regression (3eSai…pump): the flow exit intelligence would REDUCE on
    its first tick blocks the entry itself; the token keeps being
    re-evaluated instead of being bought and sold 15 s later."""
    curve = await seed_healthy_launch(redis_client, NOW)
    at = await seed_fading_flow(redis_client, curve, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, at)
    assert not a.executable
    assert any(f.code == "EXIT_SIGNAL_AT_ENTRY" and f.action.value == "WAIT" for f in a.findings)
    assert cand.state == CandidateState.ANALYZING.value
    assert (await db_session.execute(select(PaperPosition))).first() is None
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert "EXIT_SIGNAL_AT_ENTRY" in {f["code"] for f in row.assessment["findings"]}
    assert row.assessment["inputs_snapshot"]["entry_exit_check"]["action"] == "REDUCE"


async def test_invalid_saved_risk_settings_block_the_entry(db_session, redis_client):
    """A saved settings row that no longer validates must not be replaced by
    code defaults for a new entry: the operator's values (e.g. an intel
    action set to NO_TRADE) would silently stop applying."""
    from yonixalpha_core.db.models import RiskSettingsVersion

    db_session.add(RiskSettingsVersion(scope="GLOBAL", version=1, settings={"min_stop_pct": "0.5", "max_stop_pct": "0.1"}))
    await db_session.commit()
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "NO_TRADE"
    assert any(f.code == "TRADING_CONTROL_OFF" and "failed validation" in f.message for f in a.findings), a.reasons
    assert (await db_session.execute(select(PaperPosition))).first() is None


async def test_x_narrative_on_only_queues_the_candidate_and_changes_no_decision(db_session, redis_client):
    """X narrative (SHADOW): with the switch on, the gate decides exactly as without it, opens the same paper
    position, and only queues the mint for a later background lookup (nothing waits for X)."""
    import json as _json

    from yonixalpha_core import x_narrative

    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    env = SimpleNamespace(**vars(ENV), X_NARRATIVE_ENABLED=True, X_API_BEARER_TOKEN=None)
    a = await evaluate_with_gate(db_session, redis_client, env, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons  # same decision as test_healthy_launch_opens_paper_position_from_gate_plan
    assert (await db_session.execute(select(PaperPosition))).scalar_one() is not None
    queued = await redis_client.zrange(x_narrative.QUEUE, 0, -1)
    assert queued == [MINT]
    meta = _json.loads(await redis_client.get(x_narrative.QUEUE_META.format(mint=MINT)))
    assert meta["qualified"] is True and meta["executable"] is True and meta["engine"] == "solana_fresh"
