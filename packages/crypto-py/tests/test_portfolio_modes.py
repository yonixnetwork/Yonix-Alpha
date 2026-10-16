"""Portfolio-wide limits, per-strategy risk modes, mode separation (paper /
exchange-demo / no live) and paper trade management."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from yonixalpha_crypto import config_store, portfolio, registry
from yonixalpha_crypto.db import (ApiConnection, DemoAccount, DemoPosition, EngineState, Event, ExecTrade, Signal,
                                  StrategyConfig)
from yonixalpha_crypto.risk import RiskLimits
from yonixalpha_crypto.strategies import IMPLEMENTATIONS

from tests.conftest import NOW, FakeMarket, book, candles
from tests.test_engine import TEST_ID, AlwaysLong, enable, engine, market, strategy  # noqa: F401


async def signals(sf):
    async with sf() as s:
        return (await s.execute(select(Signal).order_by(Signal.id))).scalars().all()


async def test_per_asset_exposure_is_limited_across_all_strategies(sf, redis, strategy):  # noqa: F811
    md = market()
    await enable(sf)
    async with sf() as s:
        await portfolio.save(s, {"max_exposure_per_asset_quote": "1"}, 1, "t")
        await s.commit()
    e = await engine(sf, redis, md)
    await e.tick(NOW)
    sig = (await signals(sf))[-1]
    assert sig.action == "REJECTED" and any("BTC exposure" in r for r in sig.reasons)


async def test_portfolio_settings_are_validated_versioned_and_audited(sf):
    async with sf() as s:
        lim = await portfolio.load(s)
        assert lim["backtests_enabled"] is False and lim["version"] == 1
        with pytest.raises(portfolio.PortfolioError):
            await portfolio.save(s, {"max_open_positions": "2.5"}, 1, "t")
        with pytest.raises(portfolio.PortfolioError):
            await portfolio.save(s, {"backtests_enabled": "yes"}, 1, "t")
        with pytest.raises(portfolio.PortfolioError, match="unknown"):
            await portfolio.save(s, {"leverage": 10}, 1, "t")
        out = await portfolio.save(s, {"max_daily_loss_quote": 25}, 1, "t")
        assert out["version"] == 2 and out["max_daily_loss_quote"] == "25"
        with pytest.raises(portfolio.PortfolioError, match="changed elsewhere"):
            await portfolio.save(s, {"max_daily_loss_quote": 30}, 1, "t")


async def test_drawdown_limit_sets_the_emergency_stop(sf):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        s.add(DemoAccount(exchange="binance_us", quote="USDT", starting_balance=Decimal(1000), cash=Decimal(700)))
        for k, pnl in enumerate([50, -150, -150]):
            s.add(DemoPosition(strategy_id="sma_cross", exchange="binance_us", symbol="BTC/USDT", qty=1, entry_price=1,
                               entry_cost_quote=1, status="CLOSED", realized_pnl_quote=Decimal(pnl),
                               opened_at=NOW - timedelta(days=3), closed_at=NOW - timedelta(days=2, hours=-k)))
        await s.commit()
        reasons = await portfolio.check_entry(s, symbol="ETH/USDT", planned_loss=Decimal(1), notional=Decimal(10), now=NOW)
        await s.commit()
        assert any("drawdown" in r for r in reasons)
        assert (await s.get(EngineState, 1)).emergency_stop is True
        ev = (await s.execute(select(Event).where(Event.source == "risk"))).scalars().all()
        assert ev and "drawdown shutdown" in ev[0].message


def test_risk_budget_fixed_or_percent_of_equity():
    fixed = RiskLimits.from_dict({"risk_per_trade_quote": "2"})
    assert fixed.budget(Decimal(1000)) == (Decimal(2), "fixed 2")
    pct = RiskLimits.from_dict({"risk_per_trade_quote": "2", "risk_pct": "0.5"})
    b, how = pct.budget(Decimal(1000))
    assert b == Decimal(5) and "0.5% of equity" in how


async def test_cooldown_after_consecutive_losses(sf, redis, strategy):  # noqa: F811
    md = market()
    await enable(sf)
    async with sf() as s:
        cfg = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == TEST_ID))).scalar_one()
        await config_store.save_strategy(s, TEST_ID, {"risk": {"max_consecutive_losses": "2", "cooldown_minutes": "120"}},
                                         cfg.version, "t")
        for k in range(2):
            s.add(DemoPosition(strategy_id=TEST_ID, exchange="bybit", symbol="BTC/USDT", qty=1, entry_price=1,
                               entry_cost_quote=1, status="CLOSED", realized_pnl_quote=Decimal(-1),
                               opened_at=NOW - timedelta(hours=2), closed_at=NOW - timedelta(minutes=30 + k)))
        await s.commit()
    await (await engine(sf, redis, md)).tick(NOW)
    sig = (await signals(sf))[-1]
    assert sig.action == "REJECTED" and any("cooldown" in r for r in sig.reasons)


async def test_mode_rules_live_is_not_selectable_and_exchange_demo_needs_a_practice_connection(sf):
    async with sf() as s:
        await config_store.ensure_defaults(s)
        live = ApiConnection(id=uuid.uuid4(), exchange="binance", environment="LIVE", label="l", key_enc="x",
                             secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…1", enabled=True,
                             created_at=NOW, updated_at=NOW)
        tn = ApiConnection(id=uuid.uuid4(), exchange="binance", environment="TESTNET", label="t", key_enc="x",
                           secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…2", enabled=True,
                           created_at=NOW, updated_at=NOW)
        s.add_all([live, tn])
        await s.commit()
        v = lambda: (s.execute(select(StrategyConfig.version).where(StrategyConfig.strategy_id == "sma_cross")))  # noqa: E731
        with pytest.raises(config_store.ConfigError, match="no live mode"):
            await config_store.save_strategy(s, "sma_cross", {"mode": "LIVE"}, (await v()).scalar_one(), "t")
        with pytest.raises(config_store.ConfigError, match="needs a practice"):
            await config_store.save_strategy(s, "sma_cross", {"mode": "EXCHANGE_DEMO"}, (await v()).scalar_one(), "t")
        with pytest.raises(config_store.ConfigError, match="DEMO / TESTNET only"):
            await config_store.save_strategy(s, "sma_cross", {"mode": "EXCHANGE_DEMO", "exchange": "binance",
                                                              "connection_id": str(live.id)}, (await v()).scalar_one(), "t")
        with pytest.raises(config_store.ConfigError, match="for binance"):
            await config_store.save_strategy(s, "sma_cross", {"mode": "EXCHANGE_DEMO", "exchange": "bybit",
                                                              "connection_id": str(tn.id)}, (await v()).scalar_one(), "t")
        with pytest.raises(config_store.ConfigError, match="exits"):
            await config_store.save_strategy(s, "sma_cross", {"exits": {"trailing": {"method": "martingale"}}},
                                             (await v()).scalar_one(), "t")
        out = await config_store.save_strategy(s, "sma_cross", {"mode": "EXCHANGE_DEMO", "exchange": "binance",
                                                                "connection_id": str(tn.id), "enabled": True,
                                                                "exits": {"partials": [{"at_r": 1, "fraction": 0.5}]}},
                                               (await v()).scalar_one(), "t")
        assert out["mode"] == "EXCHANGE_DEMO" and out["connection_id"] == str(tn.id)
        assert out["exits"]["partials"][0]["fraction"] == "0.5"
        # a different strategy's settings are untouched
        other = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == "ema_cross"))).scalar_one()
        assert other.mode == "DISABLED" and other.connection_id is None


async def test_paper_positions_get_trailing_partials_and_breakeven(sf, redis, strategy):  # noqa: F811
    md = market()
    await enable(sf)
    async with sf() as s:
        cfg = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == TEST_ID))).scalar_one()
        await config_store.save_strategy(s, TEST_ID, {"exits": {
            "partials": [{"at_r": 1, "fraction": 0.5, "stop_to": "breakeven"}],
            "trailing": {"method": "percent", "activate_r": 1, "percent": 1}}}, cfg.version, "t")
        await s.commit()
    e = await engine(sf, redis, md)
    await e.tick(NOW)
    async with sf() as s:
        p = (await s.execute(select(DemoPosition))).scalar_one()
        qty0, stop0, entry = p.qty, p.stop_price, p.entry_price
    md.books["BTC/USDT"] = book("101.5", "101.52")  # about +1.5 R (stop 1% below)
    await e.tick(NOW + timedelta(seconds=30))
    async with sf() as s:
        p = (await s.execute(select(DemoPosition))).scalar_one()
        assert p.status == "OPEN" and p.qty < qty0 and p.exits_state["partials_done"] == [0]
        assert p.stop_price > stop0 and p.stop_price >= entry  # breakeven, then trailing
        part = Decimal(p.exits_state["partial_realized_quote"])
        assert part > 0
        ev = (await s.execute(select(Event).where(Event.source == TEST_ID))).scalars().all()
        assert any("PARTIAL_EXIT" in x.message for x in ev) and any("MOVE_STOP" in x.message for x in ev)
    md.books["BTC/USDT"] = book("99.9", "99.92")  # back below the raised stop
    await e.tick(NOW + timedelta(seconds=60))
    async with sf() as s:
        p = (await s.execute(select(DemoPosition))).scalar_one()
        assert p.status == "CLOSED"
        # the final P&L = the remainder's proceeds - its cost + the partial's realized P&L
        assert p.realized_pnl_quote == Decimal(p.proceeds_quote) - Decimal(p.entry_cost_quote) + part


class BinanceLong(AlwaysLong):
    spec = registry.StrategySpec(
        id="test_binance_long", name="test", version="t1", category="test", status=registry.IMPLEMENTED,
        status_reason="", provenance={}, exchanges=("binance",), intervals=("5m",), default_exchange="binance",
        default_symbols=("BTC/USDT",), default_interval="5m", params={"side": ("LONG", "choice:LONG|SHORT", None, None)})


async def test_exchange_demo_strategy_orders_go_through_the_executor_on_the_practice_host(sf, redis, monkeypatch):
    from yonixalpha_crypto.private import adapters, service
    from yonixalpha_crypto.private.signedhttp import SignedHttp
    from tests.fake_exchanges import FakeBinance

    registry.register(BinanceLong.spec)
    IMPLEMENTATIONS["test_binance_long"] = BinanceLong()
    fake = FakeBinance(bid="100", ask="100.02")
    monkeypatch.setattr(service, "open_account", lambda c, **kw: adapters.account_for(
        SignedHttp(c.exchange, c.environment, "aaaaaaaabbbbbbbb0007", "ccccccccdddddddd0007", transport=fake.transport())))
    try:
        md = FakeMarket("binance")
        md.series["BTC/USDT"] = candles(["100", "100", "100", "100"])
        md.books["BTC/USDT"] = book("100", "100.02", exchange="binance")
        async with sf() as s:
            await config_store.ensure_defaults(s)
            tn = ApiConnection(id=uuid.uuid4(), exchange="binance", environment="TESTNET", label="tn", key_enc="x",
                               secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…7", enabled=True,
                               status="OK", created_at=NOW, updated_at=NOW)
            s.add(tn)
            await s.commit()
            await config_store.save_strategy(s, "test_binance_long", {
                "enabled": True, "mode": "EXCHANGE_DEMO", "connection_id": str(tn.id),
                "risk": {"risk_per_trade_quote": "5", "max_position_quote": "300"}}, 1, "t")
            await config_store.set_engine(s, enabled=True, emergency_stop=None, note=None, actor="t")
            await s.commit()
        e = await engine(sf, redis, md)
        await e.tick(NOW)
        async with sf() as s:
            t = (await s.execute(select(ExecTrade))).scalar_one()
            assert t.status == "OPEN" and t.protection == "VERIFIED" and t.environment == "TESTNET"
            assert t.entry_key.startswith("test_binance_long:binance:BTC/USDT:")
            assert (await s.execute(select(DemoPosition))).scalars().all() == []  # no paper fill
            sig = (await s.execute(select(Signal))).scalars().all()[-1]
            assert sig.detail["exchange_demo"]["trade_id"] == str(t.id)
        assert all(r.url.host == "testnet.binance.vision" for r in fake.requests)
        await e.tick(NOW)  # the same candle again: no second order
        assert fake.order_count("BUY") == 1
    finally:
        IMPLEMENTATIONS.pop("test_binance_long", None)
        registry._REGISTRY.pop("test_binance_long", None)
