import json
from datetime import datetime, timezone

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.models import TokenObservation
from yonixalpha_core.solana import pump_stream

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
REPORT = {"outcome": "NO_TRADE", "trend": "DETERIORATING", "reasons": ["NO_TRADE: 3 trades since launch (need 8); inactive"],
          "metrics": {"trades_total": 3, "unique_buyers_total": 3, "liquidity_state": "NO DEX POOL YET — bonding curve market",
                      "curve_progress": "0.01", "creator_sold": True},
          "checkpoints": [{"label": "T0"}, {"label": "T+5s"}, {"label": "T+10s"}], "halves": [{}, {}]}


async def _seed(app):
    async with app.state.db_session_factory() as s:
        s.add(TokenObservation(mint="MINTNOTRADE", symbol="DUD", name="Dud", creator="C", launched_at=NOW, outcome="NO_TRADE",
                               trend="DETERIORATING", reasons=REPORT["reasons"], report=REPORT, decided_at=NOW))
        s.add(TokenObservation(mint="MINTPROMO", symbol="GOOD", outcome="PROMOTE", trend="INCREASING", reasons=["PROMOTE: ok"],
                               report={"metrics": {}}, decided_at=NOW))
        await s.commit()


async def test_observations_explain_why_a_token_was_not_traded(app, client, auth_headers):
    await _seed(app)
    r = await client.get("/api/observations", params={"outcome": "NO_TRADE"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["total"] == 1
    item = r.json()["items"][0]
    assert item["mint"] == "MINTNOTRADE" and item["reasons"][0].startswith("NO_TRADE: 3 trades")
    assert item["metrics"]["liquidity_state"].startswith("NO DEX POOL YET") and item["metrics"]["creator_sold"] is True

    detail = (await client.get("/api/observations/MINTNOTRADE", headers=auth_headers)).json()
    assert detail["observation"]["outcome"] == "NO_TRADE"
    assert [c["label"] for c in detail["observation"]["report"]["checkpoints"]] == ["T0", "T+5s", "T+10s"]
    assert (await client.get("/api/observations/UNKNOWN", headers=auth_headers)).status_code == 404
    assert (await client.get("/api/observations", params={"outcome": "BAD"}, headers=auth_headers)).status_code == 422
    assert (await client.get("/api/observations", params={"q": "GOO"}, headers=auth_headers)).json()["total"] == 1
    stats = (await client.get("/api/observations/stats", headers=auth_headers)).json()
    assert stats["retained_outcomes"] == {"NO_TRADE": 1, "PROMOTE": 1}


async def test_live_observations_come_from_the_stream_store(app, client, auth_headers):
    from yonixalpha_core.db.redis import make_redis

    redis = make_redis(get_settings())
    await redis.zadd(pump_stream.OBS_LIVE, {"LIVE1": 1})
    await redis.set(pump_stream.obs_report_key("LIVE1"), json.dumps({"outcome": "OBSERVING", "trend": "NO_ACTIVITY",
                                                                     "reasons": ["FRESH_OBSERVING: 4s of 10s"], "age_seconds": 4}))
    await redis.aclose()
    rows = (await client.get("/api/observations/live", headers=auth_headers)).json()
    assert rows[0]["mint"] == "LIVE1" and rows[0]["state"] == "FRESH_OBSERVING"


async def test_observation_endpoints_require_login(client):
    for path in ("/api/observations", "/api/observations/live", "/api/observations/stats", "/api/settings/overview"):
        assert (await client.get(path)).status_code == 401


def _use_env(app):
    get_settings.cache_clear()
    app.state.settings = get_settings()


async def test_settings_overview_never_returns_secret_values(app, client, auth_headers, monkeypatch):
    secret = "SUPERSECRETKEYVALUE123456"
    monkeypatch.setenv("HELIUS_API_KEY", secret)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret + "x")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "")
    monkeypatch.setenv("SOLANA_RPC_URL", f"https://rpc.example.com/?api-key={secret}")
    _use_env(app)
    try:
        r = await client.get("/api/settings/overview", headers=auth_headers)
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200
    assert secret not in r.text
    groups = {g["key"]: g for g in r.json()["groups"]}
    assert groups["helius"]["secrets"] == {"HELIUS_API_KEY": "configured"}
    assert groups["telegram"]["secrets"] == {"TELEGRAM_BOT_TOKEN": "configured"}
    assert not set(groups) & {"binance", "bybit", "hyperliquid", "mt5"}
    assert groups["solana"]["endpoints"]["SOLANA_RPC_URL"] == "https://rpc.example.com/…"
    assert groups["wallet"]["secrets"]["WALLET_PRIVATE_KEY"] == "not configured"
    assert any(s["section"] == "Risk & thresholds" for s in r.json()["sections"])


async def test_provider_test_reports_invalid_configuration_without_calling_out(app, client, auth_headers, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "")
    _use_env(app)
    try:
        r = await client.post("/api/settings/providers/helius/test", headers=auth_headers)
        again = await client.post("/api/settings/providers/helius/test", headers=auth_headers)
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200 and r.json()["status"] == "INVALID CONFIGURATION", r.json()
    assert again.status_code == 429  # cooldown
    overview = (await client.get("/api/settings/overview", headers=auth_headers)).json()
    helius = next(g for g in overview["groups"] if g["key"] == "helius")
    assert helius["last_test"]["helius"]["status"] == "INVALID CONFIGURATION"
    for gone in ("nope", "bybit", "binance"):
        assert (await client.post(f"/api/settings/providers/{gone}/test", headers=auth_headers)).status_code == 404
