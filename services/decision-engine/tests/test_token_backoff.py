"""R9 (2026-10-15): a decision blocked only by token data that needs new
trades (no volatility yet, too little liquidity, no flow) is re-checked after
60 s instead of 30 s and counted; anything else keeps the 30 s pace."""

from types import SimpleNamespace

from yonixalpha_core import rpc_usage
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import FinalDecision, Finding, RiskCategory, RiskLevel
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.testing.pump import FakeRpc, seed_healthy_launch

from app.gate_eval import REEVALUATE_EVERY_SECONDS, TOKEN_BLOCKED_RECHECK_SECONDS, evaluate_with_gate, token_backoff_codes
from tests.test_sol_risk_settings import ENV, NOW, SOL, make_candidate


def _a(decision, *findings, executable=False):
    return SimpleNamespace(executable=executable, decision=decision, findings=list(findings))


def _f(code, action=FinalDecision.NO_TRADE, hard=True):
    return Finding(RiskCategory.DATA, code, RiskLevel.CRITICAL, code, action, hard_block=hard)


def test_only_token_data_blocks_qualify():
    nt = FinalDecision.NO_TRADE
    assert token_backoff_codes(_a(nt, _f("AUTO_SL_NO_VOLATILITY"))) == ["AUTO_SL_NO_VOLATILITY"]
    assert token_backoff_codes(_a(nt, _f("INSUFFICIENT_MIGRATED_LIQUIDITY"), _f("FLOW_UNAVAILABLE"),
                                  _f("BONDING_CURVE_MARKET", FinalDecision.EXECUTE, False))) == [
        "FLOW_UNAVAILABLE", "INSUFFICIENT_MIGRATED_LIQUIDITY"]
    # any other blocker (a safety finding, an account limit) keeps the normal pace
    assert token_backoff_codes(_a(nt, _f("AUTO_SL_NO_VOLATILITY"), _f("MINT_AUTHORITY_ACTIVE"))) == []
    assert token_backoff_codes(_a(FinalDecision.EXECUTE, executable=True)) == []
    assert token_backoff_codes(_a(nt)) == []


async def _pace_ttl(redis, cand) -> int:
    return await redis.ttl(f"yx:gate:pace:{cand.id}")


async def test_an_insufficient_liquidity_decision_is_rechecked_after_60_s(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "min_curve_liquidity_quote": "1000",
                                                      "wait_for_liquidity_max_age_seconds": 0}, None)
    await db_session.commit()
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a is not None and not a.executable
    assert token_backoff_codes(a) == ["INSUFFICIENT_LIQUIDITY"], [f.code for f in a.findings if f.hard_block]
    assert REEVALUATE_EVERY_SECONDS < await _pace_ttl(redis_client, cand) <= TOKEN_BLOCKED_RECHECK_SECONDS
    counts = await rpc_usage.token_backoff_today(redis_client, NOW)
    assert counts["solana_fresh|INSUFFICIENT_LIQUIDITY"] == 1 and counts["solana_fresh|*"] == 1
    # the paced candidate is not re-evaluated before then (no market event)
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW) is None


async def test_a_tradable_decision_keeps_the_30_s_pace(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", SOL, None)
    await db_session.commit()
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a is not None and token_backoff_codes(a) == []
    assert await _pace_ttl(redis_client, cand) <= REEVALUATE_EVERY_SECONDS
    assert await rpc_usage.token_backoff_today(redis_client, NOW) == {}
