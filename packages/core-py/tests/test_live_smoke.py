"""live_smoke: arming refusals, and stages derived only from real order /
position rows (submitted != confirmed != filled), live PnL and STALE."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import live_smoke  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import ExecutionOrder, LiveSmokeTest, PaperPosition  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
ON = dict(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, LIVE_SMOKE_TEST_ENABLED=True,
          LIVE_SMOKE_TEST_MAX_SOL=Decimal("0.02"), LIVE_SMOKE_TEST_MAX_TRADES=1)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def env(**kw):
    return SimpleNamespace(**{**ON, **kw})


def test_defaults_are_off_and_unarmable():
    from yonixalpha_core.config import Settings

    s = Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://x/y", REDIS_URL="redis://x", JWT_SECRET="x" * 40)
    assert s.LIVE_SMOKE_TEST_ENABLED is False and s.LIVE_SMOKE_TEST_MAX_SOL is None
    problems = live_smoke.config_status(s)["problems"]
    assert any("ENABLED is false" in p for p in problems) and any("MAX_SOL is not set" in p for p in problems)


@pytest.mark.parametrize("kw,needle", [
    ({"LIVE_SMOKE_TEST_ENABLED": False}, "ENABLED is false"),
    ({"LIVE_SMOKE_TEST_MAX_SOL": None}, "MAX_SOL is not set"),
    ({"PAPER_TRADING": True}, "environment locks closed"),
])
async def test_arm_refuses_without_every_server_switch(db, kw, needle):
    with pytest.raises(live_smoke.SmokeRefused) as e:
        await live_smoke.arm(db, None, env(**kw), "FRESH", None, 30, "admin", NOW)
    assert needle in e.value.reason


async def test_arm_refuses_above_the_server_ceiling_and_when_not_ready(db):
    with pytest.raises(live_smoke.SmokeRefused) as e:
        await live_smoke.arm(db, None, env(), "FRESH", Decimal("0.05"), 30, "admin", NOW)
    assert "at most LIVE_SMOKE_TEST_MAX_SOL" in e.value.reason
    with pytest.raises(live_smoke.SmokeRefused) as e:
        await live_smoke.arm(db, None, env(), "FRESH", Decimal("0.01"), 30, "admin", NOW)
    assert e.value.stage == "EXECUTION_ROUTE_UNAVAILABLE"  # no Redis -> no ready worker


def _position(**kw) -> PaperPosition:
    base = dict(symbol="T", provider="live", side="LONG", entry_price=Decimal("0.00000004"), quantity=Decimal(500000),
                initial_quantity=Decimal(500000), remaining_quantity=Decimal(500000), entry_cost_quote=Decimal("0.02"),
                entry_at=NOW, status="open", engine="solana_fresh", asset_id="M" * 44, execution_mode="LIVE",
                execution_route="pump", execution_provider="pumpportal_local", lifecycle="FRESH",
                last_price=Decimal("0.000000044"), last_marked_at=NOW - timedelta(seconds=5), stop_loss=Decimal("0.00000003"),
                take_profit=["0.00000006"], plan={"entry_price": "0.00000004", "venue": {"type": "pump_curve", "decimals": 6}})
    return PaperPosition(**{**base, **kw})


def _order(position_id, side="BUY", status="CONFIRMED", fill=True, **kw) -> ExecutionOrder:
    f = {"sol_change_lamports": -20_100_000, "token_change_raw": 500_000_000_000, "fee_lamports": 5000, "slot": 1,
         "token_decimals": 6, "block_time": 1} if side == "BUY" else {
        "sol_change_lamports": 21_000_000, "token_change_raw": -500_000_000_000, "fee_lamports": 5000, "slot": 2,
        "token_decimals": 6, "block_time": 2}
    return ExecutionOrder(position_id=position_id, mode="LIVE", side=side, reason="entry" if side == "BUY" else "take_profit_1",
                          mint="M" * 44, provider="pumpportal_local", route="pump", amount="0.02", amount_kind="sol",
                          slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"), status=status,
                          idempotency_key=f"{side}:{status}:{uuid.uuid4()}", signature=kw.get("signature", "sig"),
                          submitted_at=NOW, confirmed_at=NOW if status == "CONFIRMED" else None, error=kw.get("error"),
                          result={"sent": kw.get("sent", True), "fill": f if fill else None})


async def _run_with(db, pos: PaperPosition, *orders) -> LiveSmokeTest:
    db.add(pos)
    await db.flush()
    for o in orders:
        db.add(o(pos.id))
    run = LiveSmokeTest(category="FRESH", max_sol=Decimal("0.02"), status="USED", stage="BUY_REQUESTED", armed_by="a",
                        expires_at=NOW, attempts=[], position_id=pos.id, mint=pos.asset_id, engine="solana_fresh")
    db.add(run)
    await db.commit()
    return run


async def test_filled_buy_and_open_position_show_real_numbers_and_pnl(db):
    run = await _run_with(db, _position(), lambda pid: _order(pid))
    v = await live_smoke.run_view(db, run, NOW)
    b = v["buy"]
    assert b["order_submitted"] and b["transaction_confirmed"] and b["actually_filled"]
    assert b["actual"]["tokens"] == "500000" and Decimal(b["actual"]["sol"]) == Decimal("0.0201")
    assert v["stage"] == "POSITION_OPEN"
    p = v["position"]
    assert p["price_status"] == "LIVE" and p["unrealized_pnl"] == Decimal("0.002") and p["unrealized_pnl_pct"] == Decimal("10.00")


async def test_stale_price_is_shown_as_stale(db):
    run = await _run_with(db, _position(last_marked_at=NOW - timedelta(minutes=5)), lambda pid: _order(pid))
    v = await live_smoke.run_view(db, run, NOW)
    assert v["position"]["price_status"] == "STALE" and v["stage"] == "POSITION_OPEN (MARKET_DATA_UNAVAILABLE)"


async def test_confirmed_without_tokens_is_not_a_fill(db):
    run = await _run_with(db, _position(status="needs_review"), lambda pid: _order(pid, fill=False))
    v = await live_smoke.run_view(db, run, NOW)
    assert v["buy"]["transaction_confirmed"] and not v["buy"]["actually_filled"] and v["stage"] == "FILL_UNVERIFIED"


@pytest.mark.parametrize("error,status,signature,sent,stage", [
    ("pumpportal: 400 bad request", "FAILED", None, False, "QUOTE_FAILED"),
    ("transaction guard refused to sign: fee too high", "FAILED", None, False, "TRANSACTION_REJECTED"),
    ("simulation failed: {'InstructionError': [3, 6001]}", "FAILED", "s", False, "TRANSACTION_REJECTED"),
    ("transaction failed on chain: slippage", "FAILED", "s", True, "TRANSACTION_REJECTED"),
    ("not confirmed within 90s; blockhash expired", "EXPIRED", "s", True, "TRANSACTION_UNCONFIRMED"),
    ("request is not for the configured wallet", "FAILED", None, False, "SIGNING_FAILED"),
    ("confirmed but fill unreadable: x", "FAILED", "s", True, "FILL_UNVERIFIED"),
])
async def test_every_buy_failure_names_its_stage(db, error, status, signature, sent, stage):
    run = await _run_with(db, _position(status="failed", quantity=Decimal(0)),
                          lambda pid: _order(pid, status=status, fill=False, error=error, signature=signature, sent=sent))
    v = await live_smoke.run_view(db, run, NOW)
    assert v["buy"]["failure_stage"] == stage and v["stage"] == stage
    assert not v["buy"]["actually_filled"]


async def test_sell_confirmed_closes_and_failed_sell_is_named(db):
    run = await _run_with(db, _position(status="closed", remaining_quantity=Decimal(0)),
                          lambda pid: _order(pid), lambda pid: _order(pid, side="SELL"))
    v = await live_smoke.run_view(db, run, NOW)
    assert v["sells"][0]["actually_filled"] and v["stage"] == "POSITION_CLOSED"
    run2 = await _run_with(db, _position(asset_id="N" * 44), lambda pid: _order(pid),
                           lambda pid: _order(pid, side="SELL", status="FAILED", fill=False, error="simulation failed: x"))
    v2 = await live_smoke.run_view(db, run2, NOW)
    assert v2["stage"] == "SELL_FAILED: TRANSACTION_REJECTED"
