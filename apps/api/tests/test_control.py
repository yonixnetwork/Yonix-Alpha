from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, RiskAssessment, Token, TradingCandidate

pytestmark = pytest.mark.asyncio

ENDPOINTS = ["/api/control/settings/GLOBAL", "/api/control/modes", "/api/control/blacklist", "/api/control/rules",
             "/api/control/assessments", "/api/control/paper/accounts", "/api/control/pipeline"]


async def test_every_endpoint_requires_auth(client):
    for url in ENDPOINTS:
        assert (await client.get(url)).status_code == 401, url


async def test_settings_roundtrip_versioned_and_validated(client, auth_headers):
    r = await client.get("/api/control/settings/solana_fresh", headers=auth_headers)
    assert r.status_code == 200 and r.json()["effective"]["min_stop_pct"] == "0.10" and r.json()["source"]["scope"] == "DEFAULT"

    r = await client.put("/api/control/settings/solana_fresh", headers=auth_headers,
                         json={"settings": {"max_position_size_quote": "0.5"}, "note": "smaller"})
    assert r.status_code == 200 and r.json()["version"]["version"] == 1
    r = await client.get("/api/control/settings/solana_fresh", headers=auth_headers)
    assert r.json()["effective"]["max_position_size_quote"] == "0.5" and r.json()["source"]["version"] == 1

    bad = await client.put("/api/control/settings/solana_fresh", headers=auth_headers,
                           json={"settings": {"min_stop_pct": "0.5", "max_stop_pct": "0.2"}})
    assert bad.status_code == 422 and "min_stop_pct must be below max_stop_pct" in bad.json()["detail"]["errors"]
    assert (await client.get("/api/control/settings/nope", headers=auth_headers)).status_code == 404
    hist = await client.get("/api/control/settings/solana_fresh/history", headers=auth_headers)
    assert [v["version"] for v in hist.json()] == [1]


