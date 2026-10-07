"""The read-only db_health tool runs end to end on a real Postgres schema:
connections, locks, table sizes, the timed page queries and the slow list."""
import asyncio
import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.config import get_settings  # noqa: E402
from yonixalpha_core.db.base import Base  # noqa: E402
from yonixalpha_core.db.redis import make_redis  # noqa: E402
from yonixalpha_core.tools import db_health  # noqa: E402


async def _setup():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    get_settings.cache_clear()
    r = make_redis(get_settings())
    await r.delete(db_health.SLOW_REQUESTS_KEY)
    await r.lpush(db_health.SLOW_REQUESTS_KEY, json.dumps({"at": "2026-10-07T10:26:00", "status": 503, "ms": 25012,
                                                          "method": "GET", "path": "/api/ml/evm", "request_id": "abc123"}))
    await r.aclose()


def test_runs_read_only_end_to_end(capsys):
    asyncio.run(_setup())
    assert asyncio.run(db_health.main([])) == 0
    out = capsys.readouterr().out
    for part in ("1. Host", "load average", "2. Database connections", "max_connections", "3. Lock waits",
                 "4. Largest tables", "opportunity_outcomes", "5. Timed page queries",
                 "ML Review: ledger review counts (7 days):", "EVM ML: copy outcomes count (14 days):",
                 "503  25012 ms GET /api/ml/evm (request abc123)", "Nothing was written."):
        assert part in out, part
    assert "FAILED" not in out.split("5. Timed page queries")[1].split("6.")[0]
