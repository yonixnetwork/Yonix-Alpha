"""Holder-change exit signal and the paper execution-failure model."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import paper_execution
from yonixalpha_core.db.base import Base, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, PlatformSetting
from yonixalpha_core.exit_intel import holder_changes, solana_exit_decision
from yonixalpha_core.solana.flow import Trade

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
ENTRY = {"top1_share": "0.05", "top10_share": "0.40", "creator_share": "0.06"}


def sell(i: int, sol: int = 300_000_000, who: str | None = None) -> Trade:
    return Trade(NOW - timedelta(seconds=10 + i), who or f"seller{i}", False, sol, 10**9, 50 * 10**9, 10**15)


def test_holder_changes_are_each_detected():
    assert holder_changes(ENTRY, dict(ENTRY)) == []
    assert "creator holdings fell" in holder_changes(ENTRY, {**ENTRY, "creator_share": "0.02"})[0]
    assert "distribution" in holder_changes(ENTRY, {**ENTRY, "top10_share": "0.20"})[0]
    assert "largest holder" in holder_changes(ENTRY, {**ENTRY, "top1_share": "0.25"})[0]
    # A creator who held under 2% selling it is not a signal; missing data is not a signal.
    assert holder_changes({**ENTRY, "creator_share": "0.01"}, {**ENTRY, "creator_share": "0"}) == []
    assert holder_changes(ENTRY, None) == [] and holder_changes(None, ENTRY) == []


def test_holder_change_alone_never_sells():
    dumped = {**ENTRY, "creator_share": "0"}
    d = solana_exit_decision([], NOW, "creator", None, None, ENTRY, dumped)
    assert d.action == "HOLD" and d.metrics["holder_changes"]


def test_holder_change_with_sell_pressure_exits():
    dumped = {**ENTRY, "creator_share": "0"}
    one_seller = [sell(0)]  # sell pressure, but a single seller: no seller dominance
    assert solana_exit_decision(one_seller, NOW, "creator", None, None).action == "HOLD"
    d = solana_exit_decision(one_seller, NOW, "creator", None, None, ENTRY, dumped)
    assert d.action == "EXIT" and any("creator holdings fell" in r for r in d.reasons)


def test_holder_change_with_liquidity_drop_reduces():
    dist = {**ENTRY, "top10_share": "0.15"}
    buys = [Trade(NOW - timedelta(seconds=5), "b", True, 10**8, 10**9, 50 * 10**9, 10**15)]
    d = solana_exit_decision(buys, NOW, None, Decimal(30), Decimal(22), ENTRY, dist)  # -27% liquidity, no sell pressure
    assert d.action == "REDUCE" and d.fraction == Decimal("0.5")
    assert solana_exit_decision(buys, NOW, None, Decimal(30), Decimal(22)).action == "HOLD"


def test_failure_settings_are_bounded_and_strict():
    s, errors = paper_execution.parse_settings({"entry_failure_pct": "5", "exit_failure_pct": 12.5})
    assert errors == [] and s.entry_failure_pct == Decimal(5) and s.exit_failure_pct == Decimal("12.5")
    for bad in ({"entry_failure_pct": "60"}, {"exit_failure_pct": "-1"}, {"exit_failure_pct": True},
                {"use_measured_live_rates": "yes"}, {"surprise": 1}, {"entry_failure_pct": "NaN"}):
        assert paper_execution.parse_settings(bad)[1], bad


def test_draw_is_deterministic_and_roughly_uniform():
    assert paper_execution.draw("exit:a:0:0") == paper_execution.draw("exit:a:0:0")
    assert not paper_execution.simulated_failure("anything", Decimal(0))
    hits = sum(paper_execution.simulated_failure(f"k{i}", Decimal(20)) for i in range(5000))
    assert 900 < hits < 1100  # ~20%


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as s:
        yield s
    await engine.dispose()


def _order(i: int, side: str, status: str) -> ExecutionOrder:
    return ExecutionOrder(mode="LIVE", side=side, reason="t", mint="M" * 32, provider="pumpportal_local", route="pump", amount="1",
                          amount_kind="sol", slippage_pct=10, priority_fee_sol=0, status=status, idempotency_key=f"{side}{i}")


async def test_operator_rate_until_enough_live_orders_then_measured(session):
    session.add(PlatformSetting(key=paper_execution.SETTINGS_KEY,
                                value={"entry_failure_pct": "7", "exit_failure_pct": "3"}))
    for i in range(10):
        session.add(_order(i, "BUY", "FAILED" if i < 5 else "CONFIRMED"))
    await session.commit()
    r = await paper_execution.effective_rates(session)
    assert (r["entry_pct"], r["entry_source"]) == (Decimal(7), "operator setting")  # 10 orders: not enough
    for i in range(10, 25):
        session.add(_order(i, "BUY", "CONFIRMED" if i % 3 else "EXPIRED"))
    session.add(_order(99, "BUY", "CANCELLED"))  # never reached the chain: not counted
    await session.commit()
    r = await paper_execution.effective_rates(session)
    m = r["measured"]["BUY"]
    assert m["orders"] == 25 and m["failed"] == 5 + 5 and r["entry_pct"] == Decimal("40.00")
    assert r["entry_source"] == "measured from 25 live BUY orders"
    assert (r["exit_pct"], r["exit_source"]) == (Decimal(3), "operator setting")


async def test_historical_excursion_needs_enough_closed_trades(session):
    from yonixalpha_core.db.models import PaperAccount, PaperPosition
    from yonixalpha_core.safety.pipeline import historical_excursion

    acct = PaperAccount(name="h", quote_currency="SOL", starting_balance=10, cash_balance=10, reset_at=NOW)
    session.add(acct)
    await session.flush()

    def pos(i, high, engine="solana_fresh", status="closed"):
        return PaperPosition(symbol="T", provider="paper", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                             stop_loss=Decimal("0.9"), take_profit=[], status=status, entry_at=NOW, exit_at=NOW + timedelta(i),
                             account_id=acct.id, engine=engine, highest_price=Decimal(high))

    for i in range(29):
        session.add(pos(i, 1 + Decimal(i) / 100))  # excursions 0%..28%
    session.add(pos(99, 5, engine="solana_migration"))  # other engine: ignored
    session.add(pos(98, 5, status="open"))  # open: ignored
    await session.commit()
    assert await historical_excursion(session, "solana_fresh") == (None, 29)
    session.add(pos(29, "1.29"))
    await session.commit()
    p75, n = await historical_excursion(session, "solana_fresh")
    assert n == 30 and p75 == Decimal("0.22")  # sorted[22] of 0.00..0.29


async def test_retries_of_one_stuck_exit_count_once(session):
    """2026-10-07: 6 450 failed retries of one position's sell had pushed the
    measured exit failure rate to the 50 % cap. A position's sell counts once,
    decided by its first final attempt; its later retries are not trials."""
    from yonixalpha_core.db.models import PaperAccount, PaperPosition

    acct = PaperAccount(name="r", quote_currency="SOL", starting_balance=10, cash_balance=10, reset_at=NOW)
    session.add(acct)
    await session.flush()
    positions = [PaperPosition(symbol="T", provider="paper", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                               stop_loss=Decimal("0.9"), take_profit=[], status="closed", entry_at=NOW, exit_at=NOW,
                               account_id=acct.id, engine="solana_fresh") for _ in range(20)]
    session.add_all(positions)
    await session.flush()
    k = 0
    for i, p in enumerate(positions):
        first = "FAILED" if i == 0 else "CONFIRMED"
        o = _order(k, "SELL", first)
        o.position_id, o.created_at = p.id, NOW
        session.add(o)
        k += 1
    for j in range(300):  # the stuck position keeps failing, then one retry confirms
        o = _order(k, "SELL", "FAILED" if j < 299 else "CONFIRMED")
        o.position_id, o.created_at = positions[0].id, NOW + timedelta(seconds=j + 1)
        session.add(o)
        k += 1
    for j in range(5):  # orders without a position still count one by one
        session.add(_order(k, "SELL", "FAILED"))
        k += 1
    await session.commit()
    m = (await paper_execution.measured_live_rates(session))["SELL"]
    assert (m["orders"], m["failed"]) == (25, 6)
    assert m["failure_pct"] == Decimal("24.00") and m["usable"]
