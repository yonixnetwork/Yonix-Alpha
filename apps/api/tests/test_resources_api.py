"""Low-resource operation (2026-10-08): System Health resources, the
resource mode and the copy-trading status with its resume check."""

from yonixalpha_core import resources
from yonixalpha_core.db.models import AuditLog


def host(avail=2000, load=0.4, swap=0):
    return lambda: {"cpus": 2, "memory": {"total_mb": 1967, "available_mb": avail, "swap_used_mb": swap},
                    "load": {"1m": load, "5m": load, "15m": load}, "pressure": {}, "disk": None, "cpu_busy_pct": None}


async def test_resources_show_mode_level_and_what_is_paused(client, auth_headers, monkeypatch, app):
    monkeypatch.setattr(resources, "sample", host(avail=345, load=9.82, swap=1298))
    await app.state.redis.set("yx:hb:paper-trading", '{"at": "2026-10-08T10:00:00+00:00", "rss_mb": 210.5, '
                                                           '"cpu_s": 100.0, "detail": {}}')
    r = (await client.get("/api/system/resources", headers=auth_headers)).json()
    assert r["mode"]["resource_mode"] == "LOW_RESOURCE" and r["mode"]["label"] == "LOW RESOURCE MODE"
    assert r["host"]["memory"]["available_mb"] == 345
    assert "copy trading" in r["paused_now"] and r["copy_trading"]["status"] == "SUSPENDED"
    assert r["copy_trading"]["label"] == "SUSPENDED — LOW SERVER RESOURCES"
    assert r["postgres"]["connections"] and "shared_buffers" in r["postgres"]["settings"]
    assert r["redis"]["used_mb"] > 0
    pt = next(w for w in r["workers"] if w["service"] == "paper-trading")
    assert pt["rss_mb"] == 210.5 and pt["cpu_pct"] is None  # one heartbeat: no share yet
    await app.state.redis.set("yx:hb:paper-trading", '{"at": "2026-10-08T10:00:30+00:00", "rss_mb": 211, '
                                                           '"cpu_s": 106.0, "detail": {}}')
    r = (await client.get("/api/system/resources", headers=auth_headers)).json()
    assert next(w for w in r["workers"] if w["service"] == "paper-trading")["cpu_pct"] == 20.0  # 6 s of 30 s


async def test_copy_resume_is_refused_while_resources_are_unsafe(client, auth_headers, monkeypatch, app):
    monkeypatch.setattr(resources, "sample", host(avail=345, load=9.82, swap=1298))
    s = (await client.get("/api/copy/trading-status", headers=auth_headers)).json()
    assert s["status"] == "SUSPENDED" and s["resume"]["recommendation"] == "WAIT" and s["resume"]["auto_resume"] is False
    assert s["current"]["ram_available_mb"] == 345 and s["current"]["swap_used_mb"] == 1298
    assert "Copy trading has been paused" in s["reason"]
    r = await client.post("/api/copy/trading-status", json={"status": "ACTIVE"}, headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["recommendation"] == "WAIT"
    assert len(r.json()["detail"]["blocked_by"]) >= 3
    assert (await client.get("/api/copy/trading-status", headers=auth_headers)).json()["status"] == "SUSPENDED"

    monkeypatch.setattr(resources, "sample", host())
    r = await client.post("/api/copy/trading-status", json={"status": "THROTTLED", "note": "after resize"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "THROTTLED"
    r = await client.post("/api/copy/trading-status", json={"status": "SUSPENDED"}, headers=auth_headers)
    assert r.json()["status"] == "SUSPENDED"
    assert (await client.post("/api/copy/trading-status", json={"status": "ON"}, headers=auth_headers)).status_code == 422

    await client.put("/api/system/resource-mode", json={"mode": "EMERGENCY"}, headers=auth_headers)
    r = await client.post("/api/copy/trading-status", json={"status": "ACTIVE"}, headers=auth_headers)
    assert r.status_code == 409 and "EMERGENCY resource mode" in r.json()["detail"]["blocked_by"]
    async with app.state.db_session_factory() as s:
        from sqlalchemy import select
        kinds = (await s.execute(select(AuditLog.event_type))).scalars().all()
    assert "copy.trading_status" in kinds and "system.resource_mode" in kinds


async def test_review_page_is_202_while_its_first_result_waits_for_resources(client, auth_headers, monkeypatch):
    monkeypatch.setattr(resources, "level", lambda s, st: (resources.CRITICAL, ["load 9.82 = 4.91 per CPU >= 3.0"]))
    r = await client.get("/api/ml/ledger-review", params={"days": 7}, headers=auth_headers)
    assert r.status_code == 202 and r.json()["review_status"] == "PENDING" and "CRITICAL" in r.json()["deferred"]
    r = await client.get("/api/ml/ledger-review", params={"days": 7, "refresh": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["review_status"] == "CURRENT"  # the operator asked: computed anyway
