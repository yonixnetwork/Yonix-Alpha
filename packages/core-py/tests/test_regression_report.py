"""Regression report (2026-10-10): closed trades split at a deploy marker,
by mode and stage; paper trades show which LIVE costs they were charged;
open positions are counted, never valued. Read-only."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PaperPosition  # noqa: E402
from yonixalpha_core.tools import regression_report as rr  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
SPLIT = NOW - timedelta(days=1)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def pos(created, pnl, mode="PAPER", engine="solana_fresh", status="closed", plan=None, exit_reason="stop_loss"):
    entry = Decimal("0.0000002")
    pct = Decimal(pnl) / Decimal("0.05")
    return PaperPosition(symbol="T", provider="paper", side="LONG", entry_price=entry, quantity=Decimal(1), take_profit=[],
                         status=status, entry_at=created, exit_at=created + timedelta(minutes=2) if status == "closed" else None,
                         engine=engine, asset_id="M", execution_mode=mode, entry_cost_quote=Decimal("0.05"),
                         fees_paid_quote=Decimal("0.001"), realized_pnl=Decimal(pnl) if status == "closed" else None,
                         realized_pnl_pct=pct if status == "closed" else None, exit_reason=exit_reason,
                         exit_price=entry * (1 + pct + Decimal("0.02")), highest_price=entry * Decimal("1.3"),
                         lowest_price=entry * Decimal("0.8"), plan=plan or {"venue": {"decimals": 6}})


async def test_split_by_marker_mode_stage_and_costs(sf):
    before, after = SPLIT - timedelta(hours=3), SPLIT + timedelta(hours=3)
    rows = [pos(before, "0.01", exit_reason="take_profit_1"), pos(before, "0.01", exit_reason="take_profit_1"),
            pos(before, "-0.005"),
            pos(after, "-0.006", plan={"venue": {"decimals": 6, "live_drift_pct": "2.5"}, "fixed_cost_quote": "0.0003"}),
            pos(after, "-0.006", plan={"venue": {"decimals": 6, "live_drift_pct": "2.5"}, "fixed_cost_quote": "0.0003"}),
            pos(after, "0.002", mode="LIVE", engine="solana_momentum"),
            pos(after, "0", status="open")]
    # LIVE: cost basis 2x the market fill (fees + rent on a tiny buy); marks are market prices
    rows[5].entry_price, rows[5].plan = rows[5].entry_price * 2, {"venue": {"decimals": 6},
                                                                  "fill": {"market_price": str(rows[5].entry_price)}}
    async with sf() as s:
        s.add_all(rows)
        await s.flush()
        for p in rows:  # created_at is server-set: place each row in its window
            await s.execute(update(PaperPosition).where(PaperPosition.id == p.id).values(created_at=p.entry_at))
        await s.commit()
        r = await rr.build(s, 7, [(SPLIT, "#X test change")], now=NOW)
    assert r["closed_trades"] == 6 and r["open_positions"] == {"PAPER": 1}
    b, a = r["by_window_stage"]["before #X | PAPER | FRESH"], r["by_window_stage"]["after #X | PAPER | FRESH"]
    assert b["trades"] == 3 and abs(b["net_sol"] - 0.015) < 1e-9 and abs(b["profit_factor"] - 4.0) < 1e-9
    assert a["trades"] == 2 and a["win_rate"] == 0 and a["profit_factor"] == 0 and abs(a["max_drawdown_sol"] - 0.012) < 1e-9
    live = r["by_window_stage"]["after #X | LIVE | MOMENTUM"]
    assert live["trades"] == 1 and abs(live["avg_mfe_pct"] - 0.3) < 1e-9  # vs the market fill, not the cost basis
    c = r["costs_by_window"]["after #X | PAPER"]
    assert c["share_charged_live_fixed_costs"] == 1 and c["share_charged_live_drift"] == 1
    assert abs(c["median_entry_drift_charged_pct"] - 0.025) < 1e-9 and abs(c["median_cost_drag_pct"] - 0.02) < 1e-9
    assert r["exit_reasons"]["before #X | PAPER | take_profit_1"]["trades"] == 2
    text = rr.render(r)
    assert "after #X | PAPER | FRESH" in text and "(small)" in text and "Nothing was written" in text
