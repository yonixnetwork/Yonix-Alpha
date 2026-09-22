import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

# Real admin credential fixture, computed at import time so lifespan's own
# admin-seeding (which runs against this same test database) produces a user
# whose password we actually know — avoids a second, conflicting seed step.
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

from yonixalpha_core.security import hash_password  # noqa: E402

TEST_ADMIN_PASSWORD = "test-password-123"
os.environ.setdefault("ADMIN_PASSWORD_HASH", hash_password(TEST_ADMIN_PASSWORD))

from yonixalpha_core.config import get_settings  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.redis import make_redis  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest_asyncio.fixture
async def app():
    get_settings.cache_clear()
    application = create_app()

    # Fresh schema per test against the real Postgres test database (matches
    # production's JSONB/UUID/INET column types exactly).
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = make_session_factory(engine)

    # Also flush Redis (db 15, dedicated to tests) before each test — login
    # lockout/failed-attempt keys carry a real TTL and will otherwise leak
    # across test runs (and across test-suite invocations within that TTL).
    redis = make_redis(get_settings())
    await redis.flushdb()
    await redis.aclose()

    async with application.router.lifespan_context(application):
        # Lifespan's own _seed_admin_user already created the "admin" user
        # above (from ADMIN_PASSWORD_HASH), using its own engine pointed at
        # this same database. Swap the app's session factory to ours so
        # request handlers and the fixture see the identical connection pool.
        application.state.db_session_factory = session_factory
        yield application

    await engine.dispose()


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_headers(client):
    login = await client.post("/api/auth/login", json={"username": "admin", "password": TEST_ADMIN_PASSWORD})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
