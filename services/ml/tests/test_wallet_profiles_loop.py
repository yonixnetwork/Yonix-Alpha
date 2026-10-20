"""Solana wallet profiles without copy trading (memecoin refocus Phase 4,
2026-10-20): while copy-engine is off (SOLANA_ONLY), the ml service rebuilds
the Smart Wallets profiles itself; while copy-engine runs it skips, so the
work is never done twice. Research only: nothing is copied or traded."""

import asyncio
import os

import pytest
from redis.asyncio import from_url
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from yonixalpha_core import resources
from yonixalpha_core.config import Settings
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.ml import steps

OFF = Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x", COPY_TRADING_ENABLED=False)
ON = Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x", COPY_TRADING_ENABLED=True)


@pytest.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/13"))
    await r.delete(steps.STEPS_KEY)
    yield r
    await r.delete(steps.STEPS_KEY)
    await r.aclose()


async def _run_once(monkeypatch, sf, redis, settings):
    calls = []

    async def fake_rebuild(session_factory):
        calls.append(1)
        return {"solana_profiles": 3}

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "rebuild_solana_wallet_profiles", fake_rebuild)
    monkeypatch.setattr(resources, "level", lambda s, st: (resources.WARNING, []))
    stop = asyncio.Event()
    task = asyncio.create_task(main._wallet_profiles_loop(sf, redis, stop))
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, 5)
    return calls, await steps.read_one(redis, "solana_wallet_profiles")


@pytest.mark.asyncio
async def test_profiles_are_rebuilt_while_copy_trading_is_off(db_session, redis, monkeypatch):
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    calls, rec = await _run_once(monkeypatch, sf, redis, OFF)
    assert calls == [1] and rec["state"] == "OK"


@pytest.mark.asyncio
async def test_profiles_are_left_to_copy_engine_while_it_runs(db_session, redis, monkeypatch):
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    calls, rec = await _run_once(monkeypatch, sf, redis, ON)
    assert calls == [] and rec["state"] == "SKIPPED" and "copy-engine is running" in rec["reason"]


@pytest.mark.asyncio
async def test_the_real_rebuild_runs_on_an_empty_database(db_session):
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    assert await main.rebuild_solana_wallet_profiles(sf) == {"solana_profiles": 0}
