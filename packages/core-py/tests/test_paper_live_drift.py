"""Paper / live parity, price drift (2026-10-09): a paper fill happens at the
decision's price, a LIVE order lands seconds later after the market moved.
The median adverse move measured on confirmed LIVE orders (buys: decision ->
build -> landing; sells: expected vs received, network fee added back) is
charged to paper fills once MIN_LIVE_SAMPLE orders of a side are measured;
a favourable median is never credited."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

from decimal import Decimal  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import paper_engine, paper_execution  # noqa: E402
from yonixalpha_core.db import models  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.safety import assess, store  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings  # noqa: E402

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


def buy_diag(decision_to_build: str | None, build_to_landing: str | None) -> dict:
    return {"price": {"components_pct": {"decision_to_build_pct": decision_to_build,
                                         "build_to_landing_pct": build_to_landing}}}


def sell_diag(expected: str, all_in: str, fee: str = "0") -> dict:
    return {"decision": {"price_sol": expected}, "price": {"all_in_price_sol": all_in, "network_fee_sol": fee}}


def order(i: int, side: str, diagnostics: dict, amount: str = "1000000", decimals: int = 6) -> models.ExecutionOrder:
    return models.ExecutionOrder(mode="LIVE", side=side, reason="entry" if side == "BUY" else "stop_loss", mint=f"M{i}",
                                 provider="pumpportal_local", route="pump", amount=amount,
                                 amount_kind="sol" if side == "BUY" else "tokens", slippage_pct=Decimal(15),
                                 priority_fee_sol=Decimal("0.0001"), idempotency_key=f"k{side}{i}", status="CONFIRMED",
                                 diagnostics=diagnostics, limits={"decimals": decimals})


def test_buy_and_sell_drift_from_one_order():
    assert paper_execution.buy_drift_pct(buy_diag("2", "3")) == Decimal("5.06")  # 1.02 * 1.03 - 1
    assert paper_execution.buy_drift_pct(buy_diag(None, "-1")) == Decimal("-1")
    assert paper_execution.buy_drift_pct({}) is None
    # sold 1 token (1e6 raw, 6 decimals) expecting 0.0100 SOL; got 0.0094 net after a 0.0001 SOL fee -> 5 % short
    assert paper_execution.sell_drift_pct(sell_diag("0.0100", "0.0094", "0.0001"), "1000000", 6) == Decimal("5.00")
    assert paper_execution.sell_drift_pct({"price": {}}, "1000000", 6) is None


async def test_drift_needs_enough_live_orders_and_never_credits(db):
    for i in range(paper_execution.MIN_LIVE_SAMPLE - 1):
        db.add(order(i, "BUY", buy_diag("1", "2")))
    await db.commit()
    d = await paper_execution.measured_live_drift(db)
    assert d["buy_pct"] is None and d["buy_n"] == paper_execution.MIN_LIVE_SAMPLE - 1  # not enough yet
    db.add(order(99, "BUY", buy_diag("1", "2")))
    for i in range(paper_execution.MIN_LIVE_SAMPLE):
        db.add(order(100 + i, "SELL", sell_diag("0.0100", "0.0110")))  # every sell got MORE than expected
    await db.commit()
    d = await paper_execution.measured_live_drift(db)
    assert d["buy_pct"] == Decimal("3.0200") and d["buy_n"] == paper_execution.MIN_LIVE_SAMPLE
    assert d["sell_pct"] == Decimal("0.0000")  # a favourable median is not credited to paper


async def test_drift_switched_off_charges_nothing(db):
    from yonixalpha_core.db.models import PlatformSetting

    db.add(PlatformSetting(key=paper_execution.SETTINGS_KEY, value={"charge_measured_live_drift": False}))
    for i in range(paper_execution.MIN_LIVE_SAMPLE):
        db.add(order(i, "BUY", buy_diag("5", "5")))
    await db.commit()
    d = await paper_execution.measured_live_drift(db)
    assert d["buy_pct"] is None and d["source"] == "switched off"


async def _round_trip(db, key: str, entry_drift=None, exit_drift=None):
    a = assess(healthy(), SafetySettings())
    assert a.executable, a.reasons
    acct = await store.get_paper_account(db, "solana")
    row, _ = await store.persist_assessment(db, a, None, key)
    pos = await paper_engine.open_position(db, acct, a, row.id, None, MODEL, None, None, NOW,
                                           venue={"kind": "spot", "type": "pump_curve"}, entry_drift_pct=entry_drift)
    pos.execution_mode = "PAPER"
    pos.exit_requested = True
    await paper_engine.apply_step(db, pos, acct, a.plan.entry_price, MODEL, None, NOW, exit_drift_pct=exit_drift)
    await db.commit()
    assert pos.status == "closed"
    return pos


async def test_paper_fills_pay_the_measured_drift(db):
    plain = await _round_trip(db, "plain")
    drifted = await _round_trip(db, "drifted", entry_drift=Decimal("4"), exit_drift=Decimal("3"))
    assert drifted.entry_cost_quote == plain.entry_cost_quote  # same SOL spent ...
    assert drifted.initial_quantity * Decimal("1.04") == plain.initial_quantity  # ... for 4 % fewer tokens
    assert drifted.entry_price > plain.entry_price
    assert drifted.plan["venue"]["live_drift_pct"] == "4"
    assert drifted.realized_pnl < plain.realized_pnl  # paper can no longer look better than LIVE lands
    zero = await _round_trip(db, "zero", entry_drift=Decimal(0), exit_drift=Decimal(0))
    assert zero.realized_pnl == plain.realized_pnl and "live_drift_pct" not in zero.plan["venue"]


def test_drift_setting_is_on_by_default():
    assert paper_execution.PaperExecutionSettings().charge_measured_live_drift is True
    s, errors = paper_execution.parse_settings({"charge_measured_live_drift": False})
    assert not errors and s.to_dict()["charge_measured_live_drift"] is False
