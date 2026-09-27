"""Manual BUY runs the normal gate with the operator's decision in place of
the strategy signal: it executes through the normal paper/live path when
every safety check passes, and is BLOCKED with the exact reasons when one
does not. It never bypasses a safety check and never touches automatic
candidates of other tokens."""

import json
import uuid

from sqlalchemy import select

from yonixalpha_core import manual_trade
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, seed_healthy_launch

from app.gate_eval import evaluate_with_gate
from tests.test_gate_eval import ENV, NOW


async def _request(db, redis, engine=None):
    req = await manual_trade.create_request(db, redis, MINT, engine, "fresh", "admin")
    cand = await db.get(TradingCandidate, uuid.UUID(req["candidate_id"]))
    return req, cand


async def test_request_creates_a_manual_only_candidate_and_queues_it(db_session, redis_client):
    await seed_healthy_launch(redis_client, NOW)
    req, cand = await _request(db_session, redis_client, "solana_fresh")
    assert req["route"] == "Pump.fun bonding curve" and req["engine"] == "solana_fresh" and not req["migrated"]
    assert cand.detail["manual_only"] and cand.engine == "discovery"
    assert await manual_trade.next_request(redis_client, timeout=1) == req["id"]


async def test_manual_buy_executes_in_paper_even_when_the_strategy_is_off(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    await store.set_strategy_mode(db_session, "solana_fresh", StrategyMode.OFF, None)
    await db_session.commit()
    req, cand = await _request(db_session, redis_client)
    op = {"id": req["id"]}
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW, op)
    assert a is not None and a.executable, a.reasons
    assert op["result"]["status"] == "PAPER_POSITION_OPEN" and op["result"]["target"] == "PAPER"
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    assert str(pos.id) == op["result"]["position_id"] and pos.execution_route == "pump"
    row = (await db_session.execute(select(RiskAssessment))).scalar_one()
    assert "OPERATOR_BUY_REQUEST" in {f["code"] for f in row.assessment["findings"]}


async def test_manual_buy_is_blocked_by_token_risk_with_the_reason(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    req, cand = await _request(db_session, redis_client)
    op = {"id": req["id"]}
    await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve, mint_authority="X" * 32)), cand, NOW, op)
    r = op["result"]
    assert r["status"] == "BLOCKED" and r["decision"] == "REJECT"
    assert any(b["category"] == "TOKEN" for b in r["blockers"]), r
    assert (await db_session.execute(select(PaperPosition))).first() is None
    assert cand.state == CandidateState.REJECTED.value


async def test_manual_buy_is_blocked_without_data(db_session, redis_client):
    await seed_healthy_launch(redis_client, NOW)
    req, cand = await _request(db_session, redis_client)
    op = {"id": req["id"]}
    src = Sources(redis_client, FakeRpc(None, fail={"getAccountInfo", "getTokenLargestAccounts"}))
    await evaluate_with_gate(db_session, redis_client, ENV, src, cand, NOW, op)
    assert op["result"]["status"] == "BLOCKED" and op["result"]["decision"] == "NO_TRADE"
    assert any("rpc" in e for e in op["result"]["data_errors"]), op["result"]  # the cause, not only "unavailable"
    assert (await db_session.execute(select(PaperPosition))).first() is None


async def test_migrated_token_uses_the_pool_route(db_session, redis_client):
    await seed_healthy_launch(redis_client, NOW)
    await redis_client.hset(f"yx:pump:curve:{MINT}", "pool", "Pool1111111111111111111111111111111111111")
    req, cand = await _request(db_session, redis_client, "solana_fresh")
    assert req["engine"] == "solana_migration" and req["migrated"] and cand.engine == "migration"
    assert "PumpSwap" in req["route"]


async def test_status_updates_are_recorded(redis_client, db_session):
    await seed_healthy_launch(redis_client, NOW)
    req, _ = await _request(db_session, redis_client)
    await manual_trade.update(redis_client, req["id"], "EVALUATING")
    r = await manual_trade.update(redis_client, req["id"], "BLOCKED", reason="x")
    assert [h["status"] for h in r["history"]] == ["QUEUED", "EVALUATING", "BLOCKED"]
    assert json.loads(await redis_client.get(manual_trade.PREFIX + req["id"]))["status"] == "BLOCKED"
