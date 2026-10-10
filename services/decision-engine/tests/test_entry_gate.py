"""Early-entry changes in the safety gate (2026-10-10): a candidate created
by an entry strategy in PAPER mode never becomes a LIVE order, a meaningful
market event re-evaluates a paced candidate early (never without one), and
an opened entry records the CURRENT_GATE_ENTRY baseline. No network."""

import json
import time
from datetime import datetime, timezone

from sqlalchemy import select

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_store, gate_events
from yonixalpha_core.db.models import EntrySignal, ExecutionOrder, PaperPosition, RiskAssessment
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.testing.pump import MINT, FakeRpc, seed_healthy_launch

from app import gate_eval
from app.gate_eval import evaluate_with_gate
from tests.test_gate_eval import ENV, LIVE_ENV, NOW, _live_mode, make_candidate


async def test_paper_only_candidate_never_goes_live_in_live_mode(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await _live_mode(db_session, redis_client, ready=True)
    cand = await make_candidate(db_session)
    cand.detail = {**cand.detail, "paper_only": True, "entry_strategy": ei.EARLY_ACCELERATION}
    await db_session.commit()
    a = await evaluate_with_gate(db_session, redis_client, LIVE_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.execution_target.value != "LIVE", a.reasons
    assert (await db_session.execute(select(ExecutionOrder))).scalars().all() == []
    pos = (await db_session.execute(select(PaperPosition))).scalars().all()
    assert all(p.execution_mode != "LIVE" for p in pos)
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert row.assessment["inputs_snapshot"]["features"] is not None


async def test_opened_entry_records_the_current_gate_entry_baseline_once(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    base = (await db_session.execute(select(EntrySignal).where(EntrySignal.strategy == ei.CURRENT_GATE_ENTRY))).scalar_one()
    assert base.mint == MINT and base.decision == "BASELINE" and base.price_raw and base.price_raw > 0
    assert base.features["trigger"] == "timer" and base.features["mode"] == "PAPER"


async def test_paced_candidate_is_re_evaluated_only_on_a_meaningful_event(db_session, redis_client):
    gate_eval._EE_CACHE.update(at=0.0, cfg=None)
    await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    # Missing on-chain data: NO_TRADE, the candidate stays under analysis.
    src = Sources(redis_client, FakeRpc(None, fail={"getAccountInfo", "getTokenLargestAccounts"}))
    state = {"phase": ei.EARLY_ACCEL_PHASE, "evaluated_at": NOW.isoformat(),
             "features": {"trades_total": 10, "net_inflow_sol_total": 1.0, "unique_sellers": 1, "sol_accumulated": 2.0,
                          "w10": {"max_buy_sol": 0.1}}}
    await entry_store.write_state(redis_client, MINT, NOW, state)
    first = await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW)
    assert first.decision.value == "NO_TRADE"
    assert await redis_client.get(gate_events.FINGERPRINT_KEY + str(cand.id))

    # Inside the 30 s pacing and no event: skipped.
    assert await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW) is None

    # The last evaluation is old enough, but nothing changed: still skipped.
    fp = json.loads(await redis_client.get(gate_events.FINGERPRINT_KEY + str(cand.id)))
    fp["_evaluated_ts"] = time.time() - 15
    await redis_client.set(gate_events.FINGERPRINT_KEY + str(cand.id), json.dumps(fp))
    assert await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW) is None

    # A significant buy and +1.5 SOL inflow: evaluated now, with its own key.
    state["features"].update(trades_total=14, net_inflow_sol_total=2.5, w10={"max_buy_sol": 0.8})
    await entry_store.write_state(redis_client, MINT, NOW, state)
    b = await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW)
    assert b is not None
    rows = (await db_session.execute(select(RiskAssessment).order_by(RiskAssessment.created_at))).scalars().all()
    assert len(rows) == 2 and rows[1].idempotency_key.endswith(f"e{int(NOW.timestamp())}")


async def test_too_recent_evaluation_waits_even_with_an_event(db_session, redis_client):
    gate_eval._EE_CACHE.update(at=0.0, cfg=None)
    await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    src = Sources(redis_client, FakeRpc(None, fail={"getAccountInfo", "getTokenLargestAccounts"}))
    state = {"phase": ei.EARLY_ACCEL_PHASE, "features": {"trades_total": 10, "net_inflow_sol_total": 1.0}}
    await entry_store.write_state(redis_client, MINT, NOW, state)
    await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW)
    state["features"].update(trades_total=30, net_inflow_sol_total=9.0)
    await entry_store.write_state(redis_client, MINT, NOW, state)
    # event_min_interval_seconds (10) has not passed since the evaluation
    assert await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW) is None


async def test_wake_list_returns_pushed_candidates_in_order(redis_client):
    import asyncio

    await gate_events.wake(redis_client, "a")
    await gate_events.wake(redis_client, "b")
    assert await gate_events.wait_for_wake(redis_client, asyncio.Event(), 2) == ["a", "b"]
    stop = asyncio.Event()
    started = datetime.now(timezone.utc)
    assert await gate_events.wait_for_wake(redis_client, stop, 1) == []
    assert (datetime.now(timezone.utc) - started).total_seconds() < 3


async def test_curve_paper_buy_is_scheduled_to_land_after_the_measured_latency(db_session, redis_client):
    """Regression audit 2026-10-10: a pump-curve paper buy is filled at the
    stream price measured LIVE latency after the decision (settled by the
    paper-trading service), and the median LIVE drift is not charged on top."""
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    d = pos.plan["entry_delay"]
    assert d["settled"] is False and d["latency_s"] == 3.0 and d["due_ts"] == round(NOW.timestamp() + 3.0, 3)
    assert "live_drift_pct" not in pos.plan["venue"]


async def test_switching_entry_delay_off_keeps_the_immediate_fill(db_session, redis_client):
    from yonixalpha_core.db.models import PlatformSetting

    db_session.add(PlatformSetting(key="paper_execution", value={"simulate_entry_delay": False}))
    await db_session.commit()
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    assert "entry_delay" not in (pos.plan or {})
