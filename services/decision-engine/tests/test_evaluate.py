from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.evaluate import MAX_OBSERVATION_SECONDS, evaluate_candidate
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import RiskEvent, StrategySignal, Token, TradingCandidate
from yonixalpha_core.decision import DecisionType
from yonixalpha_core.state_machine import CandidateState

MINT = "TestMint111111111111111111111111111111111"


def _settings(**overrides) -> Settings:
    defaults = dict(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="unused-in-tests", TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True)
    defaults.update(overrides)
    return Settings(**defaults)


async def _make_candidate(db_session, state=CandidateState.DISCOVERED, history=None) -> TradingCandidate:
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = TradingCandidate(
        token_id=token.id,
        engine="momentum",
        state=state.value,
        state_history=history if history is not None else [{"state": state.value, "at": datetime.now(timezone.utc).isoformat(), "reason": "test"}],
    )
    db_session.add(candidate)
    await db_session.commit()
    return candidate


async def test_first_evaluation_transitions_discovered_to_observing(db_session, redis_client):
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert candidate.state == CandidateState.OBSERVING.value
    assert any(h["state"] == CandidateState.OBSERVING.value for h in candidate.state_history)


async def test_no_events_yields_wait_or_no_trade_never_long(db_session, redis_client):
    """With no TokenEvents and no price/liquidity feed, DEGRADED confidence
    is capped well below the entry threshold — LONG must be unreachable.
    """
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    decision = await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert decision.decision != DecisionType.LONG


async def test_kill_switch_engaged_forces_no_trade(db_session, redis_client):
    from yonixalpha_core import kill_switch

    await kill_switch.engage(redis_client, "manual test trip")
    candidate = await _make_candidate(db_session, state=CandidateState.OBSERVING)
    now = datetime.now(timezone.utc)

    decision = await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert decision.decision == DecisionType.NO_TRADE
    assert any("kill switch" in r for r in decision.reason)


async def test_trading_disabled_forces_no_trade(db_session, redis_client):
    candidate = await _make_candidate(db_session, state=CandidateState.OBSERVING)
    now = datetime.now(timezone.utc)

    decision = await evaluate_candidate(db_session, redis_client, _settings(TRADING_ENABLED=False), candidate, now)

    assert decision.decision == DecisionType.NO_TRADE
    assert any("TRADING_ENABLED" in r for r in decision.reason)


async def test_evaluation_persists_strategy_signal_and_risk_event(db_session, redis_client):
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    signals = (await db_session.execute(select(StrategySignal).where(StrategySignal.candidate_id == candidate.id))).scalars().all()
    events = (await db_session.execute(select(RiskEvent).where(RiskEvent.candidate_id == candidate.id))).scalars().all()
    assert len(signals) == 1
    assert len(events) == 1
    assert signals[0].symbol == MINT
    assert events[0].approved is True


async def test_observation_window_elapsed_without_qualifying_rejects(db_session, redis_client):
    started_at = datetime.now(timezone.utc) - timedelta(seconds=MAX_OBSERVATION_SECONDS + 60)
    history = [
        {"state": CandidateState.DISCOVERED.value, "at": started_at.isoformat(), "reason": "test"},
        {"state": CandidateState.OBSERVING.value, "at": started_at.isoformat(), "reason": "test"},
    ]
    candidate = await _make_candidate(db_session, state=CandidateState.OBSERVING, history=history)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert candidate.state == CandidateState.REJECTED.value


async def test_still_within_observation_window_stays_observing(db_session, redis_client):
    started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    history = [
        {"state": CandidateState.DISCOVERED.value, "at": started_at.isoformat(), "reason": "test"},
        {"state": CandidateState.OBSERVING.value, "at": started_at.isoformat(), "reason": "test"},
    ]
    candidate = await _make_candidate(db_session, state=CandidateState.OBSERVING, history=history)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert candidate.state == CandidateState.OBSERVING.value
