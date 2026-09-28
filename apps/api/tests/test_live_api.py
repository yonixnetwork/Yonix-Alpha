"""Live execution API: preflight reporting, runtime settings, orders and
reconciliation. The environment locks stay closed here (as in production
until the operator opens them); nothing can switch live trading on."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core import live_trading
from yonixalpha_core.db.models import AuditLog, ExecutionOrder, ReconciliationEvent

pytestmark = pytest.mark.asyncio

ENDPOINTS = ["/api/live/status", "/api/live/settings", "/api/live/orders", "/api/live/reconciliation", "/api/live/positions"]


async def test_live_endpoints_require_auth(client):
    for url in ENDPOINTS:
        assert (await client.get(url)).status_code == 401, url


async def test_status_reports_every_blocker_and_no_secrets(client, auth_headers):
    r = await client.get("/api/live/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["permitted"] is False and body["ready"] is False
    assert body["locks"] == {"TRADING_ENABLED": False, "LIVE_TRADING_ENABLED": False, "PAPER_TRADING": True}
    assert "locks" in body["not_ready_reason"]
    assert body["verification"] == "IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION"
    checks = {c["check"]: c["ok"] for c in body["checks"]}
    assert checks["environment locks"] is False and checks["order worker"] is False
    assert "PRIVATE" not in json.dumps(body).upper().replace("WALLET_PRIVATE_KEY NOT SET", "")


async def test_worker_state_and_wallet_sync_are_reported(app, client, auth_headers):
    redis = app.state.redis
    await redis.set(live_trading.READY_KEY, json.dumps({"status": "disabled", "reason": "environment locks closed"}))
    await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": "1.5", "at": datetime.now(timezone.utc).isoformat()}))
    body = (await client.get("/api/live/status", headers=auth_headers)).json()
    assert body["worker"]["status"] == "disabled" and body["wallet_sync"]["sol"] == "1.5"


async def test_settings_are_validated_merged_and_audited(app, client, auth_headers):
    r = await client.get("/api/live/settings", headers=auth_headers)
    assert r.json()["settings"]["entry_slippage_pct"] == "10"
    ok = await client.put("/api/live/settings", headers=auth_headers, json={"entry_slippage_pct": "7.5"})
    assert ok.status_code == 200 and ok.json()["settings"]["entry_slippage_pct"] == "7.5"
    assert ok.json()["settings"]["exit_slippage_pct"] == "25"  # untouched keys kept
    for bad in ({"entry_slippage_pct": "80"}, {"priority_fee_sol": "0.005", "max_priority_fee_sol": "0.001"},
                {"nope": "1"}, {"min_sol_reserve": "abc"}):
        assert (await client.put("/api/live/settings", headers=auth_headers, json=bad)).status_code == 422, bad
    assert (await client.get("/api/live/settings", headers=auth_headers)).json()["settings"]["entry_slippage_pct"] == "7.5"
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert kinds.count("live_settings.updated") == 1


async def test_orders_and_reconciliation_are_listed(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        o = ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="M" * 32, provider="pumpportal_local", route="pump",
                           amount="0.1", amount_kind="sol", slippage_pct=10, priority_fee_sol=0, status="FAILED",
                           idempotency_key="k1", error="simulation failed", result={"fill": None})
        s.add(o)
        await s.flush()
        s.add(ReconciliationEvent(kind="unknown_holding", severity="info", mint="X" * 32, detail={"amount_raw": 5}))
        await s.commit()
    orders = (await client.get("/api/live/orders?status=FAILED", headers=auth_headers)).json()
    assert len(orders) == 1 and orders[0]["error"] == "simulation failed" and orders[0]["fill"] is None
    assert (await client.get(f"/api/live/orders/{orders[0]['id']}", headers=auth_headers)).json()["status"] == "FAILED"
    ev = (await client.get("/api/live/reconciliation", headers=auth_headers)).json()
    assert [e["kind"] for e in ev] == ["unknown_holding"]


async def test_word_filters_support_scopes_actions_and_match_types(client, auth_headers):
    ok = await client.post("/api/control/blacklist", headers=auth_headers,
                           json={"scope": "FRESH", "field": "name", "match_type": "word", "value": "rug", "action": "BLOCK"})
    assert ok.status_code == 201 and ok.json()["action"] == "BLOCK" and ok.json()["scope"] == "FRESH"
    allow = await client.post("/api/control/blacklist", headers=auth_headers,
                              json={"scope": "MIGRATED", "field": "any", "match_type": "exact", "value": "rugby", "action": "ALLOW"})
    assert allow.status_code == 201
    regex = await client.post("/api/control/blacklist", headers=auth_headers,
                              json={"scope": "GLOBAL", "field": "symbol", "match_type": "regex", "value": "^(a+)+$"})
    assert regex.status_code == 422  # catastrophic-backtracking shape refused
    rid = ok.json()["id"]
    edited = await client.put(f"/api/control/blacklist/{rid}", headers=auth_headers,
                              json={"scope": "GLOBAL", "field": "metadata", "match_type": "substring", "value": "honeypot"})
    assert edited.status_code == 200 and edited.json()["field"] == "metadata" and edited.json()["scope"] == "GLOBAL"
    assert (await client.post("/api/control/blacklist", headers=auth_headers,
                              json={"field": "name", "match_type": "fuzzy", "value": "x"})).status_code == 422


async def test_paper_execution_failure_settings(app, client, auth_headers):
    assert (await client.get("/api/paper/execution-settings")).status_code == 401
    r = (await client.get("/api/paper/execution-settings", headers=auth_headers)).json()
    assert (r["entry_pct"], r["exit_pct"], r["entry_source"]) == ("0", "0", "operator setting")
    assert r["measured"]["BUY"]["orders"] == 0 and r["measured"]["BUY"]["failure_pct"] is None
    ok = await client.put("/api/paper/execution-settings", headers=auth_headers, json={"exit_failure_pct": "8"})
    assert ok.status_code == 200 and ok.json()["exit_pct"] == "8" and ok.json()["entry_pct"] == "0"
    for bad in ({"exit_failure_pct": "75"}, {"entry_failure_pct": "x"}, {"use_measured_live_rates": "no"}, {"x": 1}):
        assert (await client.put("/api/paper/execution-settings", headers=auth_headers, json=bad)).status_code == 422, bad
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert kinds.count("paper_execution.updated") == 1


async def test_rent_reclaim_needs_auth_confirmation_and_a_ready_worker(app, client, auth_headers):
    assert (await client.post("/api/live/reclaim-rent", json={"confirm": True})).status_code == 401
    assert (await client.post("/api/live/reclaim-rent", headers=auth_headers, json={"confirm": False})).status_code == 422
    r = await client.post("/api/live/reclaim-rent", headers=auth_headers, json={"confirm": True})
    assert r.status_code == 409 and "locks" in r.json()["detail"]  # locks closed: nothing is queued

    saved = app.state.settings
    app.state.settings = saved.model_copy(update={"TRADING_ENABLED": True, "LIVE_TRADING_ENABLED": True, "PAPER_TRADING": False})
    try:
        redis = app.state.redis
        await redis.set(live_trading.READY_KEY, json.dumps({"status": "ready", "min_sol_reserve": "0.05",
                                                            "wallet_max_age_seconds": "120"}))
        await redis.set(live_trading.WALLET_KEY, json.dumps({
            "sol": "0.08", "at": datetime.now(timezone.utc).isoformat(),
            "empty_token_accounts": {"count": 5, "rent_sol": "0.0075692"}}))
        r = await client.post("/api/live/reclaim-rent", headers=auth_headers, json={"confirm": True})
        assert r.status_code == 200 and r.json()["status"] == "PENDING"
        assert (await client.post("/api/live/reclaim-rent", headers=auth_headers, json={"confirm": True})).status_code == 409
        live = (await client.get("/api/live/wallets", headers=auth_headers)).json()["live"]
        assert live["empty_token_accounts"] == {"count": 5, "rent_sol": "0.0075692"}
        assert live["rent_reclaims"][0]["status"] == "PENDING" and live["rent_reclaims"][0]["scope"] == "ALL"
    finally:
        app.state.settings = saved
    async with app.state.db_session_factory() as s:
        order = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.side == "RENT"))).scalar_one()
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert order.limits["mints"] is None and "live.rent_reclaim_requested" in kinds


async def test_auto_reclaim_setting_is_a_validated_switch(client, auth_headers):
    assert (await client.get("/api/live/settings", headers=auth_headers)).json()["settings"]["auto_reclaim_rent"] == "true"
    ok = await client.put("/api/live/settings", headers=auth_headers, json={"auto_reclaim_rent": "false"})
    assert ok.status_code == 200 and ok.json()["settings"]["auto_reclaim_rent"] == "false"
    assert (await client.put("/api/live/settings", headers=auth_headers, json={"auto_reclaim_rent": "maybe"})).status_code == 422
