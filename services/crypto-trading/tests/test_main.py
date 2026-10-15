import asyncio
import os
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yonixalpha_crypto.config import CryptoSettings
from yonixalpha_crypto.engine import Engine

DB = os.environ.setdefault("DATABASE_URL",
                           "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")


class NoRedis:
    async def get(self, key):
        return None

    async def getdel(self, key):
        return None

    async def set(self, *a, **k):
        return True

    async def aclose(self):
        pass


def test_idle_rule():
    assert Engine.idle({"state": "DISABLED", "open_positions": 0})
    assert Engine.idle({"state": "EMERGENCY_STOP", "open_positions": 0})
    assert not Engine.idle({"state": "DISABLED", "open_positions": 1})  # a position still needs protection
    assert not Engine.idle({"state": "RUNNING", "open_positions": 0})
    assert not Engine.idle({"state": "KILL_SWITCH", "open_positions": 0})


def test_the_worker_exits_by_itself_when_there_is_nothing_to_do():
    async def run():
        eng = create_async_engine(DB)
        async with eng.begin() as c:
            await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
        e = Engine(CryptoSettings(database_url=DB, redis_url="unused", loop_seconds=5), markets={}, redis=NoRedis(),
                   session_factory=async_sessionmaker(eng, expire_on_commit=False), exit_when_idle=True)
        started = datetime.now(timezone.utc)
        await asyncio.wait_for(e.run(), timeout=30)  # returns instead of looping forever
        await eng.dispose()
        return (datetime.now(timezone.utc) - started).total_seconds()

    assert asyncio.run(run()) < 10
