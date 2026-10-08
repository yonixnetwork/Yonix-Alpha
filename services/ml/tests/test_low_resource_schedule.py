"""ML training in LOW_RESOURCE mode (2026-10-08): memecoin training keeps
its normal schedule and waits only while the host is CRITICAL; a skipped run
is recorded with its reason; copy-trading wallet ML follows the copy status."""

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
    assert not ok and "runs every 1:00:00" in why  # LOW_RESOURCE: memecoin ML keeps its hourly schedule
    await steps.skipped(redis, "solana_training", why)
    rec = await steps.read_one(redis, "solana_training")
    assert rec["state"] == "SKIPPED" and rec["reason"] == why and rec["last_ok_at"] and rec["last_state"] == "OK"
    rec["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
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


@pytest.mark.asyncio
async def test_solana_only_profile_skips_the_evm_ml_cycle_and_its_frozen_sets(db_session, redis, monkeypatch):
    """SYSTEM_PROFILE=SOLANA_ONLY: the BSC / Robinhood ML cycle is skipped
    with the reason (memecoin ML on Solana is not affected), and frozen
    validation only freezes and scores the Solana families."""
    import asyncio

    from app.validation import run_validation
    from yonixalpha_core.db.models import MlValidationSet
    from yonixalpha_core.ml import frozen
    from sqlalchemy import select

    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    monkeypatch.setattr(main, "get_settings", lambda: S)
    monkeypatch.setattr(main, "EVM_INTERVAL_SECONDS", 0.05)
    ran = []
    monkeypatch.setattr(main, "run_evm_cycle", lambda *a, **k: ran.append(1))
    stop = asyncio.Event()
    task = asyncio.create_task(main._evm_loop(sf, redis, stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 2)
    rec = await steps.read_one(redis, "evm_wallet_ml")
    assert ran == [] and rec["state"] == "SKIPPED" and rec["reason"].startswith("SKIPPED - DISABLED — SOLANA_ONLY MODE")

    out = await run_validation(sf, now=datetime(2026, 10, 8, tzinfo=timezone.utc), families=frozen.SOLANA_FAMILIES)
    assert out["families"] == list(frozen.SOLANA_FAMILIES)
    async with sf() as s:
        fams = set((await s.execute(select(MlValidationSet.family))).scalars())
    assert fams <= set(frozen.SOLANA_FAMILIES)


@pytest.mark.asyncio
async def test_a_skipped_training_run_is_checked_again_when_it_is_due(redis):
    """Not due yet: wake when the interval ends, not a full hour later.
    Skipped for resources (or never ran): retry within RETRY_SECONDS."""
    now = datetime(2026, 10, 8, 22, 27, tzinfo=timezone.utc)
    assert await main._next_check_s(redis, "solana_training", 3600, now) == main.RETRY_SECONDS  # no record
    await steps._put(redis, "solana_training", {"state": "OK", "started_at": "2026-10-08T21:44:10+00:00"})
    assert await main._next_check_s(redis, "solana_training", 3600, now) == 17 * 60 + 10  # due at 22:44:10
    late = datetime(2026, 10, 8, 22, 44, tzinfo=timezone.utc)
    assert await main._next_check_s(redis, "solana_training", 3600, late) == 60.0  # at least a minute
    over = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)
    assert await main._next_check_s(redis, "solana_training", 3600, over) == main.RETRY_SECONDS  # due: pressure retry
