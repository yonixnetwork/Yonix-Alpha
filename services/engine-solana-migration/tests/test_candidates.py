from datetime import datetime, timezone

from sqlalchemy import select

from app.candidates import record_migration_detected
from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate

MINT = "TestMint111111111111111111111111111111111"

MIGRATION_INFO = {
    "pool_address": "Pool111111111111111111111111111111111111",
    "token_mint": MINT,
    "quote_mint": "So11111111111111111111111111111111111111",
    "source_signature": "Sig1111111111111111111111111111111111111111111111111111111111111111111",
    "block_time": 1700000000,
}


async def test_unknown_token_is_ignored(db_session):
    """This engine never creates a Token row — that's
    engine-solana-discovery's job.
    """
    result = await record_migration_detected(db_session, MINT, MIGRATION_INFO, datetime.now(timezone.utc))
    assert result is None

    events = await db_session.execute(select(TokenEvent))
    assert events.scalars().all() == []


async def test_known_token_creates_event_and_candidate(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.commit()

    occurred_at = datetime.now(timezone.utc)
    candidate = await record_migration_detected(db_session, MINT, MIGRATION_INFO, occurred_at)

    assert candidate is not None
    assert candidate.engine == "migration"
    assert candidate.state == "discovered"
    assert candidate.detail["pool_address"] == MIGRATION_INFO["pool_address"]

    event_result = await db_session.execute(select(TokenEvent).where(TokenEvent.token_id == token.id))
    event = event_result.scalar_one()
    assert event.event_type == "migration"
    assert event.signature == MIGRATION_INFO["source_signature"]


async def test_duplicate_migration_event_is_idempotent(db_session):
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.commit()

    occurred_at = datetime.now(timezone.utc)
    first = await record_migration_detected(db_session, MINT, MIGRATION_INFO, occurred_at)
    second = await record_migration_detected(db_session, MINT, MIGRATION_INFO, occurred_at)

    assert first is not None
    assert second is None

    candidates = await db_session.execute(select(TradingCandidate).where(TradingCandidate.token_id == token.id))
    assert len(candidates.scalars().all()) == 1
