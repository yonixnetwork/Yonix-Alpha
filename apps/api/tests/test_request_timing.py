"""Request ID / timing middleware and the controlled 503s that replace a
hanging request (audit 2026-10-07: ML review and wallet pages ended in a
proxy 504 while their queries kept running)."""
import os

from fastapi import Depends
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.request_timing import SLOW_KEY, _param_names
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine




async def test_every_response_carries_a_request_id(client):
    r = await client.get("/api/health")
    assert len(r.headers["x-request-id"]) == 16 and int(r.headers["x-response-time-ms"]) >= 0
    r = await client.get("/api/health", headers={"X-Request-ID": "abc12345-ok"})
    assert r.headers["x-request-id"] == "abc12345-ok"
    r = await client.get("/api/health", headers={"X-Request-ID": "bad id <script>"})
    assert r.headers["x-request-id"] != "bad id <script>"


async def test_a_statement_timeout_answers_503_and_is_recorded(app, client, auth_headers):
    async def slow(db: AsyncSession = Depends(get_db)):
        await db.execute(text("SET LOCAL statement_timeout = 100"))
        await db.execute(text("SELECT pg_sleep(2)"))
        return {"never": True}

    app.add_api_route("/api/_test/slow", slow)
    r = await client.get("/api/_test/slow?mint=SECRETVALUE")
    body = r.json()
    assert r.status_code == 503 and body["code"] == "QUERY_TIMEOUT"
    assert body["request_id"] == r.headers["x-request-id"] and body["request_id"] in body["detail"]
    rows = (await client.get("/api/system/slow-requests", headers=auth_headers)).json()
    entry = rows["items"][0]
    assert entry["path"] == "/api/_test/slow" and entry["status"] == 503 and entry["params"] == ["mint"]
    assert "SECRETVALUE" not in str(rows) and rows["by_path"][0]["errors"] == 1


async def test_a_full_pool_answers_503(app, client):
    async def busy():
        raise sa_exc.TimeoutError("QueuePool limit reached")

    app.add_api_route("/api/_test/busy", busy)
    r = await client.get("/api/_test/busy")
    assert r.status_code == 503 and r.json()["code"] == "DB_POOL_EXHAUSTED"
    redis = app.state.redis
    assert await redis.llen(SLOW_KEY) >= 1


async def test_the_api_engine_sets_statement_timeout_and_workers_do_not():
    s = get_settings()
    api = make_engine(s, statement_timeout_ms=1500, pool_timeout_s=5)
    worker = make_engine(s)
    try:
        async with api.connect() as c:
            assert (await c.execute(text("SHOW statement_timeout"))).scalar_one() == "1500ms"
        async with worker.connect() as c:
            assert (await c.execute(text("SHOW statement_timeout"))).scalar_one() == "0"
        assert api.pool.timeout() == 5
    finally:
        await api.dispose()
        await worker.dispose()


def test_param_names_never_values():
    assert _param_names(b"mint=abc&days=7&mint=x&flag") == ["mint", "days", "flag"]
    assert os.environ["DATABASE_URL"]  # the suite's real Postgres
