"""External bot control API routes: auth, no secret leakage, confirmation."""

import httpx


async def test_bots_require_login_and_never_leak_tokens(app, client, auth_headers):
    assert (await client.get("/api/external-bots")).status_code == 401
    settings = app.state.settings
    object.__setattr__(settings, "META_MUSE_CONTROL_URL", "http://127.0.0.1:8101")
    object.__setattr__(settings, "META_MUSE_TOKEN", "super-secret-token")
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url), request.headers.get("Authorization")))
        if request.url.path == "/close":
            return httpx.Response(200, json={"closed": True, "detail": "closed LONG"})
        return httpx.Response(200, json={"running": True, "paused": False, "position": None, "last_signal": "none",
                                         "last_error": None, "uptime_seconds": 3})
    old = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        r = await client.get("/api/external-bots", headers=auth_headers)
        assert r.status_code == 200 and "super-secret-token" not in r.text and "127.0.0.1" not in r.text
        rows = {b["name"]: b for b in r.json()}
        assert rows["meta_muse"]["state"] == "CONNECTED" and rows["goldvsbtc"]["state"] == "NOT CONFIGURED"
        assert (await client.post("/api/external-bots/meta_muse/close", json={"confirm": "x"}, headers=auth_headers)).status_code == 422
        assert (await client.post("/api/external-bots/goldvsbtc/close", json={"confirm": "goldvsbtc"},
                                  headers=auth_headers)).status_code == 409
        r = await client.post("/api/external-bots/meta_muse/close", json={"confirm": "meta_muse"}, headers=auth_headers)
        assert r.json() == {"closed": True, "detail": "closed LONG"}
        assert ("POST", "http://127.0.0.1:8101/close", "Bearer super-secret-token") in calls
    finally:
        app.state.http = old
        object.__setattr__(settings, "META_MUSE_CONTROL_URL", None)
        object.__setattr__(settings, "META_MUSE_TOKEN", None)
