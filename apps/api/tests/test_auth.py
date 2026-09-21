import pytest

pytestmark = pytest.mark.asyncio


async def test_login_success(client):
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_wrong_password(client):
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


async def test_login_unknown_user(client):
    resp = await client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


async def test_me_requires_auth(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_valid_token(client):
    login = await client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    token = login.json()["access_token"]
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"


async def test_refresh_rotates_token(client):
    login = await client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    refresh_token = login.json()["refresh_token"]

    refreshed = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != login.json()["access_token"]

    # Old refresh token must now be rejected (rotation).
    reused = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert reused.status_code == 401


async def test_logout_revokes_session(client):
    login = await client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    refresh_token = login.json()["refresh_token"]

    logout_resp = await client.post("/api/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 204

    reused = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert reused.status_code == 401


async def test_lockout_after_failed_attempts(client):
    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    locked = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert locked.status_code == 429
