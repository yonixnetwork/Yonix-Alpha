"""Momentum research tool and the momentum panel data: decision-time
features only, chronological replay, read-only."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import momentum_insight  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import OpportunityOutcome, PaperAccount, PaperPosition  # noqa: E402
from yonixalpha_core.tools import momentum_research as mr  # noqa: E402
from yonixalpha_core.tools import strategy_recovery as sr  # noqa: E402

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MARKS = [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS]


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def intel(r5: float, buyer_acc: float, flow: str = "EXPANSION", diversity: float = 0.7) -> dict:
    return {"momentum": {"return_1m": 0.03, "return_5m": r5, "volume_acceleration": 2.0, "buyer_acceleration": buyer_acc,
                         "seller_acceleration": 1.0, "new_wallet_acceleration": 1.4, "trades_1m": 25},
            "flow_state": {"state": flow, "sell_share": 0.3},
            "buyer_breadth": {"score": 0.6, "unique_buyers": 14, "components": {"diversity": diversity}},
            "now": {"price": 1.0, "high": 1.2}}


def snap(r5: float, buyer_acc: float, qualified: bool = True, **kw) -> dict:
    return {"price_raw": "0.00000003", "signal_qualified": qualified, "curve_progress": "0.5", "liquidity_sol": "20",
            "creator_sold": False, "intel": intel(r5, buyer_acc, **kw)}


async def seed(s) -> None:
    acct = PaperAccount(name="solana", quote_currency="SOL", starting_balance=Decimal(10), cash_balance=Decimal(10))
    s.add(acct)
    await s.flush()
    entry = Decimal("0.00000003")
    # winner: modest 5 m run-up; loser: late (5 m +90%) with buyers fading
    for i, (r5, ba, back) in enumerate(((0.2, 1.8, "0.013"), (0.9, 0.6, "0.007"))):
        at = T0 + timedelta(hours=i)
        p = PaperPosition(symbol=f"M{i}", provider="paper", side="LONG", entry_price=entry, quantity=Decimal(1000),
                          initial_quantity=Decimal(1000), take_profit=[], status="closed", entry_at=at,
                          exit_at=at + timedelta(minutes=4), engine="solana_momentum", asset_id=f"MINT{i}",
                          execution_mode="PAPER", account_id=acct.id, entry_cost_quote=Decimal("0.01"),
                          proceeds_quote=Decimal(back), fees_paid_quote=Decimal("0.0002"),
                          realized_pnl=Decimal(back) - Decimal("0.01"), realized_pnl_pct=(Decimal(back) - Decimal("0.01")) / Decimal("0.01"),
                          exit_reason="take_profit_1" if i == 0 else "stop_loss", exit_price=entry,
                          highest_price=entry * Decimal("1.4"), lowest_price=entry * Decimal("0.7"),
                          strategy="solana_momentum", plan={"venue": {"decimals": 6}})
        s.add(p)
        await s.flush()
        s.add(OpportunityOutcome(key=f"gate:{i}", mint=f"MINT{i}", engine="solana_momentum", stage="GATE",
                                 decision="EXECUTE", traded=True, position_id=p.id, decided_at=at, snapshot=snap(r5, ba),
                                 status="COMPLETE", loss_analysis={"classification": "MARKET_REVERSAL"} if i else None,
                                 post_exit={"classification": "GOOD_EXIT"},
                                 path={"T+5m": {"executable": {"executable_return_pct": 20.0 if i == 0 else -30.0}}}))
    # 20 more completed decisions for the replay: modest run-ups win, late ones lose
    for j in range(20):
        late = j % 2 == 1
        s.add(OpportunityOutcome(key=f"w{j}", mint=f"W{j}", engine="solana_momentum", stage="WATCH",
                                 decision="QUALIFIED", traded=False, decided_at=T0 + timedelta(hours=2, minutes=10 * j),
                                 snapshot=snap(0.8 if late else 0.15, 1.5), status="COMPLETE",
                                 path={h: {"executable": {"executable_return_pct": -25.0 if late else 12.0}}
                                       for h in mr.HORIZONS}))
    await s.commit()


async def run(sf, sections):
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await mr.build(s, sections, MARKS, NOW)
        await s.rollback()
    return lines


async def test_trades_show_decision_time_features_and_the_v2_checks(sf):
    async with sf() as s:
        await seed(s)
    lines = await run(sf, ["trades", "compare", "exits"])
    out = "\n".join(lines)
    assert "closed momentum positions: 2 (PAPER 2)" in out
    rows = [x for x in lines if x.startswith("  ") and " PAPER " in x and "|" in x]
    assert len(rows) == 2
    win, lose = rows
    # v2 checks in order exh dec brd dmp liq qw: the late, fading loser fails exhaustion and decay
    assert win.split("|")[2].split() == ["P", "P", "P", "P", "P", "?"], win
    assert lose.split("|")[2].split()[:2] == ["F", "F"], lose
    assert "MARKET_REVERSAL / GOOD_EXIT" in lose and "+90%" in lose
    assert "winners 1, losers 1" in out and "return_5m" in out
    assert "exhaustion" in out and "losers FAIL     1/1" in out
    assert "PAPER stop_loss" in out and "GOOD_EXIT 2" in out
    assert "Nothing was written." in out


async def test_replay_compares_v1_with_each_check_out_of_sample(sf):
    async with sf() as s:
        await seed(s)
    out = "\n".join(await run(sf, ["replay"]))
    assert "rows 22 (GATE 2, WATCH 20)" in out and "out of sample (last 40%)" in out
    base = [x for x in out.splitlines() if "v1 baseline" in x and "n=" in x and "horizon" not in x]
    exh = [x for x in out.splitlines() if "v1 + exhaustion " in x]
    assert base and exh
    # with the 50% cap the late (losing) decisions are filtered: a higher mean than the baseline
    mean = lambda line: float(line.split("mean")[1].split("%")[0])  # noqa: E731
    assert mean(exh[0]) > mean(base[0]), (base[0], exh[0])
    assert "threshold sweeps" in out and "chosen mv2_max_return_5m=" in out


async def test_momentum_panel_groups_by_lifecycle_and_entry_type(sf):
    async with sf() as s:
        await seed(s)
    async with sf() as s:
        r = await momentum_insight.insight(s, None, NOW)
    paper = r["results"][0]
    assert paper["mode"] == "PAPER" and paper["trades"] == 2 and paper["sample"] == "too small to judge"
    assert abs(paper["net_sol"] - 0.0) < 1e-9 and paper["net_without_best_trade_sol"] < 0
    assert {g["group"] for g in r["by_lifecycle"]} == {"CURVE"}
    assert {g["group"] for g in r["by_entry_type"]} == {"CONTINUATION"}
    assert r["recent"] and r["recent"][0]["stage"] in ("GATE", "WATCH")
