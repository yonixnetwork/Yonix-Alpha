"""The crypto schema migrates up and down on its own and never touches the
public schema."""

import asyncio
import os

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_crypto.migrate import alembic_config

from tests.conftest import migrate_fresh


async def tables(url):
    eng = create_async_engine(url)
    async with eng.connect() as c:
        rows = (await c.execute(text("SELECT table_schema, table_name FROM information_schema.tables "
                                     "WHERE table_schema IN ('crypto', 'public')"))).all()
    await eng.dispose()
    return {(a, b) for a, b in rows}


async def test_upgrade_check_downgrade_cycle_leaves_public_alone():
    url = os.environ["DATABASE_URL"]
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(text("CREATE TABLE IF NOT EXISTS public.crypto_isolation_probe (id int)"))
    await eng.dispose()
    await migrate_fresh(url)
    t = await tables(url)
    assert {("crypto", n) for n in ("engine_state", "strategy_configs", "signals", "demo_orders", "demo_positions",
                                    "demo_accounts", "config_audit", "events", "api_connections", "exec_trades", "exec_orders", "trade_actions", "exec_guard", "ohlcv",
                                    "backtest_batches", "backtest_runs", "portfolio_settings", "alembic_version")} <= t
    cfg = alembic_config(url)
    await asyncio.to_thread(command.check, cfg)  # the models match the migrations
    await asyncio.to_thread(command.downgrade, cfg, "base")
    t2 = await tables(url)
    assert {x for x in t2 if x[0] == "crypto"} == {("crypto", "alembic_version")}
    assert ("public", "crypto_isolation_probe") in t2
    await asyncio.to_thread(command.upgrade, cfg, "head")
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(text("DROP TABLE public.crypto_isolation_probe"))
    await eng.dispose()
