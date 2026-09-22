import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402 - registers models on Base.metadata


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client():
    client = from_url(os.environ["REDIS_URL"], decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
