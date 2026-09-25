"""Order-book execution model, SHORT support, strategy-supplied levels, and
futures paper accounting."""

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import paper_engine  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.liquidity import BOOK_EXHAUSTED_BPS, book_from_levels, close_fill, open_fill  # noqa: E402
from yonixalpha_core.safety.models import (  # noqa: E402
    AccountState,
    AssessmentInput,
    FinalDecision,
    MarketInfo,
    Observation,
    Provenance,
    StrategyLevels,
    StrategySignal,
)
from yonixalpha_core.safety.planning import _loss_fraction, ratchet_trailing_stop  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FRESH = Observation("test", NOW - timedelta(seconds=2))


def book(mid=Decimal("2000"), levels=40, step=Decimal("0.5"), qty=Decimal("5"), fee=Decimal("5")):
    bids = [(mid - step / 2 - step * i, qty) for i in range(levels)]
    asks = [(mid + step / 2 + step * i, qty) for i in range(levels)]
    return book_from_levels(bids, asks, fee)


def futures_input(side="LONG", **kw) -> AssessmentInput:
    m = book()
    base = AssessmentInput(
        engine="binance_futures", strategy_name="meta_muse", asset_id="ETHUSDT", symbol="ETHUSDT", now=NOW,
        market=MarketInfo(FRESH, price=m.mid, volatility=Decimal("0.004"), liquidity_quote=m.liquidity_quote,
                          age_seconds=None),
        token=None, holders=None, flow=None,
        account=AccountState(Decimal(1000), Decimal(1000), 0, Decimal(0), Decimal(0), None, Decimal(0), False),
        liquidity_model=m, signal=StrategySignal("meta_muse", "1", True, 1.0, ["divergence"]), side=side,
    )
    return replace(base, **kw)


SETTINGS = SafetySettings(min_stop_pct=Decimal("0.01"), max_position_size_quote=Decimal("500"),
                          max_total_exposure_quote=Decimal("1000"), max_token_exposure_quote=Decimal("500"),
                          max_pool_fraction=Decimal("0.05"), max_slippage_bps=Decimal("10"))


# --- order book --------------------------------------------------------------

def test_book_walks_levels_and_reports_spread_in_impact():
    m = book()
    assert m.mid == Decimal(2000) and m.spread_bps == Decimal("0.5") / 2000 * 10000
    o = open_fill(m, Decimal("100"), "LONG")  # well inside the first level
    assert o.complete and o.avg_price == Decimal("2000.25")
    assert o.impact_bps == (Decimal("2000.25") / 2000 - 1) * 10000
    big = open_fill(m, Decimal("100000"), "LONG")  # four levels deep
    assert big.avg_price > o.avg_price
    c = close_fill(m, o.quantity, "LONG")
    assert c.avg_price == Decimal("1999.75")


def test_book_that_cannot_absorb_marks_incomplete():
    m = book(levels=2)
    o = open_fill(m, Decimal("10000000"), "LONG")
    assert not o.complete and o.impact_bps == BOOK_EXHAUSTED_BPS


def test_short_opens_on_bids_and_closes_on_asks():
    m = book()
    o = open_fill(m, Decimal("1000"), "SHORT")
    assert o.avg_price < m.mid
    c = close_fill(m, o.quantity, "SHORT")
    assert c.avg_price > m.mid


def test_crossed_book_is_rejected():
    with pytest.raises(ValueError):
        book_from_levels([["101", "1"]], [["100", "1"]], 5)


# --- planning -----------------------------------------------------------------

def test_short_plan_puts_stop_above_and_targets_below():
    a = assess(futures_input("SHORT"), SETTINGS)
    assert a.decision == FinalDecision.EXECUTE, a.reasons
    p = a.plan
    assert p.side == "SHORT" and p.stop_loss.value > p.entry_price
    assert all(tp.price.value < p.entry_price for tp in p.take_profits)
    assert [tp.price.value for tp in p.take_profits] == sorted((tp.price.value for tp in p.take_profits), reverse=True)
    assert p.breakeven_price < p.entry_price
    loss = p.position_size.value * _loss_fraction(p.stop_distance_pct, p.entry_cost_bps, p.exit_cost_bps, "SHORT")
    assert loss <= p.max_loss.value + Decimal("1e-12")


def test_short_loss_fraction_exceeds_long():
    d, c = Decimal("0.02"), Decimal("10")
    assert _loss_fraction(d, c, c, "SHORT") > _loss_fraction(d, c, c, "LONG")


def test_spot_engines_cannot_short():
    a = assess(futures_input("SHORT", engine="solana_fresh"), SETTINGS)
    assert a.decision == FinalDecision.NO_TRADE and "SHORT_NOT_SUPPORTED" in {f.code for f in a.findings}


def test_strategy_levels_are_used_with_strategy_provenance():
    levels = StrategyLevels(stop_loss=Decimal("1960"), take_profits=[Decimal("2060"), Decimal("2120")],
                            move_stop_to_breakeven_at_tp1=True, source="confluence")
    a = assess(futures_input(strategy_levels=levels), SETTINGS)
    assert a.decision == FinalDecision.EXECUTE, a.reasons
    assert a.plan.stop_loss.provenance == Provenance.STRATEGY and a.plan.stop_loss.value == Decimal("1960")
    assert [tp.price.provenance for tp in a.plan.take_profits] == [Provenance.STRATEGY] * 2
    assert [tp.exit_fraction for tp in a.plan.take_profits] == [Decimal("0.5"), Decimal("0.5")]
    assert a.plan.move_stop_to_breakeven_at_tp1


