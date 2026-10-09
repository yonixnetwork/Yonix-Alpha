"""Sellable-amount protection (exit_plan, 2026-10-10): partial take-profits
never leave an unsellable remainder, a partial sale too small to pay its own
fee is deferred, protective full exits are never changed, all in integer raw
units without oversell."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

from decimal import Decimal  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import exit_plan as xp  # noqa: E402
from yonixalpha_core import paper_engine  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import TradeTimelineEvent  # noqa: E402
from yonixalpha_core.safety import assess, store  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings  # noqa: E402

from tests.test_safety_gate import MODEL, NOW, healthy  # noqa: E402

FEE = Decimal("0.000105")


def per_raw(price_per_token: str, decimals: int) -> Decimal:
    return Decimal(price_per_token) / Decimal(10) ** decimals


def test_raw_units_never_round_up():
    assert xp.to_raw(Decimal("1.2345679"), 6) == 1_234_567  # ROUND_DOWN: never more than held
    assert xp.to_raw(Decimal("0.0000009"), 6) == 0
    assert xp.to_raw(Decimal("1"), 9) == 1_000_000_000
    assert xp.from_raw(1_234_567, 6) == Decimal("1.234567")


def test_a_dust_remainder_is_folded_into_the_sale():
    # 1 raw unit left after the sale (live_trading.DUST_RAW): dust -> sell everything, even without a price
    c = xp.check_exit(1_000_001, 1_000_000, "take_profit_3", None, FEE)
    assert (c.action, c.sell_raw, c.remainder_raw) == (xp.MERGED_REMAINDER, 1_000_001, 0)
    assert not c.verified and "dust" in c.reason
    # 2 raw units are not dust by count; without a price nothing proves them worthless -> sold as planned
    c = xp.check_exit(1_000_002, 1_000_000, "take_profit_3", None, FEE)
    assert c.action == xp.SELL and c.remainder_raw == 2 and "NOT_VERIFIED" in c.notes[0]


def test_an_uneconomic_remainder_is_folded_into_the_sale():
    # remainder 1,000 raw at 1e-7 SOL/token (6 dp) is worth 1e-10 SOL << 3 sell fees
    c = xp.check_exit(10_000_000_000, 9_999_999_000, "take_profit_2", per_raw("0.0000001", 6), FEE)
    assert c.action == xp.MERGED_REMAINDER and c.sell_raw == 10_000_000_000 and c.remainder_raw == 0


def test_a_tiny_partial_take_profit_is_deferred_not_sold():
    # 40 % of a position worth 0.0002 SOL: 0.00008 SOL < 3 x 0.000105 fee
    c = xp.check_exit(2_000_000, 800_000, "take_profit_1", per_raw("0.0001", 6), FEE)
    assert (c.state, c.action, c.sell_raw, c.remainder_raw) == (xp.BELOW_ROUTE_MINIMUM, xp.DEFERRED, 0, 2_000_000)


def test_a_partial_that_empties_the_position_is_never_deferred():
    c = xp.check_exit(800_000, 800_000, "take_profit_3", per_raw("0.0001", 6), FEE)
    assert c.action == xp.SELL and c.sell_raw == 800_000


def test_protective_full_exits_are_never_changed_or_blocked():
    for reason in ("stop_loss", "trailing_stop", "manual_exit", "exit_intel_exit"):
        c = xp.check_exit(5, 1, reason, per_raw("0.0001", 6), FEE)
        assert (c.state, c.action, c.sell_raw) == (xp.SELLABLE, xp.SELL, 5), reason
        assert "never held back" in c.notes[0]


def test_a_defensive_reduce_is_enlarged_but_never_deferred():
    c = xp.check_exit(20_000_000, 800_000, "exit_intel_reduce", per_raw("0.0001", 6), FEE)
    assert c.action == xp.SELL and c.sell_raw == 800_000  # below 3 fees, but a risk reduction is not postponed
    c = xp.check_exit(800_001, 800_000, "exit_intel_reduce", None, FEE)
    assert c.action == xp.MERGED_REMAINDER


def test_never_more_than_the_position_holds():
    c = xp.check_exit(500, 9_999, "take_profit_1", None, FEE)
    assert c.sell_raw == 500 and c.remainder_raw == 0


def test_balance_route_and_transfer_states():
    assert xp.check_exit(0, 10, "take_profit_1", None, FEE).state == xp.BALANCE_MISMATCH
    nr = xp.check_exit(1_000, 10, "stop_loss", None, FEE, route_available=False)
    assert (nr.state, nr.sell_raw, nr.remainder_raw) == (xp.NO_ROUTE, 0, 1_000)
    tr = xp.check_exit(1_000, 10, "stop_loss", None, FEE, transfer_restricted=True)
    assert tr.state == xp.TRANSFER_RESTRICTION and tr.sell_raw == 0


def test_decimals_change_the_raw_amounts_not_the_decision():
    for dec in (6, 9):
        initial = xp.to_raw(Decimal("1000"), dec)
        c = xp.check_exit(initial, xp.to_raw(Decimal("400"), dec), "take_profit_1", per_raw("0.001", dec), FEE)
        assert c.action == xp.SELL and c.sell_raw == xp.to_raw(Decimal("400"), dec)


def test_schedule_over_the_whole_position_leaves_nothing_unsellable():
    # 0.4 / 0.3 / 0.3 of 1,000,001 raw: floors give 400,000 + 300,000 + 300,000 and leave 1 raw (dust) -> TP3 takes it
    tps = [(Decimal("2"), Decimal("0.4")), (Decimal("3"), Decimal("0.3")), (Decimal("4"), Decimal("0.3"))]
    plan = xp.plan_schedule(1_000_001, 1_000_001, tps, [], None, FEE)
    sells = [lvl["sell_raw"] for lvl in plan["levels"]]
    assert sells == [400_000, 300_000, 300_001] and plan["left_for_trailing_or_stop"]["raw"] == 0
    assert plan["levels"][2]["action"] == xp.MERGED_REMAINDER and "ORIGINAL" in plan["basis"]
    # with TP1 hit already, the plan starts from what is left
    later = xp.plan_schedule(1_000_001, 600_001, tps, [0], None, FEE)
    assert later["levels"][0]["status"] == "HIT" and [lv.get("sell_raw") for lv in later["levels"][1:]] == [300_000, 300_001]


def test_settings_parse_and_mode():
    s, errors = xp.parse_settings({"mode": "PAPER_AND_LIVE", "min_sale_fee_multiple": "5"})
    assert not errors and s.applies(live=True) and s.min_sale_fee_multiple == 5
    _, errors = xp.parse_settings({"mode": "YOLO", "min_remainder_fee_multiple": "-1"})
    assert len(errors) == 2
    default = xp.ExitProtectionSettings()
    assert default.mode == "PAPER" and default.applies(live=False) and not default.applies(live=True)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def _paper_position(db, key: str):
    a = assess(healthy(), SafetySettings())
    acct = await store.get_paper_account(db, "solana")
    row, _ = await store.persist_assessment(db, a, None, key)
    pos = await paper_engine.open_position(db, acct, a, row.id, None, MODEL, None, None, NOW,
                                           venue={"kind": "spot", "type": "pump_curve", "decimals": 6})
    pos.execution_mode = "PAPER"
    return pos, acct


async def test_paper_take_profits_fold_a_dust_remainder_and_close(db):
    pos, acct = await _paper_position(db, "merge")
    e = pos.entry_price
    pos.plan = {**pos.plan, "take_profits": [{"price": {"value": str(e * 2)}, "exit_fraction": "0.6"}]}
    # TP1 sells 60 % of the original; leave exactly the 40 % + 0.000002 tokens (2 raw) for the runner,
    # then take the runner out first so the TP alone would leave 2 raw units
    pos.remaining_quantity = pos.initial_quantity * Decimal("0.6") + Decimal("0.000002")
    res = await paper_engine.apply_step(db, pos, acct, e * 3, MODEL, None, NOW,
                                        exit_protection=xp.ExitProtectionSettings())
    await db.commit()
    assert res.closed and pos.status == "closed" and pos.remaining_quantity == 0
    events = (await db.execute(select(TradeTimelineEvent.event_type).where(
        TradeTimelineEvent.position_id == pos.id))).scalars().all()
    assert "exit_protection.merged_remainder" in events


async def test_paper_without_protection_is_unchanged(db):
    pos, acct = await _paper_position(db, "plain")
    e = pos.entry_price
    pos.plan = {**pos.plan, "take_profits": [{"price": {"value": str(e * 2)}, "exit_fraction": "0.6"}]}
    pos.remaining_quantity = pos.initial_quantity * Decimal("0.6") + Decimal("0.000002")
    res = await paper_engine.apply_step(db, pos, acct, e * 3, MODEL, None, NOW)
    await db.commit()
    assert not res.closed and pos.remaining_quantity == Decimal("0.000002")  # the dust stays (old behaviour)
