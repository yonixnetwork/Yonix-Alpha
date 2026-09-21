from datetime import datetime, timezone

from sqlalchemy import select

from app.candidates import record_discovered_token
from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.state_machine import CandidateState

MINT_INFO = {
    "mint": "NewMint111111111111111111111111111111111",
    "mint_authority": "Authority1111111111111111111111111111111",
    "decimals": 6,
    "signature": "Sig1111111111111111111111111111111111111111111111111111111111111111111",
    "fee_payer": "Creator11111111111111111111111111111111",
    "block_time": 1700000000,
}


async def test_record_discovered_token_creates_token_event_and_candidate(db_session):
    occurred_at = datetime.now(timezone.utc)
    candidate = await record_discovered_token(db_session, MINT_INFO, occurred_at)

    assert candidate is not None
    assert candidate.state == CandidateState.DISCOVERED.value
    assert candidate.engine == "discovery"

    token_result = await db_session.execute(select(Token).where(Token.mint_address == MINT_INFO["mint"]))
    token = token_result.scalar_one()
    assert token.creator_address == MINT_INFO["fee_payer"]
    assert token.first_seen_source == "engine-solana-discovery"

    event_result = await db_session.execute(select(TokenEvent).where(TokenEvent.token_id == token.id))
    event = event_result.scalar_one()
    assert event.event_type == "created"
    assert event.signature == MINT_INFO["signature"]


async def test_record_discovered_token_is_idempotent_for_same_mint(db_session):
    occurred_at = datetime.now(timezone.utc)
    first = await record_discovered_token(db_session, MINT_INFO, occurred_at)
    second = await record_discovered_token(db_session, MINT_INFO, occurred_at)

    assert first is not None
    assert second is None  # already-known mint: no duplicate token/event/candidate

    token_result = await db_session.execute(select(Token).where(Token.mint_address == MINT_INFO["mint"]))
    assert len(token_result.scalars().all()) == 1

    candidate_result = await db_session.execute(select(TradingCandidate).where(TradingCandidate.token_id == first.token_id))
    assert len(candidate_result.scalars().all()) == 1


async def test_record_discovered_token_different_mints_create_separate_candidates(db_session):
    occurred_at = datetime.now(timezone.utc)
    other_mint_info = {
        **MINT_INFO,
        "mint": "AnotherMint11111111111111111111111111111",
        "signature": "Sig2222222222222222222222222222222222222222222222222222222222222222222",
    }

    first = await record_discovered_token(db_session, MINT_INFO, occurred_at)
    second = await record_discovered_token(db_session, other_mint_info, occurred_at)

    assert first is not None and second is not None
    assert first.token_id != second.token_id


async def test_record_discovered_token_multi_mint_same_signature(db_session):
    """A single transaction creating two mints via CPI (see
    test_extract_mint_creations_inner_instruction_cpi in test_parse.py)
    produces two TokenEvents that legitimately share a signature but
    belong to different tokens — must not collide on the unique
    constraint (source, signature, event_type, token_id).
    """
    occurred_at = datetime.now(timezone.utc)
    second_mint_info = {**MINT_INFO, "mint": "SiblingMint111111111111111111111111111111"}  # same signature

    first = await record_discovered_token(db_session, MINT_INFO, occurred_at)
    second = await record_discovered_token(db_session, second_mint_info, occurred_at)

    assert first is not None and second is not None
    assert first.token_id != second.token_id
