"""Crypto Trading exchange connections API: login and password required,
keys never returned, environments confirmed, live orders still locked."""

import json
import os

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_crypto.private import connections

from tests.conftest import TEST_ADMIN_PASSWORD
from tests.test_crypto_api import crypto  # noqa: F401  (fixture)

KEY, SECRET = "ffffffffgggggggg7890", "hhhhhhhhiiiiiiii4321"  # fake values


def body(**over):
    b = {"exchange": "bybit", "environment": "DEMO", "confirm_environment": "DEMO", "label": "demo",
         "api_key": KEY, "api_secret": SECRET, "password": TEST_ADMIN_PASSWORD}
    b.update(over)
    return b


async def test_requires_login(client):
    assert (await client.get("/api/crypto/connections")).status_code == 401
    assert (await client.post("/api/crypto/connections", json=body())).status_code == 401


async def test_save_needs_password_and_confirmation_and_never_returns_the_key(client, auth_headers, crypto):  # noqa: F811
    r = await client.post("/api/crypto/connections", json=body(password="wrong"), headers=auth_headers)
    assert r.status_code == 403
    r = await client.post("/api/crypto/connections", json=body(confirm_environment="LIVE"), headers=auth_headers)
    assert r.status_code == 422
    r = await client.post("/api/crypto/connections", json=body(), headers=auth_headers)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["enabled"] is False and saved["key_hint"] == "…7890" and saved["host"] == "https://api-demo.bybit.com"
    listing = await client.get("/api/crypto/connections", headers=auth_headers)
    raw = listing.text + r.text
    assert KEY not in raw and SECRET not in raw and "key_enc" not in raw
    assert listing.json()["live_orders_unlocked"] is False
    # the same key cannot be saved again for another environment
    r = await client.post("/api/crypto/connections", headers=auth_headers,
                          json=body(environment="LIVE", confirm_environment="LIVE", label="live", api_secret="OTHERSECRET9999"))
    assert r.status_code == 422 and "already saved" in r.json()["detail"]
    # Binance.US and unknown environments are refused
    r = await client.post("/api/crypto/connections", headers=auth_headers,
                          json=body(exchange="binance_us", environment="LIVE", confirm_environment="LIVE",
                                    api_key="USKEY000000001", api_secret="USSECRET000001"))
    assert r.status_code == 422


async def test_live_connection_changes_need_the_password_and_show_locked_gates(client, auth_headers, crypto):  # noqa: F811
    r = await client.post("/api/crypto/connections", headers=auth_headers,
                          json=body(environment="LIVE", confirm_environment="LIVE", label="live"))
    cid = r.json()["id"]
    gates = {g["id"]: g["ok"] for g in r.json()["live_gates"]}
    assert gates["code_lock"] is False and gates["tested"] is False
    assert (await client.put(f"/api/crypto/connections/{cid}", json={"enabled": True}, headers=auth_headers)).status_code == 422
    r = await client.put(f"/api/crypto/connections/{cid}", json={"enabled": True, "password": TEST_ADMIN_PASSWORD},
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert {g["id"]: g["ok"] for g in r.json()["live_gates"]}["code_lock"] is False


async def test_connection_test_is_read_only_and_throttled(client, auth_headers, crypto, monkeypatch):  # noqa: F811
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/v5/account/wallet-balance":
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": []}})
        return httpx.Response(200, json={"retCode": 10001, "retMsg": "not supported in demo", "result": {}})

    original = connections.test

    async def mocked(session, c, **kw):
        return await original(session, c, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(connections, "test", mocked)
    cid = (await client.post("/api/crypto/connections", json=body(), headers=auth_headers)).json()["id"]
    r = await client.post(f"/api/crypto/connections/{cid}/test", headers=auth_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["connection"]["status"] == "OK"
    assert out["permissions"]["verified"] is False  # demo host did not report the key's permissions
    assert {q.method for q in sent} == {"GET"} and {q.url.host for q in sent} == {"api-demo.bybit.com"}
    assert (await client.post(f"/api/crypto/connections/{cid}/test", headers=auth_headers)).status_code == 429
    r = await client.post(f"/api/crypto/connections/{cid}/delete", json={"password": TEST_ADMIN_PASSWORD},
                          headers=auth_headers)
    assert r.status_code == 200
    assert (await client.get("/api/crypto/connections", headers=auth_headers)).json()["connections"] == []
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.connect() as c:
        events = (await c.execute(text("SELECT message, detail FROM crypto.events WHERE source = 'connections'"))).all()
    await eng.dispose()
    assert len(events) >= 3 and all(KEY not in m + json.dumps(d) for m, d in events)
