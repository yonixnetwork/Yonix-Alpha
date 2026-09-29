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
from yonixalpha_core.safety.settings import SafetySettings, default_settings_for, settings_from_dict, settings_to_dict  # noqa: E402

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

    # An engine-scoped value takes precedence over GLOBAL for that key only.
    await store.save_settings(db, "solana_fresh", {"max_position_size_quote": "0.2"}, None)
    await db.commit()
    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings.max_position_size_quote == Decimal("0.2") and meta["scope"] == "solana_fresh"
    assert meta["overrides"] == ["max_position_size_quote"]

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


async def test_engine_scope_keeps_following_global_for_keys_it_does_not_override(db):
    await store.save_settings(db, "GLOBAL", {"min_liquidity_quote": "100"}, None)
    g = settings_to_dict(settings_from_dict({"min_liquidity_quote": "100"}))
    row, _ = await store.save_settings(db, "solana_fresh", {**g, "skip_duplicate_names": False}, None)
    assert row.settings == {store.OVERRIDES: {"skip_duplicate_names": False}}
    await store.save_settings(db, "GLOBAL", {**g, "min_liquidity_quote": "250"}, None)
    await db.commit()
    s, meta = await store.load_settings(db, "solana_fresh")
    assert s.min_liquidity_quote == Decimal("250") and s.skip_duplicate_names is False
    assert meta["global_version"] == 2 and (await store.engine_overrides(db, "solana_fresh"))["keys"] == ["skip_duplicate_names"]

    await store.follow_global(db, "solana_fresh", None)
    await db.commit()
    s, meta = await store.load_settings(db, "solana_fresh")
    assert s.skip_duplicate_names is True and meta["scope"] == "GLOBAL" and meta["version"] == 2
    assert (await store.engine_overrides(db, "solana_fresh"))["mode"] == "follows_global"


async def test_legacy_full_copy_still_replaces_global(db):
    await store.save_settings(db, "GLOBAL", {"min_liquidity_quote": "100"}, None)
    db.add(RiskSettingsVersion(scope="solana_fresh", version=1, settings=settings_to_dict(
        settings_from_dict({"min_liquidity_quote": "5", "skip_duplicate_names": False}))))
    await db.commit()
    s, meta = await store.load_settings(db, "solana_fresh")
    assert s.min_liquidity_quote == Decimal("5") and meta["legacy_full_copy"] is True
    assert (await store.engine_overrides(db, "solana_fresh"))["mode"] == "legacy_full_copy"


async def test_save_for_all_engines_reaches_an_engine_that_had_its_own_value(db):
    """The reported case: duplicates allowed from the dashboard, yet momentum
    still rejected them because it kept its own stored value."""
    g = settings_to_dict(settings_from_dict({}))
    await store.save_settings(db, "GLOBAL", g, None)
    await store.save_settings(db, "solana_momentum", {**g, "skip_duplicate_names": True, "min_name_length": 5}, None)
    db.add(RiskSettingsVersion(scope="solana_migration", version=1, settings={**g, "skip_duplicate_names": True}))  # legacy copy
    await store.save_settings(db, "GLOBAL", {**g, "skip_duplicate_names": True}, None)
    await db.commit()
    assert (await store.engine_overrides(db, "solana_momentum"))["values"] == {"min_name_length": 5}  # equal to GLOBAL now

    row, _, touched = await store.save_for_all_engines(db, {"skip_duplicate_names": False}, None,
                                                       ["solana_fresh", "solana_migration", "solana_momentum"], "allow duplicates")
    await db.commit()
    for engine in ("solana_fresh", "solana_migration", "solana_momentum", "binance_futures"):
        s, _ = await store.load_settings(db, engine)
        assert s.skip_duplicate_names is False, engine
    s, _ = await store.load_settings(db, "solana_momentum")
    assert s.min_name_length == 5  # other engine-specific values stay
    assert (await store.engine_overrides(db, "solana_migration"))["mode"] == "follows_global"  # legacy copy converted

    # An engine that keeps its own value for the key is overruled by "all engines".
    await store.save_settings(db, "solana_fresh", {**settings_to_dict((await store.load_settings(db, "solana_fresh"))[0]),
                                                   "skip_duplicate_names": True}, None)
    await db.commit()
    _, _, touched = await store.save_for_all_engines(db, {"skip_duplicate_names": False}, None, ["solana_fresh"])
    await db.commit()
    assert touched == ["solana_fresh"] and (await store.load_settings(db, "solana_fresh"))[0].skip_duplicate_names is False

    with pytest.raises(store.SettingsError):
        await store.save_for_all_engines(db, {"no_such_setting": 1}, None, ["solana_fresh"])


async def test_follow_global_for_one_key_keeps_the_others(db):
    g = settings_to_dict(settings_from_dict({}))
    await store.save_settings(db, "GLOBAL", g, None)
    await store.save_settings(db, "solana_fresh", {**g, "skip_duplicate_names": False, "min_name_length": 4}, None)
    await store.follow_global(db, "solana_fresh", None, keys=["skip_duplicate_names"])
    await db.commit()
    o = await store.engine_overrides(db, "solana_fresh")
    assert o["keys"] == ["min_name_length"] and o["values"] == {"min_name_length": 4}


