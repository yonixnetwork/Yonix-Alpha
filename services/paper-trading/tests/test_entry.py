from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.entry import DEFAULT_PAPER_POSITION_SIZE_USD, try_open_position
from yonixalpha_core.db.models import PaperPosition, StrategySignal, Token, TradingCandidate
from yonixalpha_core.decision import DataQuality, DecisionType
from yonixalpha_core.state_machine import CandidateState

MINT = "TestMint111111111111111111111111111111111"


async def _make_qualified_candidate(db_session) -> TradingCandidate:
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    candidate = TradingCandidate(
        token_id=token.id,
        engine="momentum",
        state=CandidateState.QUALIFIED.value,
        state_history=[
            {"state": s, "at": datetime.now(timezone.utc).isoformat(), "reason": "test"}
            for s in (
                CandidateState.DISCOVERED.value,
                CandidateState.OBSERVING.value,
                CandidateState.QUALIFIED.value,
            )
        ],
    )
    db_session.add(candidate)
    await db_session.flush()
    return candidate


async def _add_signal(db_session, candidate, **overrides) -> StrategySignal:
    defaults = dict(
        candidate_id=candidate.id,
        symbol=MINT,
        decision=DecisionType.LONG.value,
        confidence=Decimal("0.7"),
        entry_type="MARKET",
        entry=Decimal("0.001"),
        stop_loss=Decimal("0.0008"),
        take_profit=["0.0012", "0.0015"],
        risk_score=Decimal("0.3"),
        reason=["test"],
        data_quality=DataQuality.HEALTHY.value,
    )
    defaults.update(overrides)
    signal = StrategySignal(**defaults)
    db_session.add(signal)
    await db_session.commit()
    return signal


async def test_no_signal_rejects_candidate(db_session):
    candidate = await _make_qualified_candidate(db_session)
    await db_session.commit()

    position = await try_open_position(db_session, candidate, datetime.now(timezone.utc))

    assert position is None
    assert candidate.state == CandidateState.REJECTED.value
    assert "no LONG strategy signal" in candidate.state_history[-1]["reason"]


async def test_non_long_signal_rejects_candidate(db_session):
    candidate = await _make_qualified_candidate(db_session)
    await _add_signal(db_session, candidate, decision=DecisionType.WAIT.value, entry=None)

    position = await try_open_position(db_session, candidate, datetime.now(timezone.utc))

    assert position is None
    assert candidate.state == CandidateState.REJECTED.value


async def test_missing_entry_price_rejects_candidate(db_session):
    """The state this codebase is always in today: decision-engine never
    sets Decision.entry (see services/decision-engine/app/evaluate.py), so
    this is the gate that structurally blocks every real evaluation.
    """
    candidate = await _make_qualified_candidate(db_session)
    await _add_signal(db_session, candidate, entry=None)

    position = await try_open_position(db_session, candidate, datetime.now(timezone.utc))

    assert position is None
    assert candidate.state == CandidateState.REJECTED.value
    assert "no Solana price feed" in candidate.state_history[-1]["reason"]


async def test_unsupported_route_rejects_candidate_even_with_entry_price(db_session):
    candidate = await _make_qualified_candidate(db_session)
    await _add_signal(db_session, candidate)

    position = await try_open_position(db_session, candidate, datetime.now(timezone.utc))  # migration_confirmed defaults False

    assert position is None
    assert candidate.state == CandidateState.REJECTED.value


async def test_confirmed_route_opens_a_position_and_advances_to_managing(db_session):
    candidate = await _make_qualified_candidate(db_session)
    signal = await _add_signal(db_session, candidate)
    now = datetime.now(timezone.utc)

    position = await try_open_position(db_session, candidate, now, migration_confirmed=True)

    assert position is not None
    assert position.symbol == MINT
    assert position.provider == "jupiter"
    assert position.side == DecisionType.LONG.value
    assert position.entry_price == signal.entry
    assert position.quantity == DEFAULT_PAPER_POSITION_SIZE_USD / signal.entry
    assert position.stop_loss == signal.stop_loss
    assert position.take_profit == signal.take_profit
    assert position.status == "open"
    assert candidate.state == CandidateState.MANAGING.value

    rows = (await db_session.execute(select(PaperPosition).where(PaperPosition.candidate_id == candidate.id))).scalars().all()
    assert len(rows) == 1
