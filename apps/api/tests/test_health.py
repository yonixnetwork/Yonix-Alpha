import pytest

pytestmark = pytest.mark.asyncio


async def test_liveness_ok(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_reports_checks(client):
    resp = await client.get("/api/health/ready")
    body = resp.json()
    assert "checks" in body
    assert "database" in body["checks"]
    assert "redis" in body["checks"]
