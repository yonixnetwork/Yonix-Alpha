"""Entry Intelligence API (2026-10-10): every endpoint needs a login, the
settings update is validated and audited, and the read endpoints answer on
an empty database and with a recorded signal. Nothing here can trade."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_store
from yonixalpha_core.db.models import AuditLog, EntrySignal

pytestmark = pytest.mark.asyncio
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
ENDPOINTS = ["/api/entry-intel/overview", "/api/entry-intel/signals", "/api/entry-intel/evaluation",
             "/api/entry-intel/latency", "/api/entry-intel/late-entries", "/api/entry-intel/parity",
             f"/api/entry-intel/tokens/{MINT}"]


async def test_entry_intel_requires_auth(client):
    for url in ENDPOINTS:
        assert (await client.get(url)).status_code == 401, url
    assert (await client.put("/api/entry-intel/settings", json={})).status_code == 401


async def test_read_endpoints_answer_on_an_empty_database(client, auth_headers):
    for url in ENDPOINTS:
        r = await client.get(url, headers=auth_headers)
        assert r.status_code == 200, (url, r.text)
    ov = (await client.get("/api/entry-intel/overview", headers=auth_headers)).json()
    assert set(ov["strategies"]) == set(ei.STRATEGIES) and ov["model"] is None
    assert all(m == "SHADOW" for m in ov["modes"].values())
    ev = (await client.get("/api/entry-intel/evaluation", headers=auth_headers)).json()
    assert ev["labelled"] == 0


async def test_settings_are_validated_and_audited(client, auth_headers, app):
    bad = await client.put("/api/entry-intel/settings", headers=auth_headers,
                           json={"modes": {ei.EARLY_ACCELERATION: "LIVE"}})
    assert bad.status_code == 422
    ok = await client.put("/api/entry-intel/settings", headers=auth_headers,
                          json={"modes": {ei.EARLY_ACCELERATION: "PAPER"}, "config": {"event_min_interval_seconds": 15}})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["modes"][ei.EARLY_ACCELERATION] == "PAPER" and body["config"]["event_min_interval_seconds"] == 15
    async with app.state.db_session_factory() as s:
        assert (await entry_store.load_settings(s))["modes"][ei.EARLY_ACCELERATION] == "PAPER"
        actions = (await s.execute(select(AuditLog.event_type))).scalars().all()
    assert "entry_intel.settings_updated" in actions


async def test_signals_and_token_detail_show_a_recorded_signal(client, auth_headers, app):
    now = datetime.now(timezone.utc)
    async with app.state.db_session_factory() as s:
        s.add(EntrySignal(mint=MINT, strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH", decision=ei.CANDIDATE,
                          decided_at=now, launch_at=now - timedelta(seconds=25), price_raw=30.0,
                          features={"age_seconds": 25}, reasons=["inflow accelerating"], evidence={"mode": "SHADOW"}))
        await s.commit()
    sig = (await client.get(f"/api/entry-intel/signals?strategy={ei.EARLY_ACCELERATION}", headers=auth_headers)).json()
    assert len(sig["signals"]) == 1 and sig["signals"][0]["mint"] == MINT
    assert (await client.get("/api/entry-intel/signals?strategy=NOPE", headers=auth_headers)).status_code == 422
    tok = (await client.get(f"/api/entry-intel/tokens/{MINT}", headers=auth_headers)).json()
    assert len(tok["signals"]) == 1 and tok["state"] is None
    assert (await client.get("/api/entry-intel/tokens/short", headers=auth_headers)).status_code == 422
