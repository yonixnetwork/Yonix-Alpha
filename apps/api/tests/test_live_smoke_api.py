"""LIVE_EXECUTION_SMOKE_TEST API and the LIVE/PAPER wallet view: disabled by
default, armed only with the server switch + admin password + typed phrase,
audited, and no secret ever returned."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from yonixalpha_core import live_smoke, live_trading
from yonixalpha_core.db.models import AuditLog, LiveSmokeTest

from tests.conftest import TEST_ADMIN_PASSWORD

pytestmark = pytest.mark.asyncio
ARM = {"category": "FRESH", "minutes": 30, "password": TEST_ADMIN_PASSWORD, "confirm": live_smoke.CONFIRM_PHRASE}


async def _audits(app) -> list[str]:
    async with app.state.db_session_factory() as s:
        return [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]


async def test_smoke_endpoints_require_auth(client):
    assert (await client.get("/api/live/smoke-test")).status_code == 401
    assert (await client.post("/api/live/smoke-test/arm", json=ARM)).status_code == 401
    assert (await client.get("/api/live/wallets")).status_code == 401


async def test_disabled_by_default_and_never_armed(app, client, auth_headers):
    body = (await client.get("/api/live/smoke-test", headers=auth_headers)).json()
    assert body["config"]["enabled"] is False and body["config"]["max_sol"] is None and body["runs"] == []
    assert any("ENABLED is false" in p for p in body["config"]["problems"])
    r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json=ARM)
    assert r.status_code == 409 and r.json()["detail"]["stage"] == "NOT_ARMED"
    assert "live_smoke.arm_refused" in await _audits(app)
    async with app.state.db_session_factory() as s:
        assert (await s.execute(select(LiveSmokeTest))).first() is None


async def test_password_and_phrase_are_both_required(app, client, auth_headers):
    r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json={**ARM, "password": "wrong"})
    assert r.status_code == 403 and "live_smoke.password_rejected" in await _audits(app)
    r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json={**ARM, "confirm": "yes"})
    assert r.status_code == 422


async def test_armed_with_every_switch_then_cancelled(app, client, auth_headers):
    saved = app.state.settings
    app.state.settings = saved.model_copy(update={
        "TRADING_ENABLED": True, "LIVE_TRADING_ENABLED": True, "PAPER_TRADING": False, "LIVE_SMOKE_TEST_ENABLED": True,
        "LIVE_SMOKE_TEST_MAX_SOL": Decimal("0.02"), "LIVE_SMOKE_TEST_MAX_TRADES": 1})
    try:
        redis = app.state.redis
        r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json=ARM)
        assert r.status_code == 409 and r.json()["detail"]["stage"] == "EXECUTION_ROUTE_UNAVAILABLE"  # no worker yet
        await redis.set(live_trading.READY_KEY, json.dumps({"status": "ready", "min_sol_reserve": "0.05",
                                                            "wallet_max_age_seconds": "120"}))
        await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": "0.3", "at": datetime.now(timezone.utc).isoformat()}))
        r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json={**ARM, "max_sol": "0.05"})
        assert r.status_code == 409 and "at most LIVE_SMOKE_TEST_MAX_SOL" in r.json()["detail"]["reason"]
        r = await client.post("/api/live/smoke-test/arm", headers=auth_headers, json=ARM)
        assert r.status_code == 200 and r.json()["status"] == "ARMED" and r.json()["max_sol"] == "0.02"
        run_id = r.json()["id"]
        body = (await client.get("/api/live/smoke-test", headers=auth_headers)).json()
        assert body["armed"] == run_id and body["trades_used"] == 0
        assert (await client.post("/api/live/smoke-test/arm", headers=auth_headers, json=ARM)).status_code == 409
        c = await client.post(f"/api/live/smoke-test/{run_id}/cancel", headers=auth_headers)
        assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
        assert "live_smoke.armed" in await _audits(app) and "live_smoke.cancelled" in await _audits(app)
    finally:
        app.state.settings = saved


async def test_wallets_keep_live_and_paper_apart_and_show_no_secret(app, client, auth_headers):
    pubkey = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    await app.state.redis.set(live_trading.WALLET_KEY, json.dumps({
        "sol": "0.083", "at": datetime.now(timezone.utc).isoformat(), "pubkey": pubkey, "tokens": 2,
        "valuation": {"holdings": [
            {"mint": "M" * 44, "quantity": "1000", "price_sol": "0.00001", "value_sol": "0.01", "value_usd": None,
             "price_source": "pump.fun bonding curve (last stream trade)", "price_at": "t", "status": "VALUED"},
            {"mint": "N" * 44, "quantity": "5", "price_sol": None, "value_sol": None, "status": "VALUATION UNAVAILABLE",
             "reason": "canonical PumpSwap pool does not exist"}],
            "total_sol": "0.01", "valued_count": 1, "unvalued_count": 1, "more_not_shown": 0, "sol_usd": None}}))
    body = (await client.get("/api/live/wallets", headers=auth_headers)).json()
    live, paper = body["live"], body["paper"]
    assert live["label"] == "LIVE" and paper["label"] == "PAPER"
    assert live["address"] == "9xQe…VFin" and pubkey not in json.dumps(body)
    assert live["sol"] == "0.083" and Decimal(live["available_sol"]) == Decimal("0.033")  # 0.05 reserve
    assert live["holdings"][1]["status"] == "VALUATION UNAVAILABLE" and live["total_is_complete"] is False
    assert Decimal(live["total_estimated_sol"]) == Decimal("0.093")
    assert paper["currency"] == "SOL" and "available_balance" in paper and "unrealized_pnl" in paper
    assert "private" not in json.dumps(body).lower()