async def test_live_mode_is_refused_while_env_locks_are_closed(client, auth_headers):
    r = await client.get("/api/control/modes", headers=auth_headers)
    assert r.json()["global_mode"] == "PAPER" and r.json()["env"]["live_permitted"] is False
    assert (await client.put("/api/control/modes/global", json={"mode": "LIVE"}, headers=auth_headers)).status_code == 409
    r = await client.put("/api/control/modes/global", json={"mode": "MANUAL"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["global_mode"] == "MANUAL"
    r = await client.put("/api/control/modes/strategy/solana_fresh", json={"mode": "OFF"}, headers=auth_headers)
    assert r.json()["strategies"]["solana_fresh"] == "OFF"
    assert (await client.put("/api/control/modes/strategy/solana_fresh", json={"mode": "YOLO"},
                             headers=auth_headers)).status_code == 422


async def test_blacklist_crud_validates_and_audits(app, client, auth_headers):
    r = await client.post("/api/control/blacklist", headers=auth_headers,
                          json={"field": "symbol", "match_type": "pattern", "value": "*SCAM*", "reason": "t"})
    assert r.status_code == 201
    rid = r.json()["id"]
    dup = await client.post("/api/control/blacklist", headers=auth_headers,
                            json={"field": "symbol", "match_type": "pattern", "value": "*scam*"})
    assert dup.status_code == 409
    wild = await client.post("/api/control/blacklist", headers=auth_headers,
                             json={"field": "name", "match_type": "pattern", "value": "**"})
    assert wild.status_code == 422
    assert (await client.patch(f"/api/control/blacklist/{rid}", json={"enabled": False}, headers=auth_headers)).json()["enabled"] is False
    assert (await client.delete(f"/api/control/blacklist/{rid}", headers=auth_headers)).status_code == 204
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert {"blacklist.added", "blacklist.toggled", "blacklist.deleted"} <= set(kinds)


async def test_custom_rules_only_accept_known_fields(client, auth_headers):
    fields = (await client.get("/api/control/rules/fields", headers=auth_headers)).json()
    assert "top10_share" in fields
    ok = await client.post("/api/control/rules", headers=auth_headers,
                           json={"name": "concentration", "field": "top10_share", "op": ">", "threshold": "0.3", "action": "WAIT"})
    assert ok.status_code == 201
    bad = await client.post("/api/control/rules", headers=auth_headers,
                            json={"name": "x", "field": "moon_potential", "op": ">", "threshold": "1", "action": "ALLOW"})
    assert bad.status_code == 422


async def _seed_assessment(app, decision="REQUIRE_MANUAL_APPROVAL", approval="PENDING", age=timedelta(seconds=5)):
    async with app.state.db_session_factory() as s:
        token = Token(mint_address=f"M{datetime.now().timestamp()}", first_seen_source="t")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state="observing", state_history=[])
        s.add(cand)
        await s.flush()
        row = RiskAssessment(idempotency_key=str(cand.id), candidate_id=cand.id, engine="solana_fresh", strategy="fresh",
                             asset_id=token.mint_address, symbol="T", decision=decision, status_label=decision,
                             executable=False, execution_target="NONE", overall_risk="HIGH", risk_engine_version="2.0.0",
                             assessment={"reasons": ["creator wallet sold"], "plan": {"position_size": {"value": "0.2"}}},
                             approval_state=approval, evaluated_at=datetime.now(timezone.utc) - age)
        s.add(row)
        await s.commit()
        return row.id, cand.id


async def test_assessment_list_detail_and_approval(app, client, auth_headers):
    aid, _ = await _seed_assessment(app)
    lst = await client.get("/api/control/assessments", params={"approval_state": "PENDING"}, headers=auth_headers)
    assert lst.json()["total"] == 1 and lst.json()["items"][0]["position_size"] == "0.2"
    r = await client.post(f"/api/control/assessments/{aid}/approve", headers=auth_headers)
    assert r.status_code == 200 and r.json()["approval_state"] == "APPROVED"
    again = await client.post(f"/api/control/assessments/{aid}/approve", headers=auth_headers)
    assert again.status_code == 409
    detail = await client.get(f"/api/control/assessments/{aid}", headers=auth_headers)
    assert [t["event_type"] for t in detail.json()["timeline"]] == ["manual_approval"]


async def test_stale_assessment_cannot_be_approved(app, client, auth_headers):
    aid, _ = await _seed_assessment(app, age=timedelta(minutes=30))
    r = await client.post(f"/api/control/assessments/{aid}/approve", headers=auth_headers)
    assert r.status_code == 409


async def test_decline_rejects_candidate(app, client, auth_headers):
    aid, cid = await _seed_assessment(app)
    assert (await client.post(f"/api/control/assessments/{aid}/decline", headers=auth_headers)).status_code == 200
    async with app.state.db_session_factory() as s:
        assert (await s.get(TradingCandidate, cid)).state == "rejected"


async def test_paper_accounts_and_reset(client, auth_headers):
    r = await client.get("/api/control/paper/accounts", headers=auth_headers)
    names = {a["name"]: a for a in r.json()}
    assert names["solana"]["equity"] == "10.000000000000000000" and names["solana"]["closed_positions"] == 0
    r = await client.post("/api/control/paper/accounts/solana/reset", json={"starting_balance": "5", "confirm": "solana"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["cash_balance"] == "5"
    assert (await client.post("/api/control/paper/accounts/solana/reset", json={"starting_balance": "-1", "confirm": "solana"},
                              headers=auth_headers)).status_code == 422


async def test_pipeline_health_reports_absence_honestly(client, auth_headers):
    r = await client.get("/api/control/pipeline", headers=auth_headers)
    body = r.json()
    assert r.status_code == 200 and body["stream"]["heartbeat"] is None and body["last_assessment_at"] is None


async def test_creator_and_migrated_liquidity_settings_are_dashboard_editable(client, auth_headers):
    r = await client.get("/api/control/settings/solana_migration", headers=auth_headers)
    body = r.json()
    assert body["effective"]["min_creator_tokens_created"] == 5 and body["effective"]["creator_history_check"] is True
    assert body["effective"]["creator_below_threshold_action"] == "WARN"
    assert body["effective"]["min_migrated_liquidity_usd"] == "10000" and body["effective"]["migrated_liquidity_check"] is True
    assert body["enums"]["creator_below_threshold_action"] == ["WARN", "REDUCE_SIZE", "REQUIRE_MANUAL_APPROVAL", "REJECT"]
    r = await client.put("/api/control/settings/solana_migration", headers=auth_headers,
                         json={"settings": {**body["effective"], "min_creator_tokens_created": 8,
                                            "creator_below_threshold_action": "REJECT", "min_migrated_liquidity_usd": "15000"}})
    assert r.status_code == 200, r.text
    eff = (await client.get("/api/control/settings/solana_migration", headers=auth_headers)).json()["effective"]
    assert eff["min_creator_tokens_created"] == 8 and eff["creator_below_threshold_action"] == "REJECT"
    assert eff["min_migrated_liquidity_usd"] == "15000"
    bad = await client.put("/api/control/settings/solana_migration", headers=auth_headers,
                           json={"settings": {"creator_below_threshold_action": "NUKE"}})
    assert bad.status_code == 422


async def test_scam_name_preset_installs_editable_rules_once(app, client, auth_headers):
    assert (await client.post("/api/control/blacklist/presets/scam-names")).status_code == 401
    r = await client.post("/api/control/blacklist/presets/scam-names", headers=auth_headers)
    assert r.status_code == 200 and r.json()["added"] == 19 + 11 * 2 and r.json()["skipped"] == 0
    again = await client.post("/api/control/blacklist/presets/scam-names", headers=auth_headers)
    assert again.json() == {"added": 0, "skipped": 41}
    rules = (await client.get("/api/control/blacklist", headers=auth_headers)).json()
    assert {"name", "symbol"} == {x["field"] for x in rules if x["match_type"] == "exact"}
    assert any(x["value"] == "spacex" and x["match_type"] == "substring" for x in rules)
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert "blacklist.preset_added" in kinds
