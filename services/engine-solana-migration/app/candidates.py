from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.state_machine import TERMINAL_STATES, CandidateState

log = get_logger("engine-solana-migration.candidates")

SOURCE = "engine-solana-migration"


async def record_migration_detected(
    session: AsyncSession, mint_address: str, migration_info: dict[str, Any], occurred_at: datetime
) -> TradingCandidate | None:
    """Records a detected migration/graduation event for a token and opens
    an engine="migration" TradingCandidate — unless one is already active
    for this token, or the token isn't known yet (Token rows are created
    by engine-solana-discovery; this engine never invents one, same
    boundary as engine-solana-momentum).

    Per spec section 6: migration is an event, not a buy signal — this
    function's only job is to get the event and a fresh candidate into
    Postgres. Whether it's worth acting on is the Risk/Decision Engine's
    call (Phase 5), not this engine's.
    """
    token_result = await session.execute(select(Token).where(Token.mint_address == mint_address))
    token = token_result.scalar_one_or_none()
    if token is None:
        log.warning("candidates.migration_for_unknown_token", mint=mint_address)
        await session.commit()
        return None

    event_insert = (
        insert(TokenEvent)
        .values(
            token_id=token.id,
            event_type="migration",
            source=SOURCE,
            occurred_at=occurred_at,
            signature=migration_info.get("source_signature"),
            payload=migration_info,
        )
        .on_conflict_do_nothing(constraint="uq_token_events_source_signature_type_token")
        .returning(TokenEvent.id)
    )
    result = await session.execute(event_insert)
    if result.scalar_one_or_none() is None:
        await session.commit()
        return None  # already recorded this exact migration event

    token.last_event_at = occurred_at

    active_terminal_values = {s.value for s in TERMINAL_STATES}
    existing_result = await session.execute(
        select(TradingCandidate.id).where(
            TradingCandidate.token_id == token.id,
            TradingCandidate.engine == "migration",
            TradingCandidate.state.notin_(active_terminal_values),
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        await session.commit()
        return None

    candidate = TradingCandidate(
        token_id=token.id,
        engine="migration",
        state=CandidateState.DISCOVERED.value,
        state_history=[
            {"state": CandidateState.DISCOVERED.value, "at": occurred_at.isoformat(), "reason": "pool initialization detected"}
        ],
        detail=migration_info,
    )
    session.add(candidate)
    await session.commit()

    log.info("candidates.migration_detected", mint=mint_address, candidate_id=str(candidate.id))
    return candidate