def test_strategy_stop_on_wrong_side_is_refused_not_replaced():
    levels = StrategyLevels(stop_loss=Decimal("2050"), source="confluence")
    a = assess(futures_input(strategy_levels=levels), SETTINGS)
    assert a.decision == FinalDecision.NO_TRADE and "MANUAL_SL_INVALID" in {f.code for f in a.findings}


def test_leverage_scales_balance_cap_only_within_hard_limit():
    inp = futures_input(account=AccountState(Decimal(100), Decimal(100), 0, Decimal(0), Decimal(0), None, Decimal(0), False))
    one = assess(inp, SETTINGS)
    three = assess(inp, replace(SETTINGS, max_leverage=Decimal(3)))
    assert three.plan.position_size.value >= one.plan.position_size.value


def test_short_trailing_only_moves_down():
    assert ratchet_trailing_stop(Decimal("100"), Decimal("90"), Decimal("0.05"), "SHORT") == Decimal("94.50")
    assert ratchet_trailing_stop(Decimal("94.5"), Decimal("99"), Decimal("0.05"), "SHORT") == Decimal("94.5")


# --- management ----------------------------------------------------------------

def test_short_management_stop_tp_and_breakeven():
    s = paper_engine.PositionState(Decimal(10), Decimal(10), Decimal("105"), [(Decimal("95"), Decimal("0.5")), (Decimal("90"), Decimal("0.5"))],
                                   [], True, Decimal("0.02"), Decimal("95"), None, None, None, side="SHORT",
                                   move_stop_to_breakeven_at_tp1=True, breakeven_price=Decimal("99.8"))
    r = paper_engine.manage_step(s, Decimal("94"))
    assert r.exits == [(Decimal(5), "take_profit_1")] and s.stop_loss == Decimal("99.8") and s.trailing_stop is not None
    r2 = paper_engine.manage_step(s, Decimal("99.9"))
    assert r2.closed and r2.exits[0][1] in ("stop_loss", "trailing_stop")


def test_paused_position_still_honours_stop_but_takes_no_profit():
    s = paper_engine.PositionState(Decimal(10), Decimal(10), Decimal("90"), [(Decimal("110"), Decimal("1"))], [], False,
                                   None, None, None, None, None, management_paused=True)
    assert paper_engine.manage_step(s, Decimal("120")).exits == []
    assert paper_engine.manage_step(s, Decimal("89")).exits == [(Decimal(10), "stop_loss")]


def test_exit_now_closes_everything():
    s = paper_engine.PositionState(Decimal(10), Decimal(4), Decimal("90"), [], [], False, None, None, None, None, None)
    r = paper_engine.manage_step(s, Decimal("100"), exit_now=True)
    assert r.closed and r.exits == [(Decimal(4), "manual_exit")]


# --- futures paper accounting ---------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def test_short_futures_round_trip_accounting(db):
    a = assess(futures_input("SHORT"), SETTINGS)
    acct = await store.get_paper_account(db, "binance_futures")
    start = acct.cash_balance
    row, _ = await store.persist_assessment(db, a, None, "short-1")
    pos = await paper_engine.open_position(db, acct, a, row.id, None, a_model := futures_input().liquidity_model, None, None, NOW,
                                           venue={"kind": "futures", "venue": "binance"})
    await db.commit()
    assert pos.side == "SHORT" and acct.cash_balance == start - pos.entry_cost_quote
    # Price falls 3%: a profitable short; exit against a book re-centred there.
    lower = book(mid=Decimal("1940"))
    await paper_engine.apply_step(db, pos, acct, Decimal("1940"), lower, None, NOW + timedelta(minutes=5))
    if pos.status == "open":
        pos.exit_requested = True
        await paper_engine.apply_step(db, pos, acct, Decimal("1940"), lower, None, NOW + timedelta(minutes=6))
    # A closed position is inert: another tick changes nothing.
    assert (await paper_engine.apply_step(db, pos, acct, Decimal("1"), lower, None, NOW + timedelta(minutes=7))).exits == []
    await db.commit()
    assert pos.status == "closed" and pos.realized_pnl > 0
    assert acct.cash_balance == start + pos.realized_pnl
    assert a_model is not None


async def test_equity_marks_futures_as_margin_plus_unrealized(db):
    a = assess(futures_input("LONG"), SETTINGS)
    acct = await store.get_paper_account(db, "binance_futures")
    row, _ = await store.persist_assessment(db, a, None, "long-1")
    pos = await paper_engine.open_position(db, acct, a, row.id, None, futures_input().liquidity_model, None, None, NOW,
                                           venue={"kind": "futures"})
    pos.last_price = pos.entry_price * Decimal("1.01")
    await db.commit()
    st = await store.account_state(db, acct, "ETHUSDT", NOW, False)
    expected_unrealized = (pos.last_price - pos.entry_price) * pos.remaining_quantity
    margin = Decimal(pos.plan["venue"]["margin"])
    assert st.equity == acct.cash_balance + margin + expected_unrealized
    assert st.current_exposure == pos.remaining_quantity * pos.last_price


async def test_fill_fails_when_liquidity_moved_beyond_slippage_limit(db):
    a = assess(futures_input("LONG"), SETTINGS)
    acct = await store.get_paper_account(db, "binance_futures")
    row, _ = await store.persist_assessment(db, a, None, "slip-1")
    thin = book(qty=Decimal("0.02"))  # the book thinned out between decision and fill
    with pytest.raises(paper_engine.FillError):
        await paper_engine.open_position(db, acct, a, row.id, None, futures_input().liquidity_model, None, None, NOW,
                                         venue={"kind": "futures"}, fill_model=thin, max_slippage_bps=Decimal("2"))
