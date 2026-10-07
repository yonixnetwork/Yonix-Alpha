from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from yonixalpha_core.config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings, *, statement_timeout_ms: int | None = None, pool_timeout_s: int | None = None):
    """statement_timeout_ms / pool_timeout_s: the dashboard API's limits
    (Settings.API_STATEMENT_TIMEOUT_MS / API_POOL_TIMEOUT_S); workers pass
    neither and keep the defaults (no statement limit, 30 s pool wait)."""
    kw = {}
    if statement_timeout_ms:
        kw["connect_args"] = {"server_settings": {"statement_timeout": str(int(statement_timeout_ms))}}
    if pool_timeout_s:
        kw["pool_timeout"] = pool_timeout_s
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        echo=False,
        **kw,
    )


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
