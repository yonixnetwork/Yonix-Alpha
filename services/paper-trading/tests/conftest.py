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
