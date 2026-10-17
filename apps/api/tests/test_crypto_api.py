"""Crypto Trading API: authenticated, DEMO only, per-strategy saves, and no
effect on any Memecoin setting or table."""

import asyncio
import os

import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import kill_switch
from yonixalpha_core.db.models import RiskSettingsVersion
from yonixalpha_crypto import engine as crypto_engine


async def migrate_crypto():
    from alembic import command

    from yonixalpha_crypto.migrate import alembic_config

    url = os.environ["DATABASE_URL"]
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
    await eng.dispose()
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")


@pytest_asyncio.fixture
async def crypto(app):
    await migrate_crypto()
    yield
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
    await eng.dispose()


def test_the_kill_switch_key_read_by_the_crypto_engine_is_the_platform_one():
    assert crypto_engine.KILL_SWITCH_KEY == kill_switch._KILL_SWITCH_KEY


async def test_requires_login(client):
    for path in ("/api/crypto/status", "/api/crypto/signals", "/api/crypto/orders", "/api/crypto/positions"):
        assert (await client.get(path)).status_code == 401
    assert (await client.put("/api/crypto/engine", json={"enabled": True})).status_code == 401


async def test_status_before_the_schema_exists_is_honest_not_an_error(client, auth_headers):
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
    await eng.dispose()
    r = await client.get("/api/crypto/status", headers=auth_headers)
    assert r.status_code == 200 and r.json()["schema_ready"] is False
    assert (await client.get("/api/crypto/signals", headers=auth_headers)).status_code == 503


async def test_status_lists_exchanges_and_strategies_off_by_default(client, auth_headers, crypto):
    body = (await client.get("/api/crypto/status", headers=auth_headers)).json()
    assert body["schema_ready"] and body["engine"]["enabled"] is False and body["engine"]["emergency_stop"] is False
    assert [e["id"] for e in body["exchanges"]] == ["bybit", "binance", "binance_us", "binance_data"]  # the last: public market data only
    assert all("ASSUMED" in e["fee_source"] for e in body["exchanges"])
    st = {s["id"]: s for s in body["strategies"]}
    assert st["gold_vs_btc"]["status"] == "BLOCKED" and st["confluence_matrix"]["status"] == "BLOCKED"
    assert all(s["config"]["enabled"] is False and s["config"]["mode"] == "DISABLED" for s in st.values())
    assert body["worker_running"] is False and "up -d crypto-trading" in body["worker_hint"]


async def test_saves_are_per_strategy_versioned_validated_and_demo_only(client, auth_headers, crypto):
    st = (await client.get("/api/crypto/status", headers=auth_headers)).json()["strategies"]
    mm = next(s for s in st if s["id"] == "meta_muse")
    r = await client.put("/api/crypto/strategies/meta_muse", headers=auth_headers,
                         json={"expected_version": mm["config"]["version"], "risk": {"risk_per_trade_quote": "3"}})
    assert r.status_code == 200 and r.json()["version"] == 2
    # stale version
    r = await client.put("/api/crypto/strategies/meta_muse", headers=auth_headers,
                         json={"expected_version": 1, "risk": {"risk_per_trade_quote": "4"}})
    assert r.status_code == 409
    # no live mode, no unknown keys, no single symbol for a two-asset strategy
    for bad in ({"mode": "LIVE"}, {"api_key": "x"}, {"symbols": ["ETH/USDT"]}):
        r = await client.put("/api/crypto/strategies/meta_muse", headers=auth_headers,
                             json={"expected_version": 2, **bad})
        assert r.status_code == 422, bad
    # a BLOCKED strategy cannot be enabled
    r = await client.put("/api/crypto/strategies/gold_vs_btc", headers=auth_headers,
                         json={"expected_version": 1, "enabled": True, "mode": "DEMO"})
    assert r.status_code == 422 and "cannot be enabled" in str(r.json()["detail"])
    # meta_muse (IMPLEMENTED) can be enabled in DEMO, and only in DEMO
    r = await client.put("/api/crypto/strategies/meta_muse", headers=auth_headers,
                         json={"expected_version": 2, "enabled": True, "mode": "DEMO"})
    assert r.status_code == 200 and r.json()["mode"] == "DEMO" and r.json()["version"] == 3
    other = next(s for s in (await client.get("/api/crypto/status", headers=auth_headers)).json()["strategies"]
                 if s["id"] == "gold_vs_btc")
    assert other["config"]["version"] == 1 and other["config"]["enabled"] is False  # untouched


async def test_engine_switch_never_touches_memecoin_settings(client, auth_headers, crypto, app):
    async with app.state.db_session_factory() as s:
        before = await s.scalar(select(func.count()).select_from(RiskSettingsVersion))
    for body in ({"enabled": True}, {"emergency_stop": True}, {"enabled": False, "emergency_stop": False}):
        r = await client.put("/api/crypto/engine", headers=auth_headers, json=body)
        assert r.status_code == 200
    async with app.state.db_session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(RiskSettingsVersion)) == before
    assert (await client.post("/api/crypto/health-check", headers=auth_headers)).json()["requested"] is True
    m = (await client.get("/api/crypto/metrics", params={"strategy_id": "meta_muse"}, headers=auth_headers)).json()
    assert m["closed_trades"] == 0 and m["win_rate"] is None and "needs 20" in m["unavailable_reason"]
