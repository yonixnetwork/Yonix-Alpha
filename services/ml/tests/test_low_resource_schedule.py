"""ML training on the low-resource schedule (2026-10-08): training (not
inference) runs once a day in LOW_RESOURCE mode and never while the host is
CRITICAL; a skipped run is recorded with its reason; wallet analytics follow
the copy-trading status."""

import os
from datetime import datetime, timedelta, timezone

import pytest
from redis.asyncio import from_url
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from app.evm_ml import WALLET_PAUSED, run_evm_cycle
from yonixalpha_core import resources
from yonixalpha_core.config import Settings
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.ml import steps

S = Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x")


@pytest.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/13"))
    await r.delete(steps.STEPS_KEY)
    yield r
    await r.delete(steps.STEPS_KEY)
    await r.aclose()


@pytest.mark.asyncio
async def test_training_waits_for_its_window_and_for_resources(db_session, redis, monkeypatch):
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    monkeypatch.setattr(resources, "level", lambda s, st: (resources.CRITICAL, ["load 9.82 = 4.91 per CPU >= 3.0"]))
    ok, why, st = await main._decide(sf, redis, S, "solana_training", 3600)
    assert not ok and why.startswith("SKIPPED - RESOURCE PRESSURE") and "4.91 per CPU" in why
    assert st["resource_mode"] == "LOW_RESOURCE" and st["copy_trading_effective"] == "SUSPENDED"
    monkeypatch.setattr(resources, "level", lambda s, st: (resources.WARNING, []))
    assert (await main._decide(sf, redis, S, "solana_training", 3600))[0]  # never ran: due
    await steps.timed(redis, "solana_training", _noop)
    ok, why, _ = await main._decide(sf, redis, S, "solana_training", 3600)
    assert not ok and "runs every 1 day" in why  # LOW_RESOURCE: once a day
    await steps.skipped(redis, "solana_training", why)
    rec = await steps.read_one(redis, "solana_training")
    assert rec["state"] == "SKIPPED" and rec["reason"] == why and rec["last_ok_at"] and rec["last_state"] == "OK"
    rec["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    await steps._put(redis, "solana_training", rec)
    assert (await main._decide(sf, redis, S, "solana_training", 3600))[0]


async def _noop():
    return None


@pytest.mark.asyncio
async def test_wallet_analytics_pause_with_copy_trading(db_session):
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    out = await run_evm_cycle(sf, now=datetime(2026, 10, 8, tzinfo=timezone.utc), wallet=False)
    assert out["wallet_episodes"] == out["missed_winners"] == out["wallet_models"] == WALLET_PAUSED
    assert "evm_models" in out and "exit_models" in out  # EVM entry / exit learning continues
