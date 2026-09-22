from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import PaperPosition, StrategySignal, TradingCandidate
from yonixalpha_core.decision import DecisionType
from yonixalpha_core.execution_router import ExecutionProvider, RoutingContext, route
from yonixalpha_core.logging import get_logger
from yonixalpha_core.state_machine import CandidateState, apply_transition

log = get_logger("paper-trading.entry")

# A fixed notional size for every simulated position. Paper trading has no
# real capital and no position-sizing algorithm to draw from (Phase 5's
# RiskContext.proposed_position_size is always 0 in this codebase today —
# see services/decision-engine/app/evaluate.py) — this is a simulation
# parameter, not a fabricated market number.
DEFAULT_PAPER_POSITION_SIZE_USD = Decimal("100")


async def _latest_strategy_signal(session: AsyncSession, candidate_id) -> StrategySignal | None:
    result = await session.execute(
        select(StrategySignal).where(StrategySignal.candidate_id == candidate_id).order_by(StrategySignal.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def try_open_position(
    session: AsyncSession, candidate: TradingCandidate, now: datetime, *, migration_confirmed: bool = False
) -> PaperPosition | None:
    """Attempts to simulate entering the position decision-engine approved
    for `candidate` (expected to be QUALIFIED). Returns the new
    PaperPosition on success. On failure the candidate is transitioned to
    REJECTED with the concrete reason rather than left stuck in QUALIFIED
    forever — per docs/PAPER_TRADING.md, one of the checks below always
    fails today: no signal engine in this codebase has ever populated
    Decision.entry (there is no Solana price feed to fill at), so the
    entry-price gate blocks every attempt regardless of routing.

    `migration_confirmed` defaults to False — the same conservative
    default execution_router.route() itself uses — and is never passed
    True by app/main.py: nothing in this codebase can honestly determine
    that a candidate's migration was verified (see execution_router.py's
    own docstring on why `candidate.engine == "migration"` alone is not
    sufficient evidence). It exists as a parameter purely so the success
    path below is exercisable by a test without weakening that default.
    """
    signal = await _latest_strategy_signal(session, candidate.id)
    if signal is None or signal.decision != DecisionType.LONG.value:
        apply_transition(candidate, CandidateState.REJECTED, reason="no LONG strategy signal found for this candidate")
        await session.commit()
        return None

    if signal.entry is None:
        apply_transition(
            candidate,
            CandidateState.REJECTED,
            reason="cannot paper-trade without a concrete entry price (no Solana price feed exists in this codebase)",
        )
        await session.commit()
        return None

    # A non-positive entry price is not merely useless, it is corrupting:
    # it makes `quantity` below undefined (ZeroDivisionError) and produces a
    # position whose cost basis is zero, which then breaks PnL at close
    # time. Reachable for real — StrategySignal.entry is Numeric(38, 18),
    # so any true price below 1e-18 (not unusual for a Solana memecoin
    # quoted per raw unit) rounds to exactly 0 on write.
    if signal.entry <= 0:
        apply_transition(
            candidate,
            CandidateState.REJECTED,
            reason=f"refusing to paper-trade a non-positive entry price ({signal.entry}) — likely a feed error or a price below Numeric(38,18) resolution",
        )
        await session.commit()
        return None

    provider, routing_reasons = route(RoutingContext(asset_class="solana", migration_confirmed=migration_confirmed))
    if provider == ExecutionProvider.UNSUPPORTED:
        apply_transition(candidate, CandidateState.REJECTED, reason="; ".join(routing_reasons))
        await session.commit()
        return None

    quantity = DEFAULT_PAPER_POSITION_SIZE_USD / signal.entry
    position = PaperPosition(
        candidate_id=candidate.id,
        symbol=signal.symbol,
        provider=provider.value,
        side=signal.decision,
        entry_price=signal.entry,
        quantity=quantity,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        entry_at=now,
    )
    session.add(position)

    # A paper fill is instant — there's no broker round-trip to wait on —
    # so the candidate advances straight through to MANAGING in one call.
    apply_transition(candidate, CandidateState.ENTRY_PENDING, reason=f"paper entry at {signal.entry}")
    apply_transition(candidate, CandidateState.ENTERED, reason="paper fill simulated")
    apply_transition(candidate, CandidateState.MANAGING, reason="position open")

    await session.commit()
    log.info("entry.opened", candidate_id=str(candidate.id), symbol=signal.symbol, entry_price=str(signal.entry), provider=provider.value)
    return position
