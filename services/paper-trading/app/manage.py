from datetime import datetime
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MLFeatureSnapshot, PaperPosition, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.state_machine import CandidateState, apply_transition

log = get_logger("paper-trading.manage")

# Backfilling ml_features.label from a real, realized outcome is the one
# thing this codebase ever does to it — see docs/ML.md and
# docs/PAPER_TRADING.md for the full loop this closes.
LABEL_SOURCE = "paper_trading_realized_pnl"


def _check_exit(position: PaperPosition, current_price: Decimal) -> tuple[str, Decimal] | None:
    """Returns (exit_reason, exit_price) if `current_price` has crossed a
    stop-loss or take-profit level on this position, else None. The exit
    price used is the triggered level itself — a conservative simulation
    assumption (a real fill could slip past it) — never anything beyond
    what the position's own configured levels say.
    """
    is_long = position.side != "SHORT"

    if position.stop_loss is not None:
        stop_hit = current_price <= position.stop_loss if is_long else current_price >= position.stop_loss
        if stop_hit:
            return "stop_loss", position.stop_loss

    # take_profit is persisted as a list of strings (matching
    # StrategySignal.take_profit / Decision.to_dict()'s convention of
    # serializing every Decimal to a string for JSONB storage).
    levels = sorted((Decimal(level) for level in position.take_profit or []), reverse=not is_long)
    for level in levels:
        take_profit_hit = current_price >= level if is_long else current_price <= level
        if take_profit_hit:
            return "take_profit", level

    return None


async def close_position(session: AsyncSession, position: PaperPosition, exit_price: Decimal, exit_reason: str, now: datetime) -> None:
    """Closes `position` and, if it has a candidate, backfills every
    still-unlabeled ml_features row tied to that candidate with the real
    realized outcome — profitable exit -> label=1, otherwise 0 — and
    advances the candidate through MANAGING -> EXIT_SIGNAL -> EXITING ->
    CLOSED. Caller commits nothing before this returns; this function owns
    the transaction.
    """
    is_long = position.side != "SHORT"
    position.status = "closed"
    position.exit_price = exit_price
    position.exit_at = now
    position.exit_reason = exit_reason
    position.realized_pnl = (exit_price - position.entry_price) * position.quantity if is_long else (position.entry_price - exit_price) * position.quantity
    position.realized_pnl_pct = position.realized_pnl / (position.entry_price * position.quantity)

    if position.candidate_id is not None:
        label = 1 if position.realized_pnl > 0 else 0
        await session.execute(
            update(MLFeatureSnapshot)
            .where(MLFeatureSnapshot.candidate_id == position.candidate_id, MLFeatureSnapshot.label.is_(None))
            .values(label=label, label_source=LABEL_SOURCE)
        )

        candidate = await session.get(TradingCandidate, position.candidate_id)
        if candidate is not None and candidate.state == CandidateState.MANAGING.value:
            apply_transition(candidate, CandidateState.EXIT_SIGNAL, reason=exit_reason)
            apply_transition(candidate, CandidateState.EXITING, reason=exit_reason)
            apply_transition(candidate, CandidateState.CLOSED, reason=f"paper position closed: {exit_reason} @ {exit_price}")

    await session.commit()
    log.info(
        "manage.closed",
        candidate_id=str(position.candidate_id) if position.candidate_id else None,
        symbol=position.symbol,
        exit_reason=exit_reason,
        realized_pnl=str(position.realized_pnl),
    )


async def evaluate_open_position(session: AsyncSession, position: PaperPosition, current_price: Decimal, now: datetime) -> bool:
    """Returns True if `position` was closed. This module never sources a
    price itself (see app/pricing.py and docs/PAPER_TRADING.md for why no
    such source exists for Solana today) — the caller supplies
    `current_price`, keeping this function's exit logic fully testable
    independent of that gap.
    """
    exit_info = _check_exit(position, current_price)
    if exit_info is None:
        return False
    exit_reason, exit_price = exit_info
    await close_position(session, position, exit_price, exit_reason, now)
    return True
