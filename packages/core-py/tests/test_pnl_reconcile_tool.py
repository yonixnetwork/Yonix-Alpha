"""PnL reconciliation: token-account rent is a recoverable deposit (locked
until the empty account is closed), not a consumed cost; MFE is reported
against the market fill price as well as the cost basis."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PaperAccount, PaperPosition  # noqa: E402
from yonixalpha_core.tools.pnl_reconcile import breakdown, report  # noqa: E402

T = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
COSTS = {"trade_lamports": "0.004", "program_fees_lamports": "0.00005", "network_fee_lamports": "0.000105",
         "token_account_rent_lamports": "0.00203928"}


def pos(status="closed", reclaimed=None, realized="-0.0021", high="0.0000000041", market="0.000000004"):
    fill = {"costs_sol": COSTS, "market_price": market, "market_price_basis": "trade price"}
    if reclaimed is not None:
        fill["rent_reclaimed_sol"] = reclaimed
    # 0.0062 SOL paid for 1,000,000 tokens: cost basis 6.2e-9, market 4e-9
    return SimpleNamespace(plan={"fill": fill}, status=status, realized_pnl=Decimal(realized), execution_mode="LIVE",
                           entry_price=Decimal("0.0000000062"), highest_price=Decimal(high), lowest_price=Decimal("0.0000000038"),
                           entry_cost_quote=Decimal("0.0062"), entry_at=T)


def test_unreclaimed_rent_is_locked_not_consumed():
    b = breakdown(pos())
    assert b["consumed"] == Decimal("0.000155")
    assert b["deposit"] == Decimal("0.00203928") and b["locked"] == b["deposit"] and b["reclaimed"] is None
    assert b["economic"] == Decimal("-0.0021") + Decimal("0.00203928")  # the deposit comes back when the account closes


def test_reclaimed_rent_is_already_in_the_booked_result():
    b = breakdown(pos(reclaimed="0.00203428", realized="-0.00006572"))
    assert b["locked"] == 0 and b["economic"] == b["booked"] == Decimal("-0.00006572")


def test_open_position_has_no_locked_amount_yet():
    b = breakdown(pos(status="open"))
    assert b["locked"] is None and b["economic"] == b["booked"]


def test_mfe_against_market_price_and_cost_basis():
    b = breakdown(pos())
    assert round(b["mfe_market"], 4) == Decimal("0.025")  # the price did go up 2.5 % after the fill
    assert b["mfe_cost_basis"] < Decimal("-0.3")  # against everything paid, incl. the deposit, it "never went up"
    assert b["mfe_measured"] is True


def test_missing_breakdown_is_reported_as_unknown():
    p = pos()
    p.plan = {}
    b = breakdown(p)
    assert b["consumed"] is None and b["deposit"] is None and b["locked"] is None and b["economic"] == b["booked"]


@pytest.mark.asyncio
async def test_the_tool_runs_read_only_on_the_database_and_checks_paper_books(capsys):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as s:
        acct = PaperAccount(name="solana", quote_currency="SOL", starting_balance=Decimal(10), cash_balance=Decimal("10.01"),
                            reset_at=T - timedelta(days=1))
        s.add(acct)
        await s.flush()
        s.add(PaperPosition(symbol="W", provider="paper", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                            initial_quantity=Decimal(1), take_profit=[], status="closed", entry_at=T, exit_at=T,
                            engine="solana_momentum", asset_id="MINT", execution_mode="PAPER", account_id=acct.id,
                            entry_cost_quote=Decimal("0.02"), proceeds_quote=Decimal("0.03"), realized_pnl=Decimal("0.01"),
                            fees_paid_quote=Decimal("0.0002")))
        await s.commit()
    async with sf() as s:
        await report(s, "solana_momentum", 10)
    await engine.dispose()
    out = capsys.readouterr().out
    assert "0 LIVE, 1 PAPER" in out and "ESTIMATES" in out
    assert "difference +0.000000000" in out and "Nothing was written" in out
