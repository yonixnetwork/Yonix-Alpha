"""Provider changes (2026-10-15): disabling or deleting a Solana endpoint
warns when nothing left enabled is known to work, and the audit log keeps
the values before and after (never the URL key)."""

from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog

from tests.conftest import TEST_ADMIN_PASSWORD
from tests.test_config_control import _mock_http


async def _add(client, auth_headers, name, key):
    body = {"name": name, "provider_type": "helius", "rpc_url": f"https://mainnet.{name}.example/?api-key={key}",
            "priority": 150, "timeout_seconds": "8", "password": TEST_ADMIN_PASSWORD}
    r = await client.post("/api/rpc/providers", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()["provider"]["id"]


async def _last_audit(app, event_type):
    async with app.state.db_session_factory() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.event_type == event_type)
                                .order_by(AuditLog.created_at.desc()))).scalars().first()


async def test_disabling_the_last_working_provider_warns_and_is_audited(app, client, auth_headers):
    _mock_http(app)
    a = await _add(client, auth_headers, "helius", "SECRETKEY111")
    _mock_http(app, 429)
    b = await _add(client, auth_headers, "backup", "SECRETKEY222")  # saved, but its test says RATE_LIMITED

    r = await client.patch(f"/api/rpc/providers/{a}", json={"enabled": False}, headers=auth_headers)
    assert r.status_code == 200, r.text
    w = r.json()["warnings"]
    assert len(w) == 2 and w[0].startswith("None of the Solana RPC endpoints left enabled (backup) has worked")
    assert "Only one Solana RPC endpoint is left enabled (backup)" in w[1]
    row = await _last_audit(app, "rpc.provider_updated")
    assert row.detail["values"]["enabled"] == {"before": True, "after": False}
    assert row.detail["warnings"] == w and "SECRETKEY" not in str(row.detail)

    # enabling, or a priority change, carries no warning
    r = await client.patch(f"/api/rpc/providers/{a}", json={"enabled": True}, headers=auth_headers)
    assert r.json()["warnings"] == []
    r = await client.patch(f"/api/rpc/providers/{a}", json={"priority": 20}, headers=auth_headers)
    assert r.json()["warnings"] == []
    assert (await _last_audit(app, "rpc.provider_updated")).detail["values"] == {"priority": {"before": 150, "after": 20}}

    # deleting the rate-limited one leaves the working one alone: only the "no failover" note
    r = await client.delete(f"/api/rpc/providers/{b}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["warnings"] == [
        "Only one Solana RPC endpoint is left enabled (helius): if it fails there is nothing to fail over to."]
    row = await _last_audit(app, "rpc.provider_deleted")
    assert row.detail["was"]["enabled"] is True and row.detail["was"]["host"].startswith("https://mainnet.backup.example")
    assert "SECRETKEY" not in str(row.detail)

    # the last enabled endpoint still cannot be disabled at all
    assert (await client.patch(f"/api/rpc/providers/{a}", json={"enabled": False}, headers=auth_headers)).status_code == 409
