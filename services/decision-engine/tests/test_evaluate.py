from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.evaluate import MAX_OBSERVATION_SECONDS, MODEL_NAME, evaluate_candidate
from app.ml_features import FEATURE_NAMES
from app.signal import CONFIDENCE_CAP_DEGRADED_DATA
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import MLFeatureSnapshot, RiskEvent, StrategySignal, Token, TradingCandidate
from yonixalpha_core.decision import DecisionType
from yonixalpha_core.ml import registry
from yonixalpha_core.state_machine import CandidateState

MINT = "TestMint111111111111111111111111111111111"


class _FixedProbaEstimator:
    """A picklable stand-in for a fitted sklearn classifier: joblib needs a
    module-level class to serialize/deserialize (not a closure), and a
    fixed probability makes the blending math in these tests deterministic
    rather than depending on what a real fit happens to produce.
    """

    def __init__(self, positive_proba: float):
        self.positive_proba = positive_proba

    def predict_proba(self, vector):
        return [[1 - self.positive_proba, self.positive_proba]]


async def _activate_fixed_model(db_session, positive_proba: float):
    model_version = await registry.register_trained_model(
        db_session,
        name=MODEL_NAME,
        estimator=_FixedProbaEstimator(positive_proba),
        feature_names=FEATURE_NAMES,
        training_sample_count=10,
        metrics={"holdout_auc": 0.9},
    )
    await registry.activate_model(db_session, model_version)
    await db_session.commit()
    return model_version


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


async def test_no_active_model_notes_rule_based_only(db_session, redis_client):
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    decision = await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert any("no active trained ML model" in r for r in decision.reason)


async def test_active_model_score_is_blended_but_never_escapes_degraded_cap(db_session, redis_client):
    """Even a maximally confident ML model must not be able to push a
    DEGRADED-data candidate's confidence above the safety cap — the cap is
    reapplied after blending specifically to guarantee this.
    """
    await _activate_fixed_model(db_session, positive_proba=0.99)
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    decision = await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    assert decision.confidence <= CONFIDENCE_CAP_DEGRADED_DATA
    assert any("blended with ML model" in r for r in decision.reason)
    assert any("capped" in r for r in decision.reason)


async def test_ml_feature_snapshot_persisted_with_model_link_when_active(db_session, redis_client):
    model_version = await _activate_fixed_model(db_session, positive_proba=0.5)
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    rows = (await db_session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == candidate.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].model_version_id == model_version.id
    assert rows[0].ml_score is not None
    assert rows[0].label is None
    assert set(rows[0].features.keys()) == set(FEATURE_NAMES)


async def test_ml_feature_snapshot_persisted_without_model_link_by_default(db_session, redis_client):
    candidate = await _make_candidate(db_session, state=CandidateState.DISCOVERED)
    now = datetime.now(timezone.utc)

    await evaluate_candidate(db_session, redis_client, _settings(), candidate, now)

    rows = (await db_session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == candidate.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].model_version_id is None
    assert rows[0].ml_score is None
