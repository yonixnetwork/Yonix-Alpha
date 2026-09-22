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


BPS_DIVISOR = Decimal(10_000)


async def close_position(
    session: AsyncSession,
    position: PaperPosition,
    exit_price: Decimal,
    exit_reason: str,
    now: datetime,
    *,
    per_leg_cost_bps: Decimal = Decimal(0),
) -> None:
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
    gross_pnl = (exit_price - position.entry_price) * position.quantity if is_long else (position.entry_price - exit_price) * position.quantity

    # Charged on each leg's own notional. At the default 0 bps this is
    # exactly zero and realized_pnl is the gross result — see
    # Settings.PAPER_TRADING_PER_LEG_COST_BPS for why 0 is a declared
    # assumption rather than a claim that trading is free.
    entry_notional = position.entry_price * position.quantity
    exit_notional = exit_price * position.quantity
    trading_cost = (entry_notional + exit_notional) * per_leg_cost_bps / BPS_DIVISOR
    position.realized_pnl = gross_pnl - trading_cost

    # Cost basis can be zero if a position was ever opened at entry_price 0
    # or quantity 0. app/entry.py now refuses to create such a position and
    # the paper_positions CHECK constraints reject it at the DB level, but a
    # row predating those guards must still be closable: percentage return
    # on a zero cost basis is undefined, so it stays NULL (the column is
    # nullable) rather than crashing here. This is not defensive padding —
    # an unguarded divide here previously raised DivisionByZero mid-close,
    # which aborted the whole manage batch and silently stopped every other
    # open position's stop-loss from ever being evaluated.
    cost_basis = position.entry_price * position.quantity
    if cost_basis == 0:
        position.realized_pnl_pct = None
        log.warning(
            "manage.zero_cost_basis",
            position_id=str(position.id),
            symbol=position.symbol,
            entry_price=str(position.entry_price),
            quantity=str(position.quantity),
        )
    else:
        position.realized_pnl_pct = position.realized_pnl / cost_basis

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


async def evaluate_open_position(
    session: AsyncSession,
    position: PaperPosition,
    current_price: Decimal,
    now: datetime,
    *,
    per_leg_cost_bps: Decimal = Decimal(0),
) -> bool:
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
    await close_position(session, position, exit_price, exit_reason, now, per_leg_cost_bps=per_leg_cost_bps)
    return True
