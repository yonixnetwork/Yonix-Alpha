"""Paper / live parity (audit 2026-10-07): a Solana paper position whose
plan counts the LIVE round trip's fixed costs is charged them on the paper
book: the buy fee with the entry, the sell fee with every exit and the rent
reclaim fee on close. Without them nothing changes."""

import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import paper_engine, paper_execution  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.live_trading import LiveExecutionSettings, fixed_trade_costs  # noqa: E402
from yonixalpha_core.safety import assess  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings  # noqa: E402

STORED = Decimal("1e-17")  # paper_accounts.cash_balance keeps 18 decimals; a few roundings stay below this

from tests.test_safety_gate import MODEL, NOW, healthy  # noqa: E402


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def _round_trip(db, key: str, **inp):
    # Fixed-cost accounting, independent of the stop-gap sizing assumption (off here).
    a = assess(healthy(**inp), SafetySettings(sizing_stop_gap_curve_pct=Decimal(0), sizing_stop_gap_pool_pct=Decimal(0)))
    assert a.executable, a.reasons
    acct = await store.get_paper_account(db, "solana")
    before = acct.cash_balance
    row, _ = await store.persist_assessment(db, a, None, key)
    pos = await paper_engine.open_position(db, acct, a, row.id, None, MODEL, None, None, NOW,
                                           venue={"kind": "spot", "type": "pump_curve"})
    pos.execution_mode = "PAPER"
    pos.exit_requested = True
    await paper_engine.apply_step(db, pos, acct, a.plan.entry_price, MODEL, None, NOW)
    await db.commit()
    assert pos.status == "closed"
    return a, pos, acct, before


async def test_live_fixed_costs_are_charged_to_the_paper_book(db):
    fixed, detail = fixed_trade_costs(LiveExecutionSettings())
    fees = paper_engine.paper_fixed_fees(detail)
    assert fees["buy"] + fees["sell"] + fees["close"] == fixed

    a, pos, acct, before = await _round_trip(db, "fixed", fixed_cost_quote=fixed, fixed_cost_detail=detail,
                                             paper_fixed_costs=True)
    assert a.plan.fixed_cost_quote == fixed
    plain_fill = paper_engine.entry_fill(a.plan.position_size.value, MODEL, None, a.plan.entry_price,
                                         a.plan.entry_cost_bps, None)
    assert pos.entry_cost_quote == a.plan.position_size.value + fees["buy"]
    assert pos.fees_paid_quote >= plain_fill.fee_quote + fixed  # pool fees on both legs plus every fixed cost
    # the book moved by exactly the realized result, at the 18 decimals the database stores (each atomic
    # cash change is rounded to the column, safety.store.adjust_cash)
    assert abs(acct.cash_balance - (before + pos.realized_pnl)) <= STORED


async def test_paper_without_fixed_costs_is_unchanged(db):
    a, pos, acct, before = await _round_trip(db, "plain")
    assert a.plan.fixed_cost_quote is None and pos.entry_cost_quote == a.plan.position_size.value
    assert abs(acct.cash_balance - (before + pos.realized_pnl)) <= STORED


async def test_the_fixed_cost_difference_is_exactly_the_fixed_costs(db):
    fixed, detail = fixed_trade_costs(LiveExecutionSettings())
    # same size both ways: a budget large enough that the fixed costs do not bind the size
    a1, p1, _, _ = await _round_trip(db, "with", fixed_cost_quote=fixed, fixed_cost_detail=detail, paper_fixed_costs=True)
    a2, p2, _, _ = await _round_trip(db, "without")
    if a1.plan.position_size.value == a2.plan.position_size.value:
        assert p2.realized_pnl - p1.realized_pnl == fixed
    else:  # the fixed costs reduced the size (the risk budget binds): still charged in full
        assert p1.entry_cost_quote - a1.plan.position_size.value == paper_engine.paper_fixed_fees(detail)["buy"]


def test_the_setting_is_on_by_default_and_can_be_switched_off():
    assert paper_execution.PaperExecutionSettings().charge_live_fixed_costs is True
    s, errors = paper_execution.parse_settings({"charge_live_fixed_costs": False})
    assert not errors and s.charge_live_fixed_costs is False and s.to_dict()["charge_live_fixed_costs"] is False
    assert paper_execution.parse_settings({"charge_live_fixed_costs": "yes"})[1]


async def test_every_exit_step_is_kept_on_the_position(db):
    # exit_reason keeps only the last step; the whole exit path stays on the position (2026-10-25)
    _, pos, _, _ = await _round_trip(db, "steps")
    steps = pos.plan[paper_engine.EXIT_STEPS_KEY]
    assert [s["reason"] for s in steps] == ["manual_exit"] and pos.exit_reason == "manual_exit"
    assert Decimal(steps[0]["received"]) == pos.proceeds_quote + (pos.plan.get("fixed_cost_detail") or {}).get("close", 0)
    for i in range(paper_engine.MAX_EXIT_STEPS + 5):
        paper_engine.record_exit_step(pos, NOW, f"take_profit_{i}", Decimal(1), Decimal(0), None)
    kept = pos.plan[paper_engine.EXIT_STEPS_KEY]
    assert len(kept) == paper_engine.MAX_EXIT_STEPS and kept[-1]["reason"] == f"take_profit_{paper_engine.MAX_EXIT_STEPS + 4}"
