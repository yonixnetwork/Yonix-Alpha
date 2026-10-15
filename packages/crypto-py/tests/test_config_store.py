"""Settings: off by default, one strategy's save never touches another's,
stale saves are refused, and every change is audited."""

import pytest
from sqlalchemy import func, select

from yonixalpha_crypto import config_store
from yonixalpha_crypto.db import ConfigAudit, EngineState, StrategyConfig


async def test_defaults_are_off_and_a_restart_never_turns_anything_on(sf):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        await s.commit()
        await config_store.ensure_defaults(s)  # a second start
        await s.commit()
        eng = await s.get(EngineState, 1)
        rows = (await s.execute(select(StrategyConfig))).scalars().all()
    assert eng.enabled is False and eng.emergency_stop is False
    assert {r.strategy_id for r in rows} >= {"meta_muse", "gold_vs_btc", "hyperliquid_grid", "confluence_matrix"}
    assert all(r.enabled is False and r.mode == "DISABLED" for r in rows)


async def test_a_save_changes_only_its_own_strategy_and_needs_the_current_version(sf):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        await s.commit()
        other_before = config_store.config_dict(await s.get(StrategyConfig, "gold_vs_btc"))
        out = await config_store.save_strategy(s, "meta_muse", {"risk": {"risk_per_trade_quote": "2"},
                                                                "exchange": "binance_us"}, 1, "admin")
        await s.commit()
        assert out["version"] == 2 and out["risk"]["risk_per_trade_quote"] == "2" and out["exchange"] == "binance_us"
        assert out["risk"]["max_daily_loss_quote"] == "15"  # untouched keys kept
        assert config_store.config_dict(await s.get(StrategyConfig, "gold_vs_btc")) == other_before
        with pytest.raises(config_store.ConfigConflict):
            await config_store.save_strategy(s, "meta_muse", {"interval": "15m"}, 1, "admin")
        await s.rollback()
        audits = (await s.execute(select(ConfigAudit))).scalars().all()
    assert [a.scope for a in audits] == ["meta_muse"]
    assert audits[0].before["risk"]["risk_per_trade_quote"] == "5" and audits[0].after["version"] == 2


async def test_unimplemented_or_blocked_strategies_cannot_be_enabled(sf):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        await s.commit()
        for sid in ("gold_vs_btc", "hyperliquid_grid", "confluence_matrix"):
            with pytest.raises(config_store.ConfigError, match="cannot be enabled"):
                await config_store.save_strategy(s, sid, {"enabled": True, "mode": "DEMO"}, 1, "admin")
        with pytest.raises(config_store.ConfigError):
            await config_store.save_strategy(s, "meta_muse", {"exchange": "kraken"}, 1, "admin")
        with pytest.raises(config_store.ConfigError):
            await config_store.save_strategy(s, "meta_muse", {"symbols": ["BTCUSDT"]}, 1, "admin")
        await s.rollback()
        assert await s.scalar(select(func.count()).select_from(ConfigAudit)) == 0


async def test_engine_switch_and_emergency_stop_are_audited(sf):
    async with sf() as s:
        out = await config_store.set_engine(s, enabled=True, emergency_stop=None, note="try", actor="admin")
        out2 = await config_store.set_engine(s, enabled=None, emergency_stop=True, note=None, actor="admin")
        await s.commit()
        audits = (await s.execute(select(ConfigAudit).order_by(ConfigAudit.id))).scalars().all()
    assert out["enabled"] and out2["emergency_stop"] and out2["enabled"]
    assert [a.scope for a in audits] == ["engine", "engine"] and audits[0].before["enabled"] is False
