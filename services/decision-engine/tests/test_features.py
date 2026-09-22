import uuid
from datetime import datetime, timedelta, timezone

from app.features import compute_candidate_features
from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.risk import DataQuality
from yonixalpha_core.state_machine import CandidateState

MINT = "TestMint111111111111111111111111111111111"


async def _make_candidate(db_session, token: Token) -> TradingCandidate:
    candidate = TradingCandidate(
        token_id=token.id,
        engine="momentum",
        state=CandidateState.DISCOVERED.value,
        state_history=[{"state": CandidateState.DISCOVERED.value, "at": datetime.now(timezone.utc).isoformat(), "reason": "test"}],
    )
    db_session.add(candidate)
    await db_session.flush()
    return candidate


class _NoTokenSession:
    """A minimal stand-in exposing only the one method
    compute_candidate_features calls before it would otherwise need a real
    row: session.get(). This isn't a business-logic mock — trading_candidates
    has a NOT NULL, ON DELETE CASCADE foreign key to tokens, so a real
    Postgres session can never actually produce a candidate whose token row
    is missing (deleting the token cascades and deletes the candidate too);
    the only way to exercise this defensive branch is a fake session.get().
    """

    async def get(self, model, id):  # noqa: A002 - matches AsyncSession.get's signature
        return None


async def test_missing_token_is_unavailable():
    candidate = TradingCandidate(
        token_id=uuid.uuid4(),
        engine="momentum",
        state=CandidateState.DISCOVERED.value,
        state_history=[],
    )

    features = await compute_candidate_features(_NoTokenSession(), candidate, datetime.now(timezone.utc))
    assert features.data_quality == DataQuality.UNAVAILABLE
    assert features.tx_acceleration_ratio is None


async def test_no_events_yields_zero_counts_and_degraded_quality(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = await _make_candidate(db_session, token)

    features = await compute_candidate_features(db_session, candidate, datetime.now(timezone.utc))
    assert features.data_quality == DataQuality.DEGRADED
    assert features.tx_count_current_window == 0
    assert features.tx_count_prior_window == 0
    assert features.tx_acceleration_ratio is None


async def test_computes_acceleration_ratio_from_transfer_events(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = await _make_candidate(db_session, token)

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=300)
    for i in range(2):
        db_session.add(
            TokenEvent(
                token_id=token.id,
                event_type="transfer",
                source="seed",
                occurred_at=now - window - timedelta(seconds=i + 1),
                signature=f"prior-{i}",
                payload={},
            )
        )
    for i in range(6):
        db_session.add(
            TokenEvent(
                token_id=token.id,
                event_type="transfer",
                source="seed",
                occurred_at=now - timedelta(seconds=i + 1),
                signature=f"current-{i}",
                payload={},
            )
        )
    await db_session.commit()

    features = await compute_candidate_features(db_session, candidate, now)
    assert features.tx_count_current_window == 6
    assert features.tx_count_prior_window == 2
    assert features.tx_acceleration_ratio == 3.0
    assert features.data_quality == DataQuality.DEGRADED


async def test_non_transfer_events_are_excluded(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = await _make_candidate(db_session, token)

    now = datetime.now(timezone.utc)
    db_session.add(TokenEvent(token_id=token.id, event_type="trade", source="seed", occurred_at=now, signature="trade-1", payload={}))
    await db_session.commit()

    features = await compute_candidate_features(db_session, candidate, now)
    assert features.tx_count_current_window == 0


async def test_token_age_is_computed_from_first_seen_at(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    candidate = await _make_candidate(db_session, token)

    now = token.first_seen_at.replace(tzinfo=timezone.utc) + timedelta(seconds=120)
    features = await compute_candidate_features(db_session, candidate, now)
    assert 119 <= features.token_age_seconds <= 121
