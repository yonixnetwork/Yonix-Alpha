from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.manage import close_position, evaluate_open_position
from yonixalpha_core.db.models import MLFeatureSnapshot, PaperPosition, Token, TradingCandidate
from yonixalpha_core.state_machine import CandidateState

MINT = "TestMint111111111111111111111111111111111"


async def _make_managing_candidate(db_session) -> TradingCandidate:
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    now = datetime.now(timezone.utc).isoformat()
    candidate = TradingCandidate(
        token_id=token.id,
        engine="momentum",
        state=CandidateState.MANAGING.value,
        state_history=[
            {"state": s, "at": now, "reason": "test"}
            for s in (
                CandidateState.DISCOVERED.value,
                CandidateState.OBSERVING.value,
                CandidateState.QUALIFIED.value,
                CandidateState.ENTRY_PENDING.value,
                CandidateState.ENTERED.value,
                CandidateState.MANAGING.value,
            )
        ],
    )
    db_session.add(candidate)
    await db_session.flush()
    return candidate


def _position(candidate_id=None, side="LONG", entry_price="0.0010", stop_loss="0.0008", take_profit=None) -> PaperPosition:
    return PaperPosition(
        candidate_id=candidate_id,
        symbol=MINT,
        provider="jupiter",
        side=side,
        entry_price=Decimal(entry_price),
        quantity=Decimal("100000"),
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        take_profit=take_profit or ["0.0012", "0.0015"],
        entry_at=datetime.now(timezone.utc),
    )


async def test_no_exit_when_price_between_stop_and_take_profit(db_session):
    position = _position()
    db_session.add(position)
    await db_session.commit()

    closed = await evaluate_open_position(db_session, position, Decimal("0.0010"), datetime.now(timezone.utc))

    assert closed is False
    assert position.status == "open"


async def test_long_stop_loss_triggers_close(db_session):
    position = _position()
    db_session.add(position)
    await db_session.commit()

    closed = await evaluate_open_position(db_session, position, Decimal("0.0007"), datetime.now(timezone.utc))

    assert closed is True
    assert position.status == "closed"
    assert position.exit_reason == "stop_loss"
    assert position.exit_price == Decimal("0.0008")
    assert position.realized_pnl < 0


async def test_long_take_profit_triggers_close_at_lowest_reached_level(db_session):
    position = _position(take_profit=["0.0012", "0.0015"])
    db_session.add(position)
    await db_session.commit()

    closed = await evaluate_open_position(db_session, position, Decimal("0.0013"), datetime.now(timezone.utc))

    assert closed is True
    assert position.exit_reason == "take_profit"
    assert position.exit_price == Decimal("0.0012")
    assert position.realized_pnl > 0


async def test_short_side_stop_and_take_profit_are_inverted(db_session):
    position = _position(side="SHORT", entry_price="100", stop_loss="110", take_profit=["90", "80"])
    db_session.add(position)
    await db_session.commit()

    closed = await evaluate_open_position(db_session, position, Decimal("111"), datetime.now(timezone.utc))

    assert closed is True
    assert position.exit_reason == "stop_loss"
    assert position.realized_pnl < 0  # price rose against a short


async def test_close_backfills_null_ml_labels_as_profitable(db_session):
    candidate = await _make_managing_candidate(db_session)
    db_session.add(MLFeatureSnapshot(candidate_id=candidate.id, symbol=MINT, features={"a": 1.0}, label=None))
    db_session.add(MLFeatureSnapshot(candidate_id=candidate.id, symbol=MINT, features={"a": 2.0}, label=None))
    position = _position(candidate_id=candidate.id)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.0015"), "take_profit", datetime.now(timezone.utc))

    rows = (await db_session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == candidate.id))).scalars().all()
    assert len(rows) == 2
    assert all(r.label == 1 for r in rows)
    assert all(r.label_source == "paper_trading_realized_pnl" for r in rows)


async def test_close_backfills_null_ml_labels_as_unprofitable(db_session):
    candidate = await _make_managing_candidate(db_session)
    db_session.add(MLFeatureSnapshot(candidate_id=candidate.id, symbol=MINT, features={"a": 1.0}, label=None))
    position = _position(candidate_id=candidate.id)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.0008"), "stop_loss", datetime.now(timezone.utc))

    rows = (await db_session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == candidate.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].label == 0


async def test_close_never_overwrites_an_already_labeled_row(db_session):
    candidate = await _make_managing_candidate(db_session)
    db_session.add(MLFeatureSnapshot(candidate_id=candidate.id, symbol=MINT, features={"a": 1.0}, label=1, label_source="manual"))
    position = _position(candidate_id=candidate.id)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.0008"), "stop_loss", datetime.now(timezone.utc))

    rows = (await db_session.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == candidate.id))).scalars().all()
    assert rows[0].label == 1
    assert rows[0].label_source == "manual"


async def test_close_advances_candidate_from_managing_to_closed(db_session):
    candidate = await _make_managing_candidate(db_session)
    position = _position(candidate_id=candidate.id)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.0015"), "take_profit", datetime.now(timezone.utc))

    assert candidate.state == CandidateState.CLOSED.value
    states = [h["state"] for h in candidate.state_history]
    assert states[-3:] == [CandidateState.EXIT_SIGNAL.value, CandidateState.EXITING.value, CandidateState.CLOSED.value]


async def test_close_without_candidate_link_does_not_error(db_session):
    position = _position(candidate_id=None)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.0015"), "take_profit", datetime.now(timezone.utc))

    assert position.status == "closed"
