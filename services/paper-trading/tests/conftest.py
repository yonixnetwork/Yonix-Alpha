import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"
)

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402 - registers models on Base.metadata


@pytest_asyncio.fixture
async def _engine():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine):
    async with make_session_factory(_engine)() as session:
        yield session


@pytest_asyncio.fixture
async def session_factory(_engine):
    """The same session-factory shape app/main.py's loop functions take, on
    the same engine as `db_session`, so a test can seed through one and
    exercise the real loop through the other.
    """
    return make_session_factory(_engine)


@pytest_asyncio.fixture
async def redis_client():
    from redis.asyncio import from_url

    client = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def live_armed(session_factory, redis_client, monkeypatch):
    """LIVE armed for tests that exercise live BUY processing: the global
    mode LIVE and a passing live-check record (yonixalpha_core.live_control),
    which the order worker otherwise computes from real services. The checks
    themselves are tested in packages/core-py/tests/test_live_control.py."""
    import json
    from datetime import datetime, timezone

    from yonixalpha_core import live_control
    from yonixalpha_core.db.models import PlatformSetting

    async def passing(session, redis, settings, now=None):
        rec = {"ok": True, "at": datetime.now(timezone.utc).isoformat(), "failed": []}
        await redis.set(live_control.CONTROL_KEY, json.dumps(rec), ex=live_control.CONTROL_TTL)
        return rec

    monkeypatch.setattr(live_control, "publish_enforcement", passing)
    await passing(None, redis_client, None)
    async with session_factory() as s:
        s.add(PlatformSetting(key="global_mode", value={"mode": "LIVE"}))
        await s.commit()
    yield
