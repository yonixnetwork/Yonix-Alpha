from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.state_machine import CandidateState

log = get_logger("engine-solana-discovery.candidates")

SOURCE = "engine-solana-discovery"


async def record_discovered_token(
    session: AsyncSession, mint_info: dict[str, Any], occurred_at: datetime
) -> TradingCandidate | None:
    """Idempotently records a newly-observed SPL mint: Token row (if not
    already known), its creation TokenEvent, and a fresh TradingCandidate
    in DISCOVERED state. Uses ON CONFLICT DO NOTHING keyed on the unique
    mint_address, atomically and race-safely (correct even if this service
    ever runs as multiple replicas) — if the token already exists, this is
    a no-op and returns None: we've already recorded this mint's creation
    and there is nothing new to act on.
    """
    token_insert = (
        insert(Token)
        .values(
            mint_address=mint_info["mint"],
            creator_address=mint_info.get("fee_payer"),
            first_seen_source=SOURCE,
            last_event_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["mint_address"])
        .returning(Token.id)
    )
    result = await session.execute(token_insert)
    token_id = result.scalar_one_or_none()

    if token_id is None:
        log.debug("candidates.mint_already_known", mint=mint_info["mint"])
        await session.commit()
        return None

    session.add(
        TokenEvent(
            token_id=token_id,
            event_type="created",
            source=SOURCE,
            occurred_at=occurred_at,
            signature=mint_info.get("signature"),
            trader_address=mint_info.get("fee_payer"),
            payload=mint_info,
        )
    )

    candidate = TradingCandidate(
        token_id=token_id,
        engine="discovery",
        state=CandidateState.DISCOVERED.value,
        state_history=[
            {
                "state": CandidateState.DISCOVERED.value,
                "at": occurred_at.isoformat(),
                "reason": "SPL mint initialized",
            }
        ],
        detail=mint_info,
    )
    session.add(candidate)
    await session.commit()

    log.info("candidates.discovered", mint=mint_info["mint"], candidate_id=str(candidate.id))
    return candidate
