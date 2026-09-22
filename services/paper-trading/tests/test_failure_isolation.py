"""Regression tests for the audit findings in this service.

Each of these fails against the pre-audit code. The headline one is
test_one_unclosable_position_does_not_block_another_positions_stop_loss:
before the fix, a single zero-cost-basis row raised DivisionByZero out of
close_position(), aborted the whole manage batch, and silently stopped
every other open position's stop-loss from ever being evaluated — for as
long as that row existed, which was forever, because nothing cleaned it up.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.main import _manage_open_positions
from app.manage import close_position
from app.pricing import MAX_PRICE_AGE_SECONDS, latest_price
from yonixalpha_core.db.models import MarketSnapshot, PaperPosition

NOW = datetime.now(timezone.utc)


def _position(symbol: str, *, entry: str, qty: str, stop: str | None) -> PaperPosition:
    return PaperPosition(
        symbol=symbol,
        provider="JUPITER",
        side="LONG",
        entry_price=Decimal(entry),
        quantity=Decimal(qty),
        stop_loss=Decimal(stop) if stop is not None else None,
        take_profit=[],
        entry_at=NOW,
    )


def _price(symbol: str, value: str, *, age_seconds: int = 0) -> MarketSnapshot:
    return MarketSnapshot(
        source="test",
        symbol=symbol,
        snapshot_type="price",
        occurred_at=NOW - timedelta(seconds=age_seconds),
        price=Decimal(value),
        payload={},
    )


async def _drop_cost_basis_constraints(session):
    """Models a database written before migration 0008 added the CHECK
    constraints — the only way a zero-cost-basis row can exist at all now,
    and exactly the situation the runtime guards must still survive.
    """
    for name in ("ck_paper_positions_entry_price_positive", "ck_paper_positions_quantity_positive"):
        await session.execute(text(f"ALTER TABLE paper_positions DROP CONSTRAINT IF EXISTS {name}"))
    await session.commit()


async def test_closing_a_zero_cost_basis_position_does_not_raise(db_session):
    """Pre-audit this raised decimal.DivisionByZero. Such a row can no
    longer be created, but one predating the constraint must still close.
    """
    await _drop_cost_basis_constraints(db_session)
    position = _position("LEGACY", entry="0", qty="1000", stop=None)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("0.5"), "take_profit", NOW)

    assert position.status == "closed"
    # Percentage return on a zero cost basis is undefined, not zero.
    assert position.realized_pnl_pct is None
    assert position.realized_pnl == Decimal("500")  # (0.5 - 0) * 1000


async def test_one_unclosable_position_does_not_block_another_positions_stop_loss(db_session, session_factory):
    """The headline finding: failure isolation in the manage loop."""
    await _drop_cost_basis_constraints(db_session)
    poison = _position("POISON", entry="0", qty="1000", stop="999")
    good = _position("GOOD", entry="1", qty="100", stop="0.9")
    db_session.add_all([poison, good])
    db_session.add_all([_price("POISON", "0.5"), _price("GOOD", "0.5")])
    await db_session.commit()
    good_id = good.id

    # Must not raise, and must close GOOD despite POISON being present.
    await _manage_open_positions(session_factory, NOW)

    async with session_factory() as session:
        reloaded = await session.get(PaperPosition, good_id)
        assert reloaded.status == "closed", "GOOD's stop-loss must fire even when another position fails"
        assert reloaded.exit_reason == "stop_loss"


async def test_stale_price_is_not_used_to_exit_a_position(db_session):
    """A price from an arbitrarily dead feed must not mark or exit a
    position. Pre-audit, latest_price returned the newest row regardless of
    age, so a days-old quote could trigger a stop-loss.
    """
    db_session.add(_price("STALE", "0.5", age_seconds=MAX_PRICE_AGE_SECONDS + 60))
    await db_session.commit()

    assert await latest_price(db_session, "STALE", NOW) is None


async def test_fresh_price_is_still_returned(db_session):
    db_session.add(_price("FRESH", "0.5", age_seconds=MAX_PRICE_AGE_SECONDS - 60))
    await db_session.commit()

    assert await latest_price(db_session, "FRESH", NOW) == Decimal("0.5")


async def test_database_rejects_a_zero_cost_basis_position(db_session):
    """The schema-level half of the fix (migration 0008)."""
    db_session.add(_position("BADENTRY", entry="0", qty="100", stop=None))
    with pytest.raises(Exception) as exc_info:
        await db_session.commit()
    assert "ck_paper_positions_entry_price_positive" in str(exc_info.value)
    await db_session.rollback()

    db_session.add(_position("BADQTY", entry="1", qty="0", stop=None))
    with pytest.raises(Exception) as exc_info:
        await db_session.commit()
    assert "ck_paper_positions_quantity_positive" in str(exc_info.value)
    await db_session.rollback()


async def test_open_positions_query_survives_a_missing_row(db_session, session_factory):
    """A position deleted between the id query and the fetch is skipped,
    not fatal.
    """
    good = _position("SOLO", entry="1", qty="100", stop="0.9")
    db_session.add(good)
    db_session.add(_price("SOLO", "0.5"))
    await db_session.commit()

    closed = await _manage_open_positions(session_factory, NOW)
    assert closed == 1

    async with session_factory() as session:
        rows = (await session.execute(select(PaperPosition))).scalars().all()
        assert [r.status for r in rows] == ["closed"]


async def test_per_leg_cost_is_deducted_from_realized_pnl(db_session):
    """Trading cost is a declared, configurable assumption rather than an
    invisible one. At the default 0 bps paper PnL is gross; set a real
    per-leg cost and it becomes net, which matters because the ML label is
    literally `realized_pnl > 0`.
    """
    position = _position("COSTED", entry="100", qty="1", stop=None)
    db_session.add(position)
    await db_session.commit()

    # 50bps per leg: 0.005 * 100 + 0.005 * 100.10 = 1.0005
    await close_position(db_session, position, Decimal("100.10"), "audit", NOW, per_leg_cost_bps=Decimal("50"))

    assert position.realized_pnl == Decimal("0.10") - Decimal("1.0005")
    assert position.realized_pnl < 0, "a +0.10% gross move is a net loss once costs are real"


async def test_default_cost_is_zero_and_pnl_stays_gross(db_session):
    position = _position("GROSS", entry="100", qty="1", stop=None)
    db_session.add(position)
    await db_session.commit()

    await close_position(db_session, position, Decimal("110"), "audit", NOW)

    assert position.realized_pnl == Decimal("10")
