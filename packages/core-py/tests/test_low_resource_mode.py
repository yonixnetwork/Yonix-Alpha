"""Low-resource operation (2026-10-08): host readings, the resource level,
the copy-trading resume check and the ML training schedule."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import operating_mode, resources
from yonixalpha_core.config import Settings
from yonixalpha_core.db.base import Base, make_session_factory

S = Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x")
NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def reading(avail=1500, load=0.5, cpus=2, swap=0, mem_psi=None):
    return {"cpus": cpus, "memory": {"available_mb": avail, "swap_used_mb": swap},
            "load": {"1m": load, "5m": load, "15m": load},
            "pressure": {"memory": {"some": {"avg60": mem_psi}} if mem_psi is not None else None}}


def test_sample_reads_the_host_without_inventing_values():
    s = resources.sample()
    assert s["cpus"] >= 1 and s["memory"]["total_mb"] > 0 and s["load"]["1m"] >= 0
    assert s["cpu_busy_pct"] is None or 0 <= s["cpu_busy_pct"] <= 100  # needs two readings
    s2 = resources.sample()
    assert s2["cpu_busy_pct"] is None or 0 <= s2["cpu_busy_pct"] <= 100


def test_level_lists_every_threshold_crossed():
    assert resources.level(reading(), S) == (resources.NORMAL, [])
    lvl, why = resources.level(reading(avail=345, load=9.82), S)  # the server on 2026-10-07 22:00
    assert lvl == resources.CRITICAL and len(why) == 2 and "per CPU" in why[0]
    assert resources.level(reading(avail=350), S)[0] == resources.WARNING
    assert resources.level(reading(avail=150), S)[0] == resources.CRITICAL
    assert resources.level(reading(mem_psi=25.0), S)[0] == resources.CRITICAL
    assert resources.level({"cpus": 2, "memory": {}, "load": None}, S)[0] == resources.UNKNOWN


def test_copy_resume_needs_every_condition_and_unknown_fails():
    ok, why = resources.copy_resume_check(reading(avail=2000, load=1.0, swap=0), 20.0, S)
    assert ok and why == []
    ok, why = resources.copy_resume_check(reading(avail=345, load=9.82, swap=1298), 400.0, S)
    assert not ok and len(why) == 4
    ok, why = resources.copy_resume_check(reading(avail=2000, load=1.0), None, S)
    assert not ok and "database round trip unknown" in why[0]


def test_training_schedule_by_mode_and_level():
    """Operator, 2026-10-08: memecoin ML keeps its normal schedule in
    LOW_RESOURCE mode and only waits while the host is CRITICAL."""
    hour = timedelta(hours=1)
    assert operating_mode.training_decision("NORMAL", "CRITICAL", NOW - 2 * hour, NOW, S, hour)[0]
    ok, why = operating_mode.training_decision("LOW_RESOURCE", "CRITICAL", None, NOW, S, hour)
    assert not ok and why.startswith("SKIPPED - RESOURCE PRESSURE")
    assert operating_mode.training_decision("LOW_RESOURCE", "WARNING", NOW - timedelta(minutes=61), NOW, S, hour)[0]
    ok, why = operating_mode.training_decision("LOW_RESOURCE", "NORMAL", NOW - timedelta(minutes=20), NOW, S, hour)
    assert not ok and "runs every 1:00:00" in why
    assert operating_mode.training_decision("LOW_RESOURCE", "NORMAL", None, NOW, S, hour)[0]
    assert not operating_mode.training_decision("EMERGENCY", "NORMAL", None, NOW, S, hour)[0]
    longer = Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x", ML_TRAINING_INTERVAL_LOW_RESOURCE_H=24)
    ok, why = operating_mode.training_decision("LOW_RESOURCE", "NORMAL", NOW - timedelta(hours=3), NOW, longer, hour)
    assert not ok and "runs every 1 day" in why


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as s:
        yield s
    await engine.dispose()


async def test_mode_defaults_come_from_settings_and_the_dashboard_overrides(session):
    st = await operating_mode.load(session, S)
    assert (st["resource_mode"], st["copy_trading"], st["copy_trading_effective"]) == \
        ("LOW_RESOURCE", "SUSPENDED", "SUSPENDED")
    assert st["resource_mode_source"] == st["copy_trading_source"] == "default"
    await operating_mode.save(session, username="op", copy_trading="ACTIVE", note="test")
    await session.commit()
    st = await operating_mode.load(session, S)
    assert (st["copy_trading"], st["copy_trading_source"], st["changed_by"]) == ("ACTIVE", "dashboard", "op")
    await operating_mode.save(session, username="op", resource_mode="EMERGENCY")
    await session.commit()
    st = await operating_mode.load(session, S)
    assert st["copy_trading"] == "ACTIVE" and st["copy_trading_effective"] == "SUSPENDED"
    bad = SimpleNamespace(SYSTEM_RESOURCE_MODE="turbo", COPY_TRADING_STATUS="on")
    await operating_mode.save(session, username="op", resource_mode="nonsense", copy_trading="??")
    await session.commit()
    st = await operating_mode.load(session, bad)
    assert (st["resource_mode"], st["copy_trading"]) == ("LOW_RESOURCE", "SUSPENDED")  # unknown values: the safe defaults
