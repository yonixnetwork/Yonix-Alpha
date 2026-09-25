"""Database-backed tests for yonixalpha_core.safety.store (needs the local
test Postgres, same as the services' DB tests)."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import AuditLog, PaperPosition, RiskSettingsVersion, TradeTimelineEvent  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import GlobalMode, StrategyMode  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings, default_settings_for  # noqa: E402

from tests.test_safety_gate import NOW, healthy  # noqa: E402


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def test_live_permission_needs_all_three_env_locks():
    ok = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False)
    assert store.live_trading_permitted(ok)
    for flip in ({"TRADING_ENABLED": False}, {"LIVE_TRADING_ENABLED": False}, {"PAPER_TRADING": True}):
        assert not store.live_trading_permitted(SimpleNamespace(**{**vars(ok), **flip}))
    assert not store.live_trading_permitted(SimpleNamespace())


async def test_settings_default_then_versioned_and_audited(db):
    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings == default_settings_for("solana_fresh") and meta["scope"] == "DEFAULT"
    assert settings.min_stop_pct == Decimal("0.10")  # pump.fun costs push the stop floor up
    generic, _ = await store.load_settings(db, "some_other_engine")
    assert generic == SafetySettings()
    futures, _ = await store.load_settings(db, "binance_futures")
    assert futures.min_liquidity_quote == Decimal("50000")

    row, notes = await store.save_settings(db, "GLOBAL", {"max_position_size_quote": "0.5"}, None, "tighter")
    assert row.version == 1 and notes == []
    row2, _ = await store.save_settings(db, "GLOBAL", {"max_position_size_quote": "0.4"}, None)
    assert row2.version == 2
    await db.commit()

    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings.max_position_size_quote == Decimal("0.4") and meta == {**meta, "scope": "GLOBAL", "version": 2}

    # An engine-scoped row takes precedence over GLOBAL.
    await store.save_settings(db, "solana_fresh", {"max_position_size_quote": "0.2"}, None)
    await db.commit()
    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings.max_position_size_quote == Decimal("0.2") and meta["scope"] == "solana_fresh"

    audits = (await db.execute(select(AuditLog).where(AuditLog.event_type == "risk_settings.updated"))).scalars().all()
    assert len(audits) == 3
    assert audits[1].detail["changed"] == {"max_position_size_quote": {"from": "0.5", "to": "0.4"}}


async def test_invalid_settings_are_refused_and_nothing_written(db):
    with pytest.raises(store.SettingsError) as err:
        await store.save_settings(db, "GLOBAL", {"min_stop_pct": "0.5", "max_stop_pct": "0.1"}, None)
    assert "min_stop_pct must be below max_stop_pct" in err.value.errors
    assert (await db.execute(select(RiskSettingsVersion))).scalars().all() == []


async def test_values_beyond_hard_limits_are_clamped_with_notes(db):
    row, notes = await store.save_settings(db, "GLOBAL", {"risk_per_trade_pct": "0.9"}, None)
    assert notes and "clamped" in notes[0]
    assert Decimal(row.settings["risk_per_trade_pct"]) < Decimal("0.9")


async def test_stored_row_that_no_longer_validates_falls_back_to_defaults(db):
    db.add(RiskSettingsVersion(scope="GLOBAL", version=1, settings={"min_stop_pct": "0.5", "max_stop_pct": "0.1"}))
    await db.commit()
    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings == default_settings_for("solana_fresh") and meta["scope"] == "DEFAULT" and meta["errors"]


async def test_modes_default_to_paper_and_changes_are_audited(db):
    assert await store.load_global_mode(db) == GlobalMode.PAPER
    assert await store.load_strategy_mode(db, "solana_fresh") == StrategyMode.PAPER
    await store.set_global_mode(db, GlobalMode.MANUAL, None)
    await store.set_strategy_mode(db, "solana_fresh", StrategyMode.OFF, None)
    await store.set_strategy_mode(db, "solana_fresh", StrategyMode.MANUAL, None)
    await db.commit()
    assert await store.load_global_mode(db) == GlobalMode.MANUAL
    assert await store.load_strategy_mode(db, "solana_fresh") == StrategyMode.MANUAL
    events = [a.event_type for a in (await db.execute(select(AuditLog))).scalars().all()]
    assert events.count("strategy_mode.changed") == 2 and events.count("global_mode.changed") == 1


async def test_account_state_marks_open_positions_and_counts_todays_losses(db):
    account = await store.get_paper_account(db, "solana")
    again = await store.get_paper_account(db, "solana")
    assert account.id == again.id and account.cash_balance == Decimal("10")

    account.cash_balance = Decimal("9")
    db.add(PaperPosition(symbol="AAA", provider="paper", side="LONG", entry_price=Decimal("0.001"), quantity=Decimal("1000"),
                         remaining_quantity=Decimal("1000"), last_price=Decimal("0.0005"), status="open", entry_at=NOW,
                         account_id=account.id, asset_id="mintA", take_profit=[]))
    db.add(PaperPosition(symbol="BBB", provider="paper", side="LONG", entry_price=Decimal("1"), quantity=Decimal("1"),
                         status="closed", realized_pnl=Decimal("-0.2"), exit_at=NOW - timedelta(hours=1), entry_at=NOW,
                         account_id=account.id, take_profit=[]))
    db.add(PaperPosition(symbol="CCC", provider="paper", side="LONG", entry_price=Decimal("1"), quantity=Decimal("1"),
                         status="closed", realized_pnl=Decimal("-5"), exit_at=NOW - timedelta(days=2), entry_at=NOW,
                         account_id=account.id, take_profit=[]))
    await db.commit()

    state = await store.account_state(db, account, "mintA", NOW, kill_switch_engaged=False)
    assert state.current_exposure == Decimal("0.5")
    assert state.equity == Decimal("9.5")
    assert state.available_balance == Decimal("9")
    assert state.open_positions == 1
    assert state.token_exposure == Decimal("0.5")
    assert state.daily_realized_pnl == Decimal("-0.2")  # the 2-day-old loss is excluded
    assert state.last_loss_at == NOW - timedelta(hours=1)

    other = await store.account_state(db, account, "mintZ", NOW, kill_switch_engaged=True)
    assert other.token_exposure == 0 and other.kill_switch_engaged


async def test_assessment_persistence_is_idempotent_and_writes_timeline(db):
    a = assess(healthy(), SafetySettings())
    key = store.assessment_key(a.engine, a.asset_id, NOW.isoformat())
    row, created = await store.persist_assessment(db, a, None, key)
    await db.commit()
    assert created and row.decision == a.decision.value and row.assessment["plan"]
    row2, created2 = await store.persist_assessment(db, a, None, key)
    await db.commit()
    assert not created2 and row2.id == row.id
    timeline = (await db.execute(select(TradeTimelineEvent))).scalars().all()
    assert len(timeline) == 1 and timeline[0].assessment_id == row.id


async def test_approval_state_pending_only_for_manual_approval(db):
    a = assess(healthy(global_mode=GlobalMode.MANUAL), SafetySettings())
    assert a.decision.value == "REQUIRE_MANUAL_APPROVAL"
    row, _ = await store.persist_assessment(db, a, None, "k1")
    assert row.approval_state == "PENDING"
    b = assess(healthy(), SafetySettings())
    row_b, _ = await store.persist_assessment(db, b, None, "k2")
    assert row_b.approval_state == "NONE"


async def test_assessment_with_decimal_inputs_snapshot_persists(db):
    a = assess(healthy(), SafetySettings())
    a.inputs_snapshot = {"price": Decimal("0.00000003"), "at": datetime.now(timezone.utc)}
    row, _ = await store.persist_assessment(db, a, None, "k3")
    await db.commit()
    assert row.assessment["inputs_snapshot"]["price"] == "3E-8"
