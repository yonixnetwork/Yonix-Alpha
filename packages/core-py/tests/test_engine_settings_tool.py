"""engine_settings tool (2026-10-15): a strategy's own risk values set through
the validated store; dry run by default, audit entry on apply."""

import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import AuditLog, RiskSettingsVersion  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.tools import engine_settings as es  # noqa: E402

VALUES = {"max_daily_loss_quote": "0.02", "max_loss_per_trade_sol": "0.0067"}


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = make_session_factory(engine)
    async with f() as s:
        await store.save_settings(s, "GLOBAL", {"min_stop_pct": "0.10", "max_daily_loss_quote": "0.5"}, None)
        await s.commit()
    yield f
    await engine.dispose()


async def test_dry_run_writes_nothing(sf):
    lines = await es.run(sf, "solana_migration", VALUES, "test", apply=False)
    assert any("DRY RUN" in x for x in lines) and any("rolled back" in x for x in lines)
    assert any("'max_loss_per_trade_sol': '0.0067'" in x for x in lines if x.startswith("after"))
    async with sf() as s:
        assert (await s.execute(select(func.count()).select_from(RiskSettingsVersion)
                                .where(RiskSettingsVersion.scope == "solana_migration"))).scalar_one() == 0
        eff, _ = await store.load_settings(s, "solana_migration")
        assert eff.max_daily_loss_quote == Decimal("0.5") and eff.max_loss_per_trade_sol == 0


async def test_apply_sets_only_this_strategy_and_audits(sf):
    lines = await es.run(sf, "solana_migration", VALUES, "operator approved", apply=True)
    assert any(x.startswith("SAVED as solana_migration v1") for x in lines), lines
    async with sf() as s:
        mig, _ = await store.load_settings(s, "solana_migration")
        fresh, _ = await store.load_settings(s, "solana_fresh")
        assert (mig.max_daily_loss_quote, mig.max_loss_per_trade_sol) == (Decimal("0.02"), Decimal("0.0067"))
        assert (fresh.max_daily_loss_quote, fresh.max_loss_per_trade_sol) == (Decimal("0.5"), Decimal(0))
        own = (await store.engine_overrides(s, "solana_migration"))["values"]
        assert set(own) == {"max_daily_loss_quote", "max_loss_per_trade_sol"}
        audit = (await s.execute(select(AuditLog).where(AuditLog.event_type == "risk_settings.updated")
                                 .order_by(AuditLog.created_at.desc()))).scalars().first()
        assert audit.detail["scope"] == "solana_migration" and audit.detail["note"] == "operator approved"


async def test_invalid_value_is_refused_and_nothing_is_written(sf):
    lines = await es.run(sf, "solana_migration", {"max_loss_per_trade_sol": "-1"}, "x", apply=True)
    assert any(x.startswith("REFUSED by validation") for x in lines)
    async with sf() as s:
        assert (await s.execute(select(func.count()).select_from(RiskSettingsVersion)
                                .where(RiskSettingsVersion.scope == "solana_migration"))).scalar_one() == 0
