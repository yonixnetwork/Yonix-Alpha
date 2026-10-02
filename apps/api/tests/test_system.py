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


async def test_updates_lists_watches_events_and_acknowledges(app, client, auth_headers):
    """Update monitor (master §64-66): unchecked watches read NOT_CHECKED, never
    "up to date"; events are listed newest first and acknowledged once, audited."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from yonixalpha_core import update_monitor
    from yonixalpha_core.db.models import AuditLog, UpdateEvent, UpdateWatch

    assert (await client.get("/api/system/updates")).status_code == 401
    at = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    async with app.state.db_session_factory() as s:
        s.add(UpdateWatch(key="github:ponsdotdev/ponsfamily", category="robinhood", last_checked=at,
                          latest_commit="c" * 40, classification="ACTION_REQUIRED", flags={"api": True}))
        s.add(UpdateWatch(key="pypi:httpx", category="dependency", last_checked=at, error="RateLimited: x"))
        ev = UpdateEvent(key="github:ponsdotdev/ponsfamily", detected_at=at, classification="ACTION_REQUIRED",
                         from_ref="b" * 40, to_ref="c" * 40, summary={"why": "Pons", "reasons": ["abi"]}, notified=True)
        s.add(ev)
        await s.commit()
        eid = str(ev.id)
    body = (await client.get("/api/system/updates", headers=auth_headers)).json()
    by_key = {w["key"]: w for w in body["watches"]}
    assert len(body["watches"]) == len(update_monitor.WATCHES)
    assert by_key["github:ponsdotdev/ponsfamily"]["status"] == "CHECKED"
    assert by_key["github:ponsdotdev/ponsfamily"]["classification"] == "ACTION_REQUIRED"
    assert by_key["pypi:httpx"]["status"] == "ERROR"
    assert by_key["github:anza-xyz/agave"]["status"] == "NOT_CHECKED" and by_key["github:anza-xyz/agave"]["classification"] is None
    assert body["unacknowledged"] == {"ACTION_REQUIRED": 1} and body["events"][0]["id"] == eid
    assert body["github_token"] in ("configured", "not configured (60 requests per hour)")  # never the value
    assert "automatically" in body["note"]

    r = await client.post(f"/api/system/updates/{eid}/acknowledge", headers=auth_headers)
    assert r.status_code == 200 and r.json()["acknowledged_by"]
    first = r.json()["acknowledged_at"]
    assert (await client.post(f"/api/system/updates/{eid}/acknowledge", headers=auth_headers)).json()["acknowledged_at"] == first
    assert (await client.get("/api/system/updates", headers=auth_headers)).json()["unacknowledged"] == {}
    assert (await client.post("/api/system/updates/not-a-uuid/acknowledge", headers=auth_headers)).status_code == 404
    assert (await client.post(f"/api/system/updates/{'0' * 8}-0000-0000-0000-{'0' * 12}/acknowledge",
                              headers=auth_headers)).status_code == 404
    async with app.state.db_session_factory() as s:
        audits = (await s.execute(select(AuditLog).where(AuditLog.event_type == "update_event.acknowledge"))).scalars().all()
        assert len(audits) == 1
