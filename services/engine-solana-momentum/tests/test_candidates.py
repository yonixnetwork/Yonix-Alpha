from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.candidates import record_transfer_and_evaluate
from app.momentum import WINDOW_SECONDS
from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate

MINT = "TestMint111111111111111111111111111111111"


def _transfer_info(signature: str) -> dict:
    return {
        "mint": MINT,
        "amount": "1000000",
        "decimals": 6,
        "source": "SourceAcct111111111111111111111111111111",
        "destination": "DestAcct1111111111111111111111111111111",
        "authority": "Authority1111111111111111111111111111111",
        "signature": signature,
    }


async def test_unknown_mint_is_ignored(db_session):
    """Engine C only tracks tokens already present in the `tokens` table —
    it never creates one, that's Engine A's job.
    """
    result = await record_transfer_and_evaluate(db_session, _transfer_info("sig-1"), datetime.now(timezone.utc))
    assert result is None

    events = await db_session.execute(select(TokenEvent))
    assert events.scalars().all() == []


async def test_single_transfer_below_threshold_records_event_but_no_candidate(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.commit()

    result = await record_transfer_and_evaluate(db_session, _transfer_info("sig-1"), datetime.now(timezone.utc))
    assert result is None

    events = await db_session.execute(select(TokenEvent).where(TokenEvent.token_id == token.id))
    assert len(events.scalars().all()) == 1


async def test_acceleration_crossing_threshold_creates_candidate(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=WINDOW_SECONDS)
    # Seed 2 prior-window events directly.
    for i in range(2):
        db_session.add(
            TokenEvent(
                token_id=token.id,
                event_type="transfer",
                source="seed",
                occurred_at=now - window - timedelta(seconds=i + 1),
                signature=f"seed-{i}",
                payload={},
            )
        )
    await db_session.commit()

    candidate = None
    for i in range(6):
        candidate = await record_transfer_and_evaluate(db_session, _transfer_info(f"sig-{i}"), now)

    assert candidate is not None
    assert candidate.engine == "momentum"
    assert candidate.state == "discovered"
    assert candidate.detail["current_window_count"] == 6
    assert candidate.detail["prior_window_count"] == 2


async def test_duplicate_transfer_signature_is_idempotent(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    await record_transfer_and_evaluate(db_session, _transfer_info("dup-sig"), now)
    await record_transfer_and_evaluate(db_session, _transfer_info("dup-sig"), now)

    events = await db_session.execute(select(TokenEvent).where(TokenEvent.token_id == token.id))
    assert len(events.scalars().all()) == 1


async def test_does_not_create_second_candidate_while_one_is_active(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=WINDOW_SECONDS)
    for i in range(2):
        db_session.add(
            TokenEvent(
                token_id=token.id,
                event_type="transfer",
                source="seed",
                occurred_at=now - window - timedelta(seconds=i + 1),
                signature=f"seed-{i}",
                payload={},
            )
        )
    await db_session.commit()

    for i in range(6):
        await record_transfer_and_evaluate(db_session, _transfer_info(f"first-batch-{i}"), now)

    # More accelerating transfers arrive while the first candidate is still active.
    for i in range(6):
        await record_transfer_and_evaluate(db_session, _transfer_info(f"second-batch-{i}"), now)

    candidates = await db_session.execute(select(TradingCandidate).where(TradingCandidate.token_id == token.id))
    assert len(candidates.scalars().all()) == 1
