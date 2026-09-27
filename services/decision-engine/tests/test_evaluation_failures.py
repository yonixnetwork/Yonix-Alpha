"""A candidate whose evaluation raises is never lost silently: the exact
error and place are recorded (timeline + system event/Telegram), no position
is opened, and a candidate that keeps failing is rejected with that reason
instead of holding a budget slot forever."""

import decimal
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core.db.base import Base, make_session_factory

from yonixalpha_core.db.models import PaperPosition, RiskAssessment, TradeTimelineEvent
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import FakeRpc, migrated_curve_account, seed_healthy_launch

from app import diagnostics
from app.gate_eval import evaluate_with_gate
from tests.test_gate_eval import ENV, NOW, make_candidate


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_migrated_curve_candidate_is_assessed_not_crashed(db_session, redis_client):
    """The production candidate a8fd3422…: its curve had migrated on chain
    before the stream recorded the pool."""
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV,
                                 Sources(redis_client, FakeRpc(curve, curve_account=migrated_curve_account())), cand, NOW)
    assert a is not None and not a.executable and "PRICE_UNAVAILABLE" in {f.code for f in a.findings}
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert row.assessment["inputs_snapshot"]["curve_state"] == "MIGRATED_ON_CHAIN"
    assert (await db_session.execute(select(PaperPosition))).first() is None
    assert cand.state == CandidateState.ANALYZING.value


def _crash(secret_api_key="must-not-leak", bot_token="must-not-leak-either"):
    virtual_quote_reserves, virtual_token_reserves = Decimal(0), Decimal(0)
    return virtual_quote_reserves / virtual_token_reserves


async def test_failure_is_recorded_with_place_and_inputs_then_rejected(session_factory, redis_client):
    async with session_factory() as s:
        cand = await make_candidate(s)
    try:
        _crash()
    except decimal.InvalidOperation as exc:
        err = exc
    now = datetime.now(timezone.utc)
    for i in range(diagnostics.MAX_EVALUATION_FAILURES):
        detail = await diagnostics.record_failure(session_factory, redis_client, cand.id, err, now)
    assert detail["error_type"] == "decimal.DivisionUndefined (0 / 0)"
    w = detail["where"]
    assert w["function"] == "_crash" and "virtual_quote_reserves / virtual_token_reserves" in w["code"]
    assert w["inputs"]["virtual_token_reserves"] == "0"
    assert "secret_api_key" not in w["inputs"] and "bot_token" not in w["inputs"]
    assert detail["mint"] and detail["engine"] == "discovery" and detail["attempt"] == diagnostics.MAX_EVALUATION_FAILURES
    assert detail["decision"].startswith("REJECTED")
    async with session_factory() as s:
        c = await s.get(type(cand), cand.id)
        events = (await s.execute(select(TradeTimelineEvent).where(TradeTimelineEvent.candidate_id == cand.id))).scalars().all()
    assert c.state == CandidateState.REJECTED.value and "EVALUATION_FAILED x5: decimal.DivisionUndefined" in c.state_history[-1]["reason"]
    assert len(events) == diagnostics.MAX_EVALUATION_FAILURES
    assert "must-not-leak" not in str([e.detail for e in events])


async def test_a_success_resets_the_failure_count(session_factory, redis_client):
    async with session_factory() as s:
        cand = await make_candidate(s)
    detail = await diagnostics.record_failure(session_factory, redis_client, cand.id, RuntimeError("x"), NOW)
    assert detail["attempt"] == 1 and detail["decision"].startswith("retried")
    await diagnostics.clear_failures(redis_client, cand.id)
    detail = await diagnostics.record_failure(session_factory, redis_client, cand.id, RuntimeError("x"), NOW)
    assert detail["attempt"] == 1
