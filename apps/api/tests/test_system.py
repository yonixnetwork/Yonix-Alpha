import pytest

from yonixalpha_core.db.models import SystemEvent

pytestmark = pytest.mark.asyncio


async def _add_event(app, service, event_type, severity="info", detail=None):
    async with app.state.db_session_factory() as session:
        session.add(SystemEvent(service=service, event_type=event_type, severity=severity, detail=detail))
        await session.commit()


async def test_status_requires_auth(client):
    resp = await client.get("/api/system/status")
    assert resp.status_code == 401


async def test_status_reports_unknown_for_services_with_no_events(client, auth_headers):
    resp = await client.get("/api/system/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kill_switch"] == {"engaged": False, "reason": None}
    assert body["services"]["decision-engine"]["status"] == "unknown"
    assert body["services"]["decision-engine"]["last_event_at"] is None
    # every known service is reported, even with zero rows
    assert set(body["services"].keys()) == {
        "data-solana",
        "engine-solana-discovery",
        "engine-solana-migration",
        "engine-solana-momentum",
        "decision-engine",
        "ml",
        "paper-trading",
        "data-evm",
        "copy-engine",
    }


async def test_status_reports_running_after_service_started(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started")

    resp = await client.get("/api/system/status", headers=auth_headers)
    body = resp.json()
    assert body["services"]["decision-engine"]["status"] == "running"
    assert body["services"]["decision-engine"]["last_event_at"] is not None


async def test_status_reports_stopped_after_service_stopped(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started")
    await _add_event(app, "decision-engine", "service_stopped")

    resp = await client.get("/api/system/status", headers=auth_headers)
    body = resp.json()
    assert body["services"]["decision-engine"]["status"] == "stopped"


async def test_status_reports_running_after_restart(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started")
    await _add_event(app, "decision-engine", "service_stopped")
    await _add_event(app, "decision-engine", "service_started")

    resp = await client.get("/api/system/status", headers=auth_headers)
    body = resp.json()
    assert body["services"]["decision-engine"]["status"] == "running"


async def test_status_ignores_non_lifecycle_events_for_running_state(app, client, auth_headers):
    """An error event logged after service_started shouldn't flip status
    to stopped — only service_started/service_stopped decide running vs.
    stopped; other events are visible via /api/system/events instead.
    """
    await _add_event(app, "decision-engine", "service_started")
    await _add_event(app, "decision-engine", "evaluation_loop_failed", severity="error")

    resp = await client.get("/api/system/status", headers=auth_headers)
    body = resp.json()
    assert body["services"]["decision-engine"]["status"] == "running"


async def test_status_reflects_kill_switch_state(client, auth_headers):
    await client.post("/api/risk/kill-switch/engage", json={"reason": "test"}, headers=auth_headers)

    resp = await client.get("/api/system/status", headers=auth_headers)
    body = resp.json()
    assert body["kill_switch"]["engaged"] is True
    assert "test" in body["kill_switch"]["reason"]


async def test_list_events_requires_auth(client):
    resp = await client.get("/api/system/events")
    assert resp.status_code == 401


async def test_list_events_returns_seeded_rows(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started")
    await _add_event(app, "ml", "training_skipped_insufficient_samples", detail={"available_samples": 0})

    resp = await client.get("/api/system/events", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


async def test_list_events_filters_by_service(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started")
    await _add_event(app, "ml", "service_started")

    resp = await client.get("/api/system/events", params={"service": "ml"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["service"] == "ml"


async def test_list_events_filters_by_severity(app, client, auth_headers):
    await _add_event(app, "decision-engine", "service_started", severity="info")
    await _add_event(app, "decision-engine", "evaluation_loop_failed", severity="error")

    resp = await client.get("/api/system/events", params={"severity": "error"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["severity"] == "error"