async def test_allow_word_filter_saved_in_the_dashboard_is_loaded_as_allow(db):
    """An ALLOW rule used to load as BLOCK (the action was dropped), so an
    exempted word blocked tokens instead."""
    from yonixalpha_core.db.models import BlacklistEntry
    from yonixalpha_core.safety.rules import match_blacklist

    db.add(BlacklistEntry(scope="GLOBAL", field="name", match_type="substring", value="elon", action="BLOCK"))
    db.add(BlacklistEntry(scope="GLOBAL", field="name", match_type="exact", value="elon dog", action="ALLOW"))
    await db.commit()
    rules = await store.load_blacklist(db)
    assert sorted(r.action for r in rules) == ["ALLOW", "BLOCK"]
    assert match_blacklist(rules, "solana_momentum", "elon dog", "ED", "M1") is None  # exempted
    assert match_blacklist(rules, "solana_momentum", "elon cat", "EC", "M2") is not None  # still blocked


def _alternatives(name, value):
    """Valid-looking non-default values to try for one setting."""
    from yonixalpha_core.safety.settings import ENUM_FIELDS

    if name in ENUM_FIELDS:
        return [o for o in ENUM_FIELDS[name] if o != value]
    if isinstance(value, bool):
        return [not value]
    if name == "tp_r_multiples":
        return [(Decimal("1.5"), Decimal("2.5"), Decimal("3.5"))]
    if name == "tp_exit_fractions":
        return [(Decimal("0.5"), Decimal("0.3"), Decimal("0.2"))]
    if value is None:
        return [0.6]
    if isinstance(value, int):
        return [value + 1, value - 1, value * 2, 1]
    if isinstance(value, Decimal):
        if value == 0:
            return [Decimal("0.01"), Decimal("1")]
        return [value * Decimal("1.1"), value * Decimal("0.9"), value * 2, value / 2]
    return []


async def test_every_risk_setting_saved_on_the_dashboard_reaches_every_solana_engine(db):
    """Each of the risk settings is changed on its own, saved to GLOBAL the
    way the dashboard saves it, and must then be exactly what the fresh,
    migration and momentum engines load. A setting with no valid alternative
    value would be reported, not skipped silently."""
    from dataclasses import fields, replace

    from yonixalpha_core.safety.settings import clamp, validate

    base = SafetySettings()
    untestable = []
    for f in fields(SafetySettings):
        current = getattr(base, f.name)
        chosen = None
        for alt in _alternatives(f.name, current):
            candidate = replace(base, **{f.name: alt})
            clamped, _ = clamp(candidate)
            if not validate(candidate) and getattr(clamped, f.name) == alt:
                chosen = alt
                break
        if chosen is None:
            untestable.append(f.name)
            continue
        await store.save_settings(db, "GLOBAL", settings_to_dict(replace(base, **{f.name: chosen})), None)
        await db.commit()
        for engine in ("solana_fresh", "solana_migration", "solana_momentum"):
            s, meta = await store.load_settings(db, engine)
            assert getattr(s, f.name) == chosen, (f.name, engine, getattr(s, f.name), chosen)
            assert store.settings_block_reason(engine, meta) is None
    assert untestable == [], f"settings with no valid alternative value to test: {untestable}"


async def test_global_intel_action_reaches_every_solana_engine_unless_one_overrides_it(db):
    g = settings_to_dict(SafetySettings())
    await store.save_settings(db, "GLOBAL", {**g, "dump_cluster_high_action": "NO_TRADE"}, None)
    await db.commit()
    for engine in ("solana_fresh", "solana_migration", "solana_momentum"):
        s, meta = await store.load_settings(db, engine)
        assert s.dump_cluster_high_action == "NO_TRADE", engine
        assert store.settings_block_reason(engine, meta) is None
    # An engine's own saved value wins for that engine only, and is listed as an override.
    await store.save_settings(db, "solana_momentum", {**g, "dump_cluster_high_action": "WARN"}, None)
    await db.commit()
    s, meta = await store.load_settings(db, "solana_momentum")
    assert s.dump_cluster_high_action == "WARN" and "dump_cluster_high_action" in meta["overrides"]
    assert (await store.load_settings(db, "solana_fresh"))[0].dump_cluster_high_action == "NO_TRADE"


async def test_invalid_saved_settings_block_new_entries_instead_of_trading_on_defaults(db):
    db.add(RiskSettingsVersion(scope="GLOBAL", version=1, settings={"min_stop_pct": "0.5", "max_stop_pct": "0.1",
                                                                    "dump_cluster_high_action": "NO_TRADE"}))
    await db.commit()
    settings, meta = await store.load_settings(db, "solana_fresh")
    assert settings.dump_cluster_high_action == "WARN"  # the defaults, which would silently drop NO_TRADE
    reason = store.settings_block_reason("solana_fresh", meta)
    assert reason and "failed validation" in reason and "new entries blocked" in reason
