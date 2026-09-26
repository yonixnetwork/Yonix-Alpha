"""Dashboard provider-key updates: write-only, admin password required,
only provider keys, queued for the host helper (never applied by the api)."""

import json

from yonixalpha_core import env_updates
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.models import AuditLog
from sqlalchemy import select

from app.api.routes import settings_center
from tests.conftest import TEST_ADMIN_PASSWORD


def _use_env(app):
    get_settings.cache_clear()
    app.state.settings = get_settings()


async def test_key_endpoints_require_login(client):
    assert (await client.get("/api/settings/keys")).status_code == 401
    assert (await client.post("/api/settings/keys", json={"updates": {"HELIUS_API_KEY": "k"}, "password": "x"})).status_code == 401
    assert (await client.get(f"/api/settings/keys/requests/{'0' * 32}")).status_code == 401


async def test_key_status_never_returns_secret_values(app, client, auth_headers, monkeypatch, tmp_path):
    secret = "HELIUSSECRETVALUE987654"
    monkeypatch.setenv("HELIUS_API_KEY", secret)
    monkeypatch.setenv("SOLANA_RPC_URL", f"https://mainnet.helius-rpc.com/?api-key={secret}")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setattr(settings_center, "ENV_SPOOL", str(tmp_path / "missing"))
    _use_env(app)
    try:
        r = await client.get("/api/settings/keys", headers=auth_headers)
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200 and secret not in r.text
    keys = {k["key"]: k for k in r.json()["keys"]}
    assert keys["HELIUS_API_KEY"] == {"key": "HELIUS_API_KEY", "kind": "secret", "configured": True, "value": None}
    assert keys["SOLANA_RPC_URL"]["value"] == "https://mainnet.helius-rpc.com/…"
    assert keys["TELEGRAM_CHAT_ID"]["value"] == "-100123"
    assert r.json()["installed"] is False and "WALLET_PRIVATE_KEY" in r.json()["server_only"]
    assert "WALLET_PRIVATE_KEY" not in keys and "TRADING_ENABLED" not in keys


async def test_update_needs_the_admin_password_and_locks_out(app, client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_center, "ENV_SPOOL", str(tmp_path))
    body = {"updates": {"HELIUS_API_KEY": "new"}, "password": "wrong"}
    for _ in range(settings_center.PASSWORD_MAX_FAILURES):
        assert (await client.post("/api/settings/keys", headers=auth_headers, json=body)).status_code == 403
    # Locked out, even with the right password now.
    r = await client.post("/api/settings/keys", headers=auth_headers, json={**body, "password": TEST_ADMIN_PASSWORD})
    assert r.status_code == 429
    assert not list(tmp_path.iterdir())


async def test_update_is_validated_and_queued_for_the_host_helper(app, client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_center, "ENV_SPOOL", str(tmp_path))
    ok = {"password": TEST_ADMIN_PASSWORD}
    # Server-only keys and bad values are refused before anything is written.
    r = await client.post("/api/settings/keys", headers=auth_headers,
                          json={**ok, "updates": {"TRADING_ENABLED": "true", "WALLET_PRIVATE_KEY": "x"}})
    assert r.status_code == 422 and "server only" in r.text
    r = await client.post("/api/settings/keys", headers=auth_headers, json={**ok, "updates": {"SOLANA_RPC_URL": "http://x"}})
    assert r.status_code == 422
    assert not list(tmp_path.iterdir())

    new_value = "NEWHELIUSKEYVALUE55555"
    r = await client.post("/api/settings/keys", headers=auth_headers,
                          json={**ok, "updates": {"HELIUS_API_KEY": new_value, "SOLANA_RPC_URL": ""}})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    assert r.json()["status"] == "QUEUED" and new_value not in r.text
    req = json.loads((tmp_path / f"req-{rid}.json").read_text())
    assert req["updates"] == {"HELIUS_API_KEY": new_value, "SOLANA_RPC_URL": ""} and req["user"] == "admin"
    assert oct((tmp_path / f"req-{rid}.json").stat().st_mode)[-3:] == "600"
    assert (await client.get(f"/api/settings/keys/requests/{rid}", headers=auth_headers)).json()["status"] == "QUEUED"
    # A second change right away is refused (cooldown).
    again = await client.post("/api/settings/keys", headers=auth_headers, json={**ok, "updates": {"JUPITER_API_KEY": "j"}})
    assert again.status_code == 429

    # The host helper's result is reported back.
    (tmp_path / f"req-{rid}.json").unlink()
    env_updates.write_private(env_updates.result_path(str(tmp_path), rid),
                              {"id": rid, "status": "APPLIED", "keys": ["HELIUS_API_KEY", "SOLANA_RPC_URL"]})
    res = (await client.get(f"/api/settings/keys/requests/{rid}", headers=auth_headers)).json()
    assert res["status"] == "APPLIED"
    assert (await client.get("/api/settings/keys/requests/..%2Fetc", headers=auth_headers)).status_code == 404

    async with app.state.db_session_factory() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.event_type == "provider_keys.update_queued"))).scalars().all()
    assert rows and rows[0].detail["keys"] == ["HELIUS_API_KEY", "SOLANA_RPC_URL"] and new_value not in json.dumps(rows[0].detail)


async def test_update_refused_until_the_helper_is_installed(app, client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_center, "ENV_SPOOL", str(tmp_path / "not-installed"))
    r = await client.post("/api/settings/keys", headers=auth_headers,
                          json={"password": TEST_ADMIN_PASSWORD, "updates": {"HELIUS_API_KEY": "k"}})
    assert r.status_code == 503 and "install-env-updater" in r.text
