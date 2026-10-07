"""LIVE_EXECUTION_SMOKE_TEST through the real gate evaluation: an armed run
buys only what the full gate approves against the live wallet, never more
than its max_sol, never in a category it was not armed for, and never
changes the global mode. Provider boundaries are not exercised here (the
BUY order is only queued); see paper-trading's live worker tests."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core import live_smoke, live_trading
from yonixalpha_core.db.models import ExecutionOrder, LiveSmokeTest, PaperPosition
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.testing.pump import FakeRpc, seed_healthy_launch

from app.gate_eval import evaluate_with_gate
from tests.test_gate_eval import NOW, make_candidate

SMOKE_ENV = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, TELEGRAM_BOT_TOKEN=None,
                            TELEGRAM_CHAT_ID=None, LIVE_SMOKE_TEST_ENABLED=True, LIVE_SMOKE_TEST_MAX_SOL=Decimal("0.02"),
                            LIVE_SMOKE_TEST_MAX_TRADES=1)


async def _wallet(db, redis, sol: str) -> None:
    await store.set_strategy_mode(db, "solana_fresh", StrategyMode.AUTO, None)
    acct = await live_trading.get_live_account(db)
    acct.cash_balance = Decimal(sol)
    await db.commit()
    await redis.set(live_trading.READY_KEY, json.dumps({"status": "ready", "min_sol_reserve": "0.05",
                                                        "wallet_max_age_seconds": "120"}))
    await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": sol, "at": datetime.now(timezone.utc).isoformat()}))


async def _arm(db, redis, category="FRESH", sol="0.02") -> LiveSmokeTest:
    run = await live_smoke.arm(db, redis, SMOKE_ENV, category, Decimal(sol), 30, "admin", datetime.now(timezone.utc))
    await db.commit()
    return run


async def test_armed_fresh_run_queues_one_capped_live_buy_and_keeps_global_paper(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await _wallet(db_session, redis_client, "0.3")
    run = await _arm(db_session, redis_client)
    cand = await make_candidate(db_session)
    await evaluate_with_gate(db_session, redis_client, SMOKE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)

    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    order = (await db_session.execute(select(ExecutionOrder))).scalar_one()
    assert pos.execution_mode == "LIVE" and pos.status == "pending_entry" and pos.execution_route == "pump"
    assert pos.plan["venue"]["smoke_test_run"] == str(run.id)
    assert order.side == "BUY" and order.status == "PENDING" and Decimal(order.amount) <= Decimal("0.02")
    await db_session.refresh(run)
    assert run.status == "USED" and run.position_id == pos.id and run.stage == "BUY_REQUESTED"
    assert await store.load_global_mode(db_session) == GlobalMode.PAPER  # never switched
    view = await live_smoke.run_view(db_session, run, NOW)
    assert view["stage"] == "BUY_REQUESTED" and view["buy"]["order_submitted"] is False
    # One buy allowed: the test cannot be armed again.
    try:
        await _arm(db_session, redis_client)
        raise AssertionError("armed past LIVE_SMOKE_TEST_MAX_TRADES")
    except live_smoke.SmokeRefused as exc:
        assert "MAX_TRADES" in exc.reason


async def test_other_category_leaves_normal_paper_trading_untouched(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await _wallet(db_session, redis_client, "0.3")
    run = await _arm(db_session, redis_client, category="MIGRATED")
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, SMOKE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.execution_target.value == "PAPER"
    assert (await db_session.execute(select(PaperPosition))).scalar_one().execution_mode == "PAPER"
    assert (await db_session.execute(select(ExecutionOrder))).first() is None
    await db_session.refresh(run)
    assert run.status == "ARMED" and run.attempts == []


async def test_unsafe_token_is_never_bought_and_the_stage_is_recorded(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await _wallet(db_session, redis_client, "0.3")
    run = await _arm(db_session, redis_client)
    cand = await make_candidate(db_session)
    await evaluate_with_gate(db_session, redis_client, SMOKE_ENV,
                             Sources(redis_client, FakeRpc(curve, mint_authority="X" * 32)), cand, NOW)
    assert (await db_session.execute(select(ExecutionOrder))).first() is None
    await db_session.refresh(run)
    assert run.status == "ARMED" and run.attempts[-1]["stage"] == "SAFETY_REJECTED"
    assert "MINT_AUTHORITY" in run.attempts[-1]["codes"]


async def test_small_wallet_is_risk_rejected_not_resized(db_session, redis_client):
    """0.055 SOL minus the 0.05 reserve leaves 0.005: too small to trade.
    The smoke test never lowers a limit to make a buy happen. Since the
    2026-10-07 audit the LIVE round trip's fixed costs are counted here too
    (the smoke assessment had reused the paper input without them): at
    0.005 SOL they alone exceed the stop, so the refusal names that."""
    curve = await seed_healthy_launch(redis_client, NOW)
    await _wallet(db_session, redis_client, "0.3")
    run = await _arm(db_session, redis_client)
    acct = await live_trading.get_live_account(db_session)
    acct.cash_balance = Decimal("0.055")
    await db_session.commit()
    cand = await make_candidate(db_session)
    await evaluate_with_gate(db_session, redis_client, SMOKE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert (await db_session.execute(select(ExecutionOrder))).first() is None
    await db_session.refresh(run)
    assert run.attempts[-1]["stage"] == "RISK_REJECTED" and "STOP_INSIDE_COSTS" in run.attempts[-1]["codes"]


async def test_expired_run_reports_no_test_execution_candidate(db_session, redis_client):
    await _wallet(db_session, redis_client, "0.3")
    run = await _arm(db_session, redis_client)
    run.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    run.attempts = [{"stage": "SAFETY_REJECTED", "at": "x"}]
    await db_session.commit()
    assert await live_smoke.armed_run(db_session, datetime.now(timezone.utc)) is None
    await db_session.refresh(run)
    assert run.status == "EXPIRED" and run.stage == "NO_TEST_EXECUTION_CANDIDATE" and "SAFETY_REJECTED 1" in run.stage_reason
