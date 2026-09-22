from datetime import datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import RiskEvent, StrategySignal, Token, TradingCandidate
from yonixalpha_core.decision import Decision, DecisionType, EntryType, no_trade
from yonixalpha_core.logging import get_logger
from yonixalpha_core.risk import DataQuality, RiskConfig, RiskContext, evaluate as evaluate_risk
from yonixalpha_core.state_machine import CandidateState, apply_transition

from app.features import compute_candidate_features
from app.signal import ENTRY_CONFIDENCE_THRESHOLD, score

log = get_logger("decision-engine.evaluate")

# How long a candidate may sit in OBSERVING without qualifying before this
# engine gives up on it. Purely a housekeeping bound (keeps the candidate
# table from accumulating stale OBSERVING rows forever) — it is not itself a
# trading signal, and the value is a conservative operational default rather
# than anything derived from backtested data this codebase doesn't have.
MAX_OBSERVATION_SECONDS = 3600


def _risk_config_from_settings(settings: Settings) -> RiskConfig:
    return RiskConfig(
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        max_position_size=Decimal(str(settings.MAX_POSITION_SIZE)) if settings.MAX_POSITION_SIZE is not None else None,
        max_daily_loss=Decimal(str(settings.MAX_DAILY_LOSS)) if settings.MAX_DAILY_LOSS is not None else None,
        max_open_positions=settings.MAX_OPEN_POSITIONS,
        # MAX_SLIPPAGE (Settings, Phase 1) is a plain fraction, not basis
        # points, and nothing in this evaluation flow ever produces a
        # proposed_slippage_bps for a Solana candidate (no Solana
        # quote/slippage feed exists in this codebase) — mapping it here
        # would imply a unit conversion never verified against a real
        # execution venue, so max_slippage_bps is deliberately left unset.
    )


async def _count_open_positions(session: AsyncSession) -> int:
    """Portfolio-wide count of TradingCandidate rows past ENTERED — the
    only notion of an "open Solana position" this codebase has, since no
    separate Solana position table exists (unlike Binance's Position model
    from Phase 4). Counts across all tokens/engines, not just this
    candidate's own token.
    """
    open_states = [
        CandidateState.ENTERED.value,
        CandidateState.MANAGING.value,
        CandidateState.EXIT_SIGNAL.value,
        CandidateState.EXITING.value,
    ]
    result = await session.execute(select(func.count()).select_from(TradingCandidate).where(TradingCandidate.state.in_(open_states)))
    return result.scalar_one()


def _observation_started_at(candidate: TradingCandidate) -> datetime | None:
    for entry in candidate.state_history or []:
        if entry.get("state") == CandidateState.OBSERVING.value:
            return datetime.fromisoformat(entry["at"])
    return None


async def evaluate_candidate(
    session: AsyncSession, redis: Redis, settings: Settings, candidate: TradingCandidate, now: datetime
) -> Decision:
    """The Phase 5 evaluation pipeline for one Solana TradingCandidate:
    compute features -> score confidence -> check risk -> build a Decision
    -> persist the full audit trail (StrategySignal + RiskEvent) -> advance
    the candidate's state machine. Risk always runs, even when the
    signal-engine confidence already disqualifies the candidate, so the
    audit trail (RiskEvent) is complete regardless of which check actually
    blocked the trade — per spec sections 15/36/38, risk is the final
    authority, never skipped just because a signal already said no.
    """
    if candidate.state == CandidateState.DISCOVERED.value:
        apply_transition(candidate, CandidateState.OBSERVING, reason="entered decision-engine evaluation")

    token = await session.get(Token, candidate.token_id)
    symbol = token.symbol or token.mint_address if token is not None else str(candidate.token_id)

    features = await compute_candidate_features(session, candidate, now)
    confidence, score_reasons = score(features)

    kill_switch_engaged = await kill_switch.is_engaged(redis)
    open_position_count = await _count_open_positions(session)

    risk_config = _risk_config_from_settings(settings)
    risk_context = RiskContext(
        now=now,
        data_quality=features.data_quality,
        kill_switch_engaged=kill_switch_engaged,
        open_position_count=open_position_count,
        # proposed_position_size/current_portfolio_exposure/daily_realized_pnl
        # stay at RiskContext's Decimal(0) defaults: this engine has no
        # position-sizing algorithm and no Solana PnL feed to compute real
        # figures from, so those checks stay honestly unenforced rather
        # than fed a fabricated number.
    )
    verdict = evaluate_risk(risk_config, risk_context)

    if features.data_quality in (DataQuality.STALE, DataQuality.UNAVAILABLE):
        decision_type = DecisionType.NO_TRADE
    elif not verdict.approved:
        decision_type = DecisionType.NO_TRADE
    elif confidence >= ENTRY_CONFIDENCE_THRESHOLD:
        decision_type = DecisionType.LONG
    else:
        decision_type = DecisionType.WAIT

    reasons = list(score_reasons)
    if not verdict.approved:
        reasons.extend(verdict.reasons)

    if decision_type == DecisionType.NO_TRADE and features.data_quality in (DataQuality.STALE, DataQuality.UNAVAILABLE):
        decision = no_trade(reasons[0] if reasons else "insufficient data", data_quality=features.data_quality)
    else:
        decision = Decision(
            decision=decision_type,
            confidence=confidence,
            entry_type=EntryType.MARKET if decision_type == DecisionType.LONG else None,
            entry=None,  # no Solana price feed exists to set a concrete entry price from
            stop_loss=None,
            take_profit=[],
            risk_score=round(1.0 - confidence, 4),
            reason=reasons or ["no qualifying signal"],
            data_quality=features.data_quality,
        )

    session.add(
        StrategySignal(
            candidate_id=candidate.id,
            symbol=symbol,
            decision=decision.decision.value,
            confidence=Decimal(str(decision.confidence)),
            entry_type=decision.entry_type.value if decision.entry_type else None,
            entry=decision.entry,
            stop_loss=decision.stop_loss,
            take_profit=[str(tp) for tp in decision.take_profit],
            risk_score=Decimal(str(decision.risk_score)),
            reason=decision.reason,
            data_quality=decision.data_quality.value,
        )
    )
    session.add(
        RiskEvent(
            candidate_id=candidate.id,
            symbol=symbol,
            approved=verdict.approved,
            reasons=verdict.reasons,
            context={
                "kill_switch_engaged": kill_switch_engaged,
                "open_position_count": open_position_count,
                "data_quality": features.data_quality.value,
            },
        )
    )

    if candidate.state == CandidateState.OBSERVING.value:
        if decision_type == DecisionType.LONG and verdict.approved:
            apply_transition(candidate, CandidateState.QUALIFIED, reason="; ".join(decision.reason))
        else:
            started_at = _observation_started_at(candidate)
            elapsed = (now - started_at).total_seconds() if started_at is not None else 0.0
            if elapsed >= MAX_OBSERVATION_SECONDS:
                apply_transition(
                    candidate,
                    CandidateState.REJECTED,
                    reason=f"observation window ({MAX_OBSERVATION_SECONDS}s) elapsed without qualifying",
                )

    await session.commit()
    log.info(
        "evaluate.decision",
        candidate_id=str(candidate.id),
        symbol=symbol,
        decision=decision.decision.value,
        confidence=decision.confidence,
        risk_approved=verdict.approved,
        candidate_state=candidate.state,
    )
    return decision
