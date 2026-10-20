"""Strategy recovery evidence tool (2026-10-14): read-only reconstruction of
winners and losers, per-category results, the migration pipeline and the
settings history. Runs inside a READ ONLY transaction."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    AuditLog,
    OpportunityOutcome,
    PaperAccount,
    PaperPosition,
    RiskAssessment,
    RiskSettingsVersion,
    Token,
    TradeTimelineEvent,
    TradingCandidate,
)
from yonixalpha_core.tools import strategy_recovery as sr  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
OCT6 = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def pos(acct, at, cost, back, engine="solana_migration", mint="MINT_A", mode="PAPER", plan=None, **kw):
    cost, back = Decimal(cost), Decimal(back)
    entry = Decimal("0.00000004")
    return PaperPosition(symbol="WIN", provider="paper", side="LONG", entry_price=entry, quantity=Decimal(1000),
                         initial_quantity=Decimal(1000), take_profit=[], status="closed", entry_at=at,
                         exit_at=at + timedelta(minutes=9), engine=engine, asset_id=mint, execution_mode=mode,
                         account_id=acct.id, entry_cost_quote=cost, proceeds_quote=back, fees_paid_quote=Decimal("0.0003"),
                         realized_pnl=back - cost, realized_pnl_pct=(back - cost) / cost, exit_reason=kw.pop("exit_reason", "stop_loss"),
                         exit_price=entry * back / cost, highest_price=entry * Decimal("12"), lowest_price=entry * Decimal("0.9"),
                         strategy="post_migration_flow", plan=plan or {"venue": {"decimals": 6}, "binding_cap": "max_position_size"},
                         **kw)


async def seed(s) -> PaperPosition:
    acct = PaperAccount(name="solana", quote_currency="SOL", starting_balance=Decimal(10), cash_balance=Decimal("9.5"))
    tok = Token(mint_address="MINT_A", symbol="WIN", first_seen_source="pump_stream")
    s.add_all([acct, tok])
    await s.flush()
    cand = TradingCandidate(token_id=tok.id, engine="migration", state="closed",
                            state_history=[{"state": "discovered", "at": OCT6.isoformat(), "reason": "pool found"}],
                            detail={"source": "migration_watch", "mint": "MINT_A"})
    s.add(cand)
    await s.flush()
    ra = RiskAssessment(idempotency_key="k1", candidate_id=cand.id, engine="solana_migration", strategy="post_migration_flow",
                        asset_id="MINT_A", decision="EXECUTE", status_label="EXECUTE", executable=True, execution_target="PAPER",
                        overall_risk="LOW", risk_engine_version="1", evaluated_at=OCT6 - timedelta(seconds=3),
                        assessment={"findings": [{"code": "HIGH_VOLATILITY", "action": "REDUCE_SIZE", "hard_block": False}],
                                    "reasons": ["buys > sells"], "versions": {"strategy": "post_migration_flow v1"},
                                    "settings_snapshot": {"risk_per_trade_pct": "0.01", "max_position_size_quote": "0.01"},
                                    "inputs_snapshot": {"features": {"h1_buys": 40, "h1_sells": 12, "pool_age_s": 300},
                                                        "pool": {"address": "POOL"}}})
    s.add(ra)
    await s.flush()
    winner = pos(acct, OCT6, "0.01", "0.11", candidate_id=cand.id, assessment_id=ra.id, exit_reason="take_profit_3",
                 tp_hits=[1, 2, 3])
    others = [pos(acct, OCT6 + timedelta(hours=1), "0.01", "0.008", mint="MINT_B"),
              pos(acct, datetime(2026, 10, 8, 9, tzinfo=timezone.utc), "0.01", "0.0085", mint="MINT_C"),
              pos(acct, datetime(2026, 10, 12, 0, 26, tzinfo=timezone.utc), "0.4694", "0.3726", mint="MINT_D",
                  plan={"venue": {"decimals": 6}, "binding_cap": "risk", "token_category": "MIGRATED"}),
              pos(acct, NOW - timedelta(hours=2), "0.025", "0.02", engine="solana_fresh", mint="MINT_E",
                  plan={"venue": {"decimals": 6}, "entry_delay": {"latency_s": 1.97, "measured": True, "price_change_pct": "1.5"}}),
              pos(acct, OCT6 + timedelta(hours=1, minutes=2), "0.005", "0.004", mode="LIVE", mint="MINT_B")]
    s.add_all([winner, *others])
    await s.flush()
    s.add_all([
        OpportunityOutcome(key="o1", mint="MINT_A", engine="solana_migration", stage="GATE", decision="EXECUTE", traded=True,
                           position_id=winner.id, candidate_id=cand.id, decided_at=OCT6 - timedelta(seconds=3),
                           snapshot={"migrated": True, "age_seconds": 300, "liquidity_sol": "85", "buyers": 40, "sellers": 12,
                                     "signal_reasons": ["h1 buys > sells", "pool age >= 120 s"]}, peak_pct=Decimal("1100")),
        TradeTimelineEvent(position_id=winner.id, candidate_id=cand.id, event_type="paper_exit.take_profit_1",
                           occurred_at=OCT6 + timedelta(minutes=2), detail={"returned": "0.004"}),
        RiskSettingsVersion(scope="GLOBAL", version=1, settings={"risk_per_trade_pct": "0.01", "max_daily_loss_quote": "0.5"}),
        RiskSettingsVersion(scope="solana_migration", version=1, settings={"__overrides__": {"max_position_size_quote": "0.01"}}),
        RiskSettingsVersion(scope="solana_migration", version=2, settings={"__overrides__": {"max_daily_loss_quote": "0.02"}}),
        AuditLog(event_type="risk_settings.updated_all", detail={"changes": {"max_position_size_quote": "2"}, "api_key": "x"}),
    ])
    await s.commit()
    return winner


async def test_report_reconstructs_the_winner_and_splits_migration_by_day(sf):
    async with sf() as s:
        winner = await seed(s)
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await sr.build(s, list(sr.SECTIONS), 10, 12, [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS], NOW)
        await s.rollback()
    out = "\n".join(lines)
    # the winner: +1000% on a 0.01 SOL entry, realized (proceeds - cost), reconstructed with what the gate saw
    assert f"### {str(winner.id)[:8]} [best by return % #1]" in out and "return 1000.00% = realized / entry cost" in out
    assert "post_migration_flow v1" in out and '"h1_buys":40' in out and "pool age >= 120 s" in out
    assert "AFTER entry (not known at decision): peak 1100" in out and "paper_exit.take_profit_1" in out
    assert "MIGRATED pool <10m" in out  # pool age at the decision, from the gate snapshot
    # migration with / without Oct 6: Oct 6 = +0.1 - 0.002, the rest lose
    assert "Oct 6 only" in out and "excluding Oct 6" in out
    excl = next(x for x in lines if "excluding Oct 6" in x)
    assert "net -0.0983" in excl, excl
    # settings history shows the per-engine cap and the audit detail without key-like fields
    assert '"max_position_size_quote":"0.01"' in out and '"max_daily_loss_quote":"0.02"' in out
    assert "updated_all" in out and "api_key" not in out
    # the risk engine's own view, computed inside the read-only transaction
    assert "risk engine right now" in out and "risk summary unavailable" not in out, out[-3000:]
    assert "LIVE" in out and "same token traded PAPER and LIVE within 10 minutes: 1" in out
    assert "hours with no candidate at all:" in out
    assert "Nothing was written." in out


async def test_curve_entries_keep_their_category_and_urls_are_hidden(sf):
    async with sf() as s:
        await seed(s)
        p = (await s.execute(select(PaperPosition).where(PaperPosition.engine == "solana_fresh"))).scalar_one()
        p.plan = {**p.plan, "venue": {"decimals": 6, "type": "pump_curve"}}
        p.lifecycle = "MIGRATED"  # the token migrated while the position was held
        ra = (await s.execute(select(RiskAssessment))).scalar_one()
        ra.assessment = {**ra.assessment, "reasons": ["RPC error from https://rpc.example/abcdef0123456789 (403)"]}
        await s.commit()
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await sr.build(s, ["trades", "deep"], 10, 12, [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS], NOW)
        await s.rollback()
    out = "\n".join(lines)
    assert "FRESH -> migrated while held" in out
    assert "abcdef0123456789" not in out and "<url>" in out


async def test_pick_and_established_migrated_tokens(sf):
    async with sf() as s:
        winner = await seed(s)
        o = (await s.execute(select(OpportunityOutcome))).scalar_one()
        o.snapshot = {**o.snapshot, "market_cap_sol": "93350"}  # far above a completed curve (~410 SOL)
        await s.commit()
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await sr.build(s, ["deep", "trades"], 10, 12, [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS], NOW,
                               pick=[str(winner.id)[:8]])
        await s.rollback()
    out = "\n".join(lines)
    assert out.count("### ") == 1 and f"### {str(winner.id)[:8]} [picked]" in out
    assert "MIGRATED established (mcap >3k SOL)" in out


async def test_the_tool_cannot_write(sf):
    async with sf() as s:
        await seed(s)
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(Exception, match="read-only"):
            await s.execute(text("UPDATE paper_accounts SET cash_balance = 0"))
        await s.rollback()
    async with sf() as s:
        assert (await s.execute(select(func.count()).select_from(PaperPosition))).scalar_one() == 6
        assert (await s.execute(select(PaperAccount.cash_balance))).scalar_one() == Decimal("9.5")


async def test_equity_curve_peak_drawdown_and_without_the_best_trade(sf):
    async with sf() as s:
        await seed(s)
        acct = (await s.execute(select(PaperAccount))).scalar_one()
        acct.reset_at = datetime(2026, 9, 26, tzinfo=timezone.utc)  # trades before a reset belong to an older book
        await s.commit()
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await sr.build(s, ["equity"], 10, 12, [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS], NOW)
        await s.rollback()
    out = "\n".join(lines)
    # PAPER exits in order: +0.1, -0.002, -0.0015, -0.0968, -0.005 from 10 SOL (the LIVE trade is not counted)
    assert "closed trades 5" in out, out
    assert "end 9.9947 (-0.0053" in out and "peak 10.1000" in out, out
    # largest fall from the 10.1 peak: 10.1 -> 9.9947
    assert "max drawdown 0.1053 (1.04% of the peak before it)" in out, out
    # without the +0.1 winner the account only loses
    assert "without the single best trade" in out and "end 9.8947 (-0.1053)" in out, out
    assert "2026-10-06  n=2" in out and "open PAPER positions on this account: 0" in out
    # the last marker is #74 (Oct 11): only the Oct 12 and the recent fresh trade closed after it
    assert "every trade closed after the last marker (#74 migration pool detection, 10-11 08:09:00): 2" in out, out
    assert "MIGRATED" in out and "stop_loss" in out
    assert "Nothing was written." in out


def test_curve_math():
    c = sr._curve(Decimal(10), [Decimal(1), Decimal(-3), Decimal(1)])
    assert c == {"end": Decimal(9), "peak": Decimal(11), "trough": Decimal(8), "dd": Decimal(3),
                 "dd_pct": Decimal(3) / Decimal(11)}


async def test_stop_realism_compares_the_planned_stop_with_the_realized_loss(sf):
    async with sf() as s:
        await seed(s)
        for p in (await s.execute(select(PaperPosition))).scalars():
            p.plan = {**(p.plan or {}), "stop_distance_pct": "0.10"}
            if p.engine == "solana_fresh":  # a curve entry whose token migrated while held, planned to lose 0.0025
                p.plan = {**p.plan, "venue": {"decimals": 6, "type": "pump_curve"}}
                p.lifecycle, p.max_loss_quote = "MIGRATED", Decimal("0.0025")
        await s.commit()
    async with sf() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        lines = await sr.build(s, ["stops"], 10, 12, [(datetime.fromisoformat(a), n) for a, n in sr.DEPLOYS], NOW)
        await s.rollback()
    out = "\n".join(lines)
    # PAPER stop exits: -20%, -15%, -20.6% (migration) and -20% (fresh); the planned stop is 10%
    row = next(x for x in lines if x.strip().startswith("PAPER MIGRATED stop_loss"))
    assert row.split()[3] == "3" and "10.0%" in row, row
    # the migrated-while-held curve entry is its own group; it lost 0.005, twice its planned max loss
    fresh = next(x for x in lines if x.strip().startswith("PAPER FRESH ->migrated stop_loss"))
    assert fresh.split()[4] == "1" and fresh.rstrip().endswith("2.00x / 2.00x"), fresh
    assert "worst 5 stop exits by realized loss" in out
    worst = [x for x in lines if x.startswith("    ")]
    assert len(worst) == 5 and "-20.6%" in worst[0] and "P4" in worst[0], worst  # MINT_D, entered Oct 12 (after #74)
    assert any("FRESH ->migrated" in x and "pump_curve" in x and "2.00x" in x for x in worst), worst
    assert "LIVE" in out and "Nothing was written." in out
    assert sr._pct([0.1, 0.2, 0.3, 0.4], 0.9) == 0.4 and sr._pct([], 0.5) is None
