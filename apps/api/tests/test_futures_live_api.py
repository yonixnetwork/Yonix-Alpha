"""Futures live execution API: readiness is reported, never inferred;
settings are validated and audited."""

from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog


async def test_futures_status_and_settings(app, client, auth_headers):
    assert (await client.get("/api/live/futures")).status_code == 401
    r = (await client.get("/api/live/futures", headers=auth_headers)).json()
    assert set(r["venues"]) == {"binance", "bybit", "hyperliquid", "mt5"}
    assert all(not v["ready"] and "locks closed" in v["reason"] for v in r["venues"].values())
    assert r["settings"]["max_leverage"] == "5"
    bad = await client.put("/api/live/futures/settings", json={"max_leverage": 50, "api_key": "x"}, headers=auth_headers)
    assert bad.status_code == 422 and len(bad.json()["detail"]["errors"]) == 2
    ok = await client.put("/api/live/futures/settings", json={"max_leverage": 3, "min_free_balance": "25"}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["settings"]["max_leverage"] == "3"
    async with app.state.db_session_factory() as s:
        row = (await s.execute(select(AuditLog).where(AuditLog.event_type == "futures_live_settings.updated"))).scalar_one()
        assert row.detail["after"]["min_free_balance"] == "25"


async def test_config_validation_endpoint_names_variables_only(client, auth_headers):
    r = (await client.get("/api/system/config-validation", headers=auth_headers)).json()
    assert {"solana_fresh", "meta_muse", "gold_btc_trend", "confluence_matrix", "hyperliquid_grid", "alerts",
            "control_apis"} <= set(r["modules"])
    assert "SOLANA_RPC_URL is not set" in r["modules"]["solana_fresh"]["errors"]
