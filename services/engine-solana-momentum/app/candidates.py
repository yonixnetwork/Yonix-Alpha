from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.state_machine import TERMINAL_STATES, CandidateState

from app.momentum import compute_transfer_acceleration

log = get_logger("engine-solana-momentum.candidates")

SOURCE = "engine-solana-momentum"


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


async def record_transfer_and_evaluate(
    session: AsyncSession, transfer_info: dict[str, Any], occurred_at: datetime
) -> TradingCandidate | None:
    """Records a transferChecked event for an already-known token (this
    engine deliberately does NOT create Token rows for mints it has never
    seen before — per the spec, Engine C tracks *existing* tokens; brand
    new ones are Engine A's job) and, if the resulting transaction-count
    acceleration crosses the threshold, opens a fresh momentum
    TradingCandidate — unless one is already active for this token, in
    which case this is a no-op (avoid duplicate candidates for an ongoing
    acceleration episode).
    """
    token_result = await session.execute(select(Token).where(Token.mint_address == transfer_info["mint"]))
    token = token_result.scalar_one_or_none()
    if token is None:
        await session.commit()
        return None

    event_insert = (
        insert(TokenEvent)
        .values(
            token_id=token.id,
            event_type="transfer",
            source=SOURCE,
            occurred_at=occurred_at,
            signature=transfer_info.get("signature"),
            trader_address=transfer_info.get("authority"),
            token_amount=_to_decimal(transfer_info.get("amount")),
            payload=transfer_info,
        )
        .on_conflict_do_nothing(constraint="uq_token_events_source_signature_type_token")
        .returning(TokenEvent.id)
    )
    result = await session.execute(event_insert)
    if result.scalar_one_or_none() is None:
        await session.commit()
        return None  # already recorded this exact transfer

    token.last_event_at = occurred_at

    acceleration = await compute_transfer_acceleration(session, token.id, occurred_at)
    if not acceleration.is_accelerating:
        await session.commit()
        return None

    active_terminal_values = {s.value for s in TERMINAL_STATES}
    existing_result = await session.execute(
        select(TradingCandidate.id).where(
            TradingCandidate.token_id == token.id,
            TradingCandidate.engine == "momentum",
            TradingCandidate.state.notin_(active_terminal_values),
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        await session.commit()
        return None  # already tracking this token's current acceleration episode

    detail = {
        "current_window_count": acceleration.current_count,
        "prior_window_count": acceleration.prior_count,
        "ratio": acceleration.ratio,
    }
    candidate = TradingCandidate(
        token_id=token.id,
        engine="momentum",
        state=CandidateState.DISCOVERED.value,
        state_history=[
            {
                "state": CandidateState.DISCOVERED.value,
                "at": occurred_at.isoformat(),
                "reason": f"transfer acceleration ratio={acceleration.ratio:.2f}",
            }
        ],
        detail=detail,
    )
    session.add(candidate)
    await session.commit()

    log.info("candidates.momentum_detected", mint=transfer_info["mint"], candidate_id=str(candidate.id), **detail)
    return candidate
