import pytest
from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, RiskEvent

pytestmark = pytest.mark.asyncio


async def _seed_risk_event(app, approved=True) -> RiskEvent:
    async with app.state.db_session_factory() as session:
        event = RiskEvent(symbol="TESTMINT", approved=approved, reasons=[] if approved else ["kill switch engaged"])
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event


async def test_list_risk_events_requires_auth(client):
    resp = await client.get("/api/risk/events")
    assert resp.status_code == 401


async def test_list_risk_events_returns_seeded_rows(app, client, auth_headers):
    await _seed_risk_event(app, approved=True)
    await _seed_risk_event(app, approved=False)

    resp = await client.get("/api/risk/events", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


async def test_list_risk_events_filters_by_approved(app, client, auth_headers):
    await _seed_risk_event(app, approved=True)
    await _seed_risk_event(app, approved=False)

    resp = await client.get("/api/risk/events", params={"approved": "false"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["approved"] is False


async def test_kill_switch_status_requires_auth(client):
    resp = await client.get("/api/risk/kill-switch")
    assert resp.status_code == 401


async def test_kill_switch_starts_disengaged(client, auth_headers):
    resp = await client.get("/api/risk/kill-switch", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"engaged": False, "reason": None}


async def test_engage_kill_switch_updates_status_and_writes_audit_log(app, client, auth_headers):
    resp = await client.post("/api/risk/kill-switch/engage", json={"reason": "manual stop for testing"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["engaged"] is True
    assert "manual stop for testing" in body["reason"]

    status_resp = await client.get("/api/risk/kill-switch", headers=auth_headers)
    assert status_resp.json()["engaged"] is True

    async with app.state.db_session_factory() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.event_type == "kill_switch_engaged"))
        logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].detail == {"reason": "manual stop for testing"}
    assert logs[0].user_id is not None


async def test_engage_kill_switch_requires_a_reason(client, auth_headers):
    resp = await client.post("/api/risk/kill-switch/engage", json={"reason": ""}, headers=auth_headers)
    assert resp.status_code == 422


async def test_disengage_kill_switch(app, client, auth_headers):
    await client.post("/api/risk/kill-switch/engage", json={"reason": "test"}, headers=auth_headers)

    resp = await client.post("/api/risk/kill-switch/disengage", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"engaged": False, "reason": None}

    async with app.state.db_session_factory() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.event_type == "kill_switch_disengaged"))
        logs = result.scalars().all()
    assert len(logs) == 1


async def test_kill_switch_requires_auth_to_mutate(client):
    resp = await client.post("/api/risk/kill-switch/engage", json={"reason": "test"})
    assert resp.status_code == 401
    resp = await client.post("/api/risk/kill-switch/disengage")
    assert resp.status_code == 401


async def test_engage_kill_switch_sends_telegram_alert(client, auth_headers, monkeypatch):
    calls = []

    async def fake_send(settings, text, client=None):
        calls.append(text)
        return True

    import app.api.routes.risk as risk_module

    monkeypatch.setattr(risk_module, "send_telegram_alert", fake_send)

    resp = await client.post("/api/risk/kill-switch/engage", json={"reason": "manual stop for testing"}, headers=auth_headers)
    assert resp.status_code == 200
    assert len(calls) == 1
    assert "ENGAGED" in calls[0]
    assert "manual stop for testing" in calls[0]


async def test_disengage_kill_switch_sends_telegram_alert(client, auth_headers, monkeypatch):
    calls = []

    async def fake_send(settings, text, client=None):
        calls.append(text)
        return True

    import app.api.routes.risk as risk_module

    monkeypatch.setattr(risk_module, "send_telegram_alert", fake_send)

    await client.post("/api/risk/kill-switch/engage", json={"reason": "test"}, headers=auth_headers)
    resp = await client.post("/api/risk/kill-switch/disengage", headers=auth_headers)

    assert resp.status_code == 200
    assert len(calls) == 2  # engage, then disengage
    assert "disengaged" in calls[-1].lower()


async def test_kill_switch_alert_failure_does_not_break_the_request(app, client, auth_headers, monkeypatch):
    """send_telegram_alert itself is designed to never raise (see
    yonixalpha_core.notify), but the kill switch is the single most
    safety-critical write action in this system — engaging it must never
    fail just because a notification call misbehaves. risk.py wraps the
    call in its own try/except (`_alert`) as a second line of defense;
    this proves that guard actually works even if a future change to
    notify.py's contract slips and it starts raising.
    """

    async def failing_send(settings, text, client=None):
        raise RuntimeError("simulated Telegram outage")

    import app.api.routes.risk as risk_module

    monkeypatch.setattr(risk_module, "send_telegram_alert", failing_send)

    resp = await client.post("/api/risk/kill-switch/engage", json={"reason": "test"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["engaged"] is True

    status_resp = await client.get("/api/risk/kill-switch", headers=auth_headers)
    assert status_resp.json()["engaged"] is True
