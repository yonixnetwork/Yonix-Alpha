import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from yonixalpha_core import events
from yonixalpha_core.db.models import (
    AuditLog,
    MLFeatureSnapshot,
    ModelVersion,
    Notification,
    OpportunityOutcome,
    PaperPosition,
    RiskAssessment,
    SystemEvent,
    Token,
)
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FEATURE_VERSION, SOLANA_FEATURES
from yonixalpha_core.safety import store

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

NEW_ENDPOINTS = ["/api/analytics/performance", "/api/strategies", "/api/summary",
                 "/api/notifications", "/api/notifications/prefs", "/api/system/health", "/api/system/observability",
                 "/api/ml/review", "/api/ml/predictions", "/api/ml/data-quality", "/api/ml/samples", f"/api/tokens/{MINT}",
                 "/api/paper/orders", "/api/evm/coordination-settings", "/api/evm/coordination/summary", "/api/evm/observations",
                 "/api/evm/observation-settings", "/api/evm/streams"]


async def test_new_endpoints_require_auth(client):
    for url in NEW_ENDPOINTS:
        assert (await client.get(url)).status_code == 401, url


async def test_removed_legacy_endpoints_are_gone(client, auth_headers):
    # Futures / FX / grid / Gold vs BTC / external bots were removed (archive/legacy-futures-forex-grid-2026-09-29).
    for url in ("/api/analytics/gold-btc", "/api/venues", "/api/venues/binance/market", "/api/bots", "/api/live/futures",
                "/api/strategies/meta_muse", "/api/strategies/hyperliquid_grid"):
        assert (await client.get(url, headers=auth_headers)).status_code == 404, url
    for url in ("/api/strategies/hyperliquid_grid/start", "/api/strategies/hyperliquid_grid/stop"):
        assert (await client.post(url, headers=auth_headers)).status_code in (404, 405), url
    assert (await client.put("/api/live/futures/settings", headers=auth_headers, json={})).status_code in (404, 405)


async def seed_closed(app, account: str, engine: str, pnls: list[str], strategy: str | None = None) -> None:
    async with app.state.db_session_factory() as s:
        acct = await store.get_paper_account(s, account)
        acct.reset_at = NOW - timedelta(days=1)  # trades before a reset are (correctly) excluded
        for i, pnl in enumerate(pnls):
            a = None
            if strategy:
                a = RiskAssessment(idempotency_key=f"k-{engine}-{strategy}-{i}", engine=engine, strategy=strategy, asset_id="ETHUSDT",
                                   symbol="ETHUSDT", decision="EXECUTE", status_label="x", executable=True, execution_target="PAPER",
                                   overall_risk="LOW", risk_engine_version="1", assessment={}, approval_state="NONE", evaluated_at=NOW)
                s.add(a)
                await s.flush()
            s.add(PaperPosition(account_id=acct.id, assessment_id=a.id if a else None, engine=engine, symbol="ETHUSDT",
                                asset_id="ETHUSDT", provider="paper", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                                take_profit=[], status="closed", realized_pnl=Decimal(pnl), fees_paid_quote=Decimal("0.1"),
                                entry_at=NOW - timedelta(hours=2 + i), exit_at=NOW - timedelta(hours=1, minutes=i),
                                plan={}))
        s.add(PaperPosition(account_id=acct.id, engine=engine, symbol="OPEN", asset_id="OPEN", provider="paper", side="LONG",
                            entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="open", entry_at=NOW))
        await s.commit()


async def test_performance_analytics_per_currency_and_excludes_open(app, client, auth_headers):
    await seed_closed(app, "solana", "solana_fresh", ["10", "-5", "0", "20"], strategy="solana_fresh")
    await seed_closed(app, "evm_bsc", "evm_bsc", ["0.5"])
    r = await client.get("/api/analytics/performance", headers=auth_headers)
    assert r.status_code == 200
    accts = {a["account"]: a for a in r.json()["accounts"]}
    assert "binance_futures" not in accts and set(accts) >= {"solana", "copy_solana", "evm_bsc", "evm_robinhood"}
    sol = accts["solana"]
    o = sol["overall"]
    assert sol["currency"] == "SOL" and sol["open_positions_not_counted"] == 1
    assert (o["trades"], o["wins"], o["losses"], o["breakeven"]) == (4, 2, 1, 1)
    assert o["win_rate"] == 0.5 and Decimal(o["profit_factor"]) == 6 and Decimal(o["expectancy"]) == Decimal("6.25")
    assert Decimal(o["avg_win"]) == 15 and Decimal(o["avg_loss"]) == -5
    assert "solana_fresh" in sol["by_strategy"] and "solana" in sol["by_venue"]
    assert accts["evm_bsc"]["currency"] == "BNB" and accts["evm_bsc"]["overall"]["trades"] == 1
    only = await client.get("/api/analytics/performance?account=solana&strategy=nothing", headers=auth_headers)
    assert only.json()["accounts"][0]["overall"]["trades"] == 0
    assert (await client.get("/api/analytics/performance?account=binance_futures", headers=auth_headers)).status_code == 404


async def test_strategies_catalog_config_validation_and_modes(client, auth_headers):
    r = await client.get("/api/strategies", headers=auth_headers)
    names = {s["name"]: s for s in r.json()}
    assert set(names) == {"solana_fresh", "solana_migration", "solana_momentum"}
    ok = await client.put("/api/strategies/solana_fresh/config", headers=auth_headers,
                          json={"config": {"manual_stop_loss_pct": "0.2"}})
    assert ok.status_code == 200 and ok.json()["config"]["manual_stop_loss_pct"] == "0.2"
    bad = await client.put("/api/strategies/solana_fresh/config", headers=auth_headers,
                           json={"config": {"manual_stop_loss_pct": "2", "secret": "x"}})
    errors = bad.json()["detail"]["errors"]
    assert bad.status_code == 422 and any("unknown" in e for e in errors) and any("manual_stop_loss_pct" in e for e in errors)

    r = await client.put("/api/strategies/solana_momentum/mode", headers=auth_headers, json={"mode": "OFF"})
    assert r.status_code == 200 and r.json()["mode"] == "OFF" and r.json()["effective_mode"] == "OFF"
    assert (await client.put("/api/strategies/solana_momentum/mode", headers=auth_headers, json={"mode": "NOPE"})).status_code == 422
    assert (await client.put("/api/strategies/meta_muse/mode", headers=auth_headers, json={"mode": "AUTO"})).status_code == 404


async def test_health_states_from_evidence(app, client, auth_headers):
    redis = app.state.redis
    await events.heartbeat(redis, "decision-engine", detail={"venues": {"jupiter": {
        "last_ok_at": NOW.isoformat(), "consecutive_failures": 0, "last_error": None, "last_error_at": None, "calls": 3}}})
    async with app.state.db_session_factory() as s:
        s.add(SystemEvent(service="ml", event_type="service_started", severity="info"))
        await s.commit()
    r = await client.get("/api/system/health", headers=auth_headers)
    c = {x["name"]: x for x in r.json()["connections"]}
    assert c["postgres"]["state"] == "CONNECTED" and c["redis"]["state"] == "CONNECTED"
    assert c["decision-engine"]["state"] == "CONNECTED"
    assert c["ml"]["state"] == "UNAVAILABLE"  # ran before, heartbeat gone
    assert c["paper-trading"]["state"] == "UNKNOWN"  # never seen
    assert c["jupiter"]["state"] == "CONNECTED"
    # SOLANA_ONLY with COPY_TRADING_ENABLED=false (the defaults): switched off on purpose, not a failure
    assert (c["data-evm"]["state"], c["copy-engine"]["state"]) == ("DISABLED", "DISABLED")
    assert c["data-evm"]["detail"].startswith("DISABLED — SOLANA_ONLY MODE")
    assert "COPY_TRADING_ENABLED=false" in c["copy-engine"]["detail"]
    for gone in ("binance", "bybit", "hyperliquid", "mt5", "binance_execution", "data-binance", "execution-futures"):
        assert gone not in c, gone
    assert r.json()["overall"] == "UNAVAILABLE"
    app.state.settings = app.state.settings.model_copy(update={"SYSTEM_PROFILE": "MULTI_CHAIN", "COPY_TRADING_ENABLED": True})
    c = {x["name"]: x for x in (await client.get("/api/system/health", headers=auth_headers)).json()["connections"]}
    assert c["data-evm"]["state"] == "UNKNOWN" and c["copy-engine"]["state"] == "UNKNOWN"  # on, never seen
    obs = await client.get("/api/system/observability", headers=auth_headers)
    assert obs.status_code == 200 and "redis_memory" in obs.json() and obs.json()["position_loop"] is None
    await redis.set("yx:pm:last_pass", json.dumps({"at": datetime.now(timezone.utc).isoformat(), "pass_ms": 40,
                                                    "interval_s": 2.0, "managed": 3, "closed": 0, "unpriced": 0}))
    pl = (await client.get("/api/system/observability", headers=auth_headers)).json()["position_loop"]
    assert pl["managed"] == 3 and pl["pass_ms"] == 40 and 0 <= pl["age_s"] < 60


async def test_disabled_services_report_not_configured_and_legacy_services_are_hidden(app, client, auth_headers):
    redis = app.state.redis
    await events.heartbeat(redis, "data-solana", status="disabled", detail={"reason": "SOLANA_RPC_URL not set"})
    c = {x["name"]: x for x in (await client.get("/api/system/health", headers=auth_headers)).json()["connections"]}
    assert c["data-solana"]["state"] == "NOT CONFIGURED" and "SOLANA_RPC_URL not set" in c["data-solana"]["detail"]
    # --profile legacy services: listed only while they actually run
    assert "engine-solana-momentum" not in c and "engine-solana-migration" not in c
    await events.heartbeat(redis, "engine-solana-momentum")
    c = {x["name"]: x for x in (await client.get("/api/system/health", headers=auth_headers)).json()["connections"]}
    assert c["engine-solana-momentum"]["state"] == "CONNECTED"


async def test_summary_topbar(app, client, auth_headers):
    await seed_closed(app, "evm_bsc", "evm_bsc", ["0.01"])
    r = await client.get("/api/summary", headers=auth_headers)
    b = r.json()
    bsc = next(a for a in b["accounts"] if a["name"] == "evm_bsc")
    assert bsc["currency"] == "BNB" and Decimal(bsc["realized_pnl_today"]) in (Decimal("0.01"), Decimal(0))
    assert not {a["name"] for a in b["accounts"]} & {"binance_futures", "bybit_futures", "hyperliquid", "mt5"}
    assert b["open_positions"] == 1 and b["env"]["live_permitted"] is False and b["global_mode"] == "PAPER"
    assert b["system_status"] in ("CONNECTED", "DEGRADED", "STALE", "UNAVAILABLE", "UNKNOWN")


async def test_notifications_feed_and_prefs(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        n = await events.notify(s, None, None, "entry", "Paper entry: X LONG")
        await s.commit()
        nid = str(n.id)
    r = await client.get("/api/notifications?unread=true", headers=auth_headers)
    assert r.json()["unread"] == 1 and r.json()["items"][0]["kind"] == "entry"
    assert (await client.post(f"/api/notifications/{nid}/read", headers=auth_headers)).json()["read_at"]
    assert (await client.get("/api/notifications", headers=auth_headers)).json()["unread"] == 0
    prefs = (await client.get("/api/notifications/prefs", headers=auth_headers)).json()
    assert prefs["entry"]["telegram"] is True and prefs["tp1"]["telegram"] is False
    r = await client.put("/api/notifications/prefs", headers=auth_headers, json={"entry": {"telegram": False}})
    assert r.json()["entry"]["telegram"] is False
    assert (await client.put("/api/notifications/prefs", headers=auth_headers, json={"nope": {"telegram": True}})).status_code == 422
    async with app.state.db_session_factory() as s:
        assert await events.telegram_enabled_for(s, "entry") is False


async def test_position_controls_and_trade_details(app, client, auth_headers):
    await seed_closed(app, "solana", "solana_fresh", [])
    async with app.state.db_session_factory() as s:
        pid = str((await s.execute(select(PaperPosition.id).where(PaperPosition.status == "open"))).scalar_one())
    r = await client.post(f"/api/paper/positions/{pid}/pause", headers=auth_headers)
    assert r.status_code == 200 and r.json()["position"]["management_paused"] is True and "stop loss is still enforced" in r.json()["note"]
    assert (await client.post(f"/api/paper/positions/{pid}/resume", headers=auth_headers)).json()["position"]["management_paused"] is False
    assert (await client.post(f"/api/paper/positions/{pid}/exit", headers=auth_headers)).json()["position"]["exit_requested"] is True
    assert (await client.post(f"/api/paper/positions/{pid}/exit", headers=auth_headers)).status_code == 409
    d = await client.get(f"/api/paper/positions/{pid}", headers=auth_headers)
    assert [t["type"] for t in d.json()["timeline"]] == ["operator_pause", "operator_resume", "operator_exit"]
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert "paper_position.exit" in kinds


async def test_ignore_and_rule_edits_and_reset_confirmation(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        a = RiskAssessment(idempotency_key="pending-1", engine="solana_fresh", strategy="solana_fresh", asset_id=MINT,
                           symbol="TEST", decision="REQUIRE_MANUAL_APPROVAL", status_label="x", executable=False,
                           execution_target="NONE", overall_risk="MODERATE", risk_engine_version="1", assessment={},
                           approval_state="PENDING", evaluated_at=NOW)
        s.add(a)
        await s.commit()
        aid = str(a.id)
    r = await client.post(f"/api/control/assessments/{aid}/ignore", headers=auth_headers)
    assert r.status_code == 200 and r.json()["approval_state"] == "IGNORED"
    assert (await client.post(f"/api/control/assessments/{aid}/approve", headers=auth_headers)).status_code == 409

    rid = (await client.post("/api/control/blacklist", headers=auth_headers,
                             json={"field": "symbol", "match_type": "exact", "value": "SCAM"})).json()["id"]
    r = await client.put(f"/api/control/blacklist/{rid}", headers=auth_headers,
                         json={"field": "symbol", "match_type": "pattern", "value": "*RUG*", "reason": "edit"})
    assert r.status_code == 200 and r.json()["value"] == "*RUG*"
    assert (await client.put(f"/api/control/blacklist/{rid}", headers=auth_headers,
                             json={"field": "symbol", "match_type": "pattern", "value": "**"})).status_code == 422
    rule = (await client.post("/api/control/rules", headers=auth_headers, json={
        "name": "min liq", "field": "liquidity_quote", "op": "<", "threshold": "5", "action": "REJECT"})).json()
    r = await client.put(f"/api/control/rules/{rule['id']}", headers=auth_headers, json={
        "name": "min liq", "field": "liquidity_quote", "op": "<", "threshold": "10", "action": "REJECT"})
    assert r.status_code == 200 and r.json()["threshold"] == "10"

    wrong = await client.post("/api/control/paper/accounts/solana/reset", headers=auth_headers,
                              json={"starting_balance": "5", "confirm": "evm_bsc"})
    assert wrong.status_code == 422


async def test_token_details(app, client, auth_headers):
    assert (await client.get(f"/api/tokens/{MINT}", headers=auth_headers)).status_code == 404
    assert (await client.get("/api/tokens/not-a-mint!", headers=auth_headers)).status_code in (404, 422)
    async with app.state.db_session_factory() as s:
        s.add(Token(mint_address=MINT, symbol="TEST", first_seen_source="pump_stream"))
        await s.commit()
    r = await client.get(f"/api/tokens/{MINT}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["token"]["symbol"] == "TEST"


async def test_ml_review_promote_retire(app, client, auth_headers):
    artifact = b"not loaded by the API"  # review/promote/retire work on rows, never unpickle
    async with app.state.db_session_factory() as s:
        good = ModelVersion(name="gate_solana_fresh", version=1, status="challenger", feature_names=SOLANA_FEATURES,
                            training_sample_count=100, metrics={"promotable": True, "challenger": {"auc": 0.7}, "reference": {"x": 1}},
                            artifact=artifact)
        weak = ModelVersion(name="gate_solana_fresh", version=2, status="challenger", feature_names=SOLANA_FEATURES,
                            training_sample_count=100, metrics={"promotable": False}, artifact=artifact)
        s.add_all([good, weak])
        s.add(MLFeatureSnapshot(symbol="TEST", engine="solana_fresh", features={}, feature_version=FEATURE_VERSION,
                                label=1, quality_status="ok"))
        await s.commit()
        good_id, weak_id = str(good.id), str(weak.id)
    review = {m["model"]: m for m in (await client.get("/api/ml/review", headers=auth_headers)).json()}
    assert "gate_futures" not in review  # model of the removed futures engines
    gf = review["gate_solana_fresh"]
    assert gf["mode"] == "RULES ONLY" and gf["champion"] is None and gf["samples"]["labeled"] == 1
    assert "reference" not in gf["challenger"]["metrics"]
    assert (await client.post(f"/api/ml/models/{weak_id}/promote", headers=auth_headers, json={})).status_code == 409
    r = await client.post(f"/api/ml/models/{good_id}/promote", headers=auth_headers, json={"note": "reviewed"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    await app.state.redis.set(f"{DRIFT_FLAG_PREFIX}gate_solana_fresh", "1")
    review = {m["model"]: m for m in (await client.get("/api/ml/review", headers=auth_headers)).json()}
    assert review["gate_solana_fresh"]["mode"] == "ML IGNORED (drift)"
    assert (await client.post("/api/ml/gate_solana_fresh/retire", headers=auth_headers, json={"reason": "drift"})).status_code == 200
    assert (await client.post("/api/ml/gate_solana_fresh/retire", headers=auth_headers, json={"reason": "again"})).status_code == 404
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert "ml.promoted" in kinds and "ml.champion_retired" in kinds
    for url in ("/api/ml/predictions", "/api/ml/data-quality", "/api/ml/samples?labeled=true"):
        assert (await client.get(url, headers=auth_headers)).status_code == 200, url


async def test_ledger_review_and_shadow_models_are_review_only(app, client, auth_headers):
    now = datetime.now(timezone.utc)
    async with app.state.db_session_factory() as s:
        shadow = ModelVersion(name="shadow_p_upside_50", version=1, status="shadow", feature_names=["x"],
                              training_sample_count=300, metrics={"target": "P_UPSIDE_50", "kind": "binary",
                                                                  "holdout": {"roc_auc": 0.61, "pr_auc": 0.4}},
                              artifact=b"not loaded by the API")
        s.add(shadow)
        s.add(OpportunityOutcome(key="lr1", mint="LR1", engine="solana_fresh", stage="GATE", decision="REJECT", traded=False,
                                 reasons=["MANIPULATION_HIGH: x"], decided_at=now - timedelta(hours=2), snapshot={}, horizons={},
                                 status="COMPLETE", analysis={"counterfactual": {"classification": "MISSED_WIN"}},
                                 path={"T+5m": {"change_pct": 40.0}}, ml_shadow={"scores": {"P_UPSIDE_50": {"value": 0.3}}}))
        await s.commit()
        shadow_id = str(shadow.id)
    r = (await client.get("/api/ml/ledger-review?days=1", headers=auth_headers)).json()
    assert r["counts"]["missed_win"] == 1 and r["missed_win_buckets"]["rejecting_rule"] == {"MANIPULATION_HIGH": 1}
    assert r["shadow_models"][0]["target"] == "P_UPSIDE_50" and r["shadow_models"][0]["holdout"]["roc_auc"] == 0.61
    items = (await client.get("/api/ml/opportunities?category=missed_win", headers=auth_headers)).json()["items"]
    assert items[0]["path"]["T+5m"]["change_pct"] == 40.0 and items[0]["ml_shadow"]["scores"]["P_UPSIDE_50"]["value"] == 0.3
    assert (await client.get("/api/ml/opportunities?category=bogus", headers=auth_headers)).status_code == 422
    # A shadow model can never be promoted into the decision path.
    assert (await client.post(f"/api/ml/models/{shadow_id}/promote", headers=auth_headers, json={})).status_code == 409


async def test_no_endpoint_leaks_secrets(app, client, auth_headers):
    body = json.dumps([(await client.get(u, headers=auth_headers)).text for u in ("/api/summary", "/api/system/health", "/api/strategies")])
    for key in ("API_SECRET", "api_secret", "JWT_SECRET", "password"):
        assert key not in body
    async with app.state.db_session_factory() as s:
        assert (await s.execute(select(Notification))).first() is None


async def test_positions_and_decisions_filter_by_strategy(app, client, auth_headers):
    await seed_closed(app, "solana", "solana_fresh", ["1"], strategy="solana_fresh")
    await seed_closed(app, "solana", "solana_momentum", ["2", "3"], strategy="solana_momentum")
    r = await client.get("/api/paper/positions?strategy=solana_momentum&status=closed", headers=auth_headers)
    assert r.json()["total"] == 2
    r = await client.get("/api/control/assessments?strategy=solana_fresh", headers=auth_headers)
    assert r.json()["total"] == 1


async def test_ml_readiness_reports_rules_only_on_an_empty_system(client, auth_headers):
    r = (await client.get("/api/ml/readiness", headers=auth_headers)).json()
    assert r["ml_contributing"] is False and r["states"][0] == "INSUFFICIENT_DATA"
    names = {m["model"]: m for m in r["models"]}
    assert set(names) == {"gate_solana_fresh", "gate_solana_momentum", "gate_solana_migration"}
    assert all(m["state"] == "INSUFFICIENT_DATA" and m["samples"]["needed"] == 50 for m in names.values())
    assert r["dataset"]["decisions"] == 0 and "never influence" in r["dataset"]["note"]


async def test_sol_usd_is_served_only_while_fresh(app, client, auth_headers):
    from yonixalpha_core.solana import sol_price
    r = (await client.get("/api/tokens/sol-usd", headers=auth_headers)).json()
    assert r["price"] is None and "no SOL/USD" in r["reason"]
    now = datetime.now(timezone.utc)
    await sol_price.store(app.state.redis, Decimal("150.25"), "Jupiter quote 1 SOL -> USDC", now)
    r = (await client.get("/api/tokens/sol-usd", headers=auth_headers)).json()
    assert r["price"] == "150.25" and r["source"].startswith("Jupiter") and r["age_s"] < 60
    await sol_price.store(app.state.redis, Decimal("150.25"), "Jupiter quote 1 SOL -> USDC", now - timedelta(minutes=10))
    assert (await client.get("/api/tokens/sol-usd", headers=auth_headers)).json()["price"] is None


async def test_ml_ablation_reports_not_run_then_the_stored_result(app, client, auth_headers):
    from yonixalpha_core.ml.readiness import ABLATION_KEY
    r = (await client.get("/api/ml/ablation", headers=auth_headers)).json()
    assert r["status"] == "NOT_RUN" and "200" in r["reason"]
    await app.state.redis.set(ABLATION_KEY, json.dumps({"status": "EVALUATED", "samples": 900, "targets": {}}))
    r = (await client.get("/api/ml/ablation", headers=auth_headers)).json()
    assert r["status"] == "EVALUATED" and r["samples"] == 900


async def test_launchpads_status_is_evidence_based_and_controls_are_audited(app, client, auth_headers):
    from yonixalpha_core import kill_switch
    r = (await client.get("/api/launchpads", headers=auth_headers)).json()
    lp = {x["key"]: x for x in r["launchpads"]}
    assert lp["noxa"]["status"] == "DISABLED" and "launches" in lp["noxa"]["why"]
    assert lp["fourmeme"]["status"] == "UNVERIFIED" and lp["fourmeme"]["checks"]["QUOTE"] == {"status": "NOT_RUN"}
    assert lp["pumpfun"]["checks"]["BUY"]["status"] == "FAIL"  # no confirmed LIVE buy in this empty system
    # activity health: nothing recorded in this empty system, so nothing is presented as active
    assert lp["fourmeme"]["activity_status"] == "UNVERIFIED" and lp["fourmeme"]["listed"] is False
    assert lp["fourmeme"]["trades_7d"] == 0 and lp["pumpfun"]["trades_7d"] is None  # Solana trades: not tracked, not 0
    assert lp["noxa"]["activity_status"] == "DISABLED" and "INACTIVE" in r["activity_statuses"]
    assert {x["key"] for x in (await client.get("/api/launchpads?chain=bsc", headers=auth_headers)).json()["launchpads"]} == {"fourmeme", "flap", "genius_fun"}
    # an unverified launchpad cannot be switched LIVE
    bad = await client.put("/api/controls/launchpad:fourmeme", json={"mode": "LIVE"}, headers=auth_headers)
    assert bad.status_code == 409 and "cannot be set LIVE" in bad.json()["detail"]
    ok = await client.put("/api/controls/chain:bsc", json={"enabled": False, "note": "test"}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["switches"]["chain:bsc"]["enabled"] is False
    chains = (await client.get("/api/chains", headers=auth_headers)).json()["chains"]
    assert {c["chain"]: c["enabled"] for c in chains} == {"solana": True, "bsc": False, "robinhood": True}
    assert (await client.put("/api/controls/nonsense", json={"enabled": False}, headers=auth_headers)).status_code == 422
    # emergency actions need an explicit confirmation phrase
    assert (await client.post("/api/controls/emergency-exit", json={"confirm": "yes"}, headers=auth_headers)).status_code == 422
    em = await client.post("/api/controls/emergency-exit", json={"confirm": "EMERGENCY EXIT", "reason": "test"}, headers=auth_headers)
    assert em.status_code == 200 and em.json()["kill_switch"] == "ENGAGED" and await kill_switch.is_engaged(app.state.redis)
    cp = await client.post("/api/controls/close-positions", json={"confirm": "CLOSE POSITIONS"}, headers=auth_headers)
    assert cp.status_code == 200 and cp.json()["exit_requested"] == 0
    # CLOSE POSITIONS flags the active app's positions (Solana and EVM alike),
    # never a leftover position of the removed futures engines (nothing manages it).
    await seed_closed(app, "solana", "solana_fresh", [])
    await seed_closed(app, "evm_bsc", "evm_bsc", [])
    await seed_closed(app, "binance_futures", "binance_futures", [])
    cp = await client.post("/api/controls/close-positions", json={"confirm": "CLOSE POSITIONS"}, headers=auth_headers)
    assert cp.status_code == 200 and cp.json()["exit_requested"] == 2
    async with app.state.db_session_factory() as s:
        flagged = {p.engine: p.exit_requested for p in (await s.execute(select(PaperPosition).where(
            PaperPosition.status == "open"))).scalars()}
    assert flagged == {"solana_fresh": True, "evm_bsc": True, "binance_futures": False}
    detail = (await client.get("/api/chains/robinhood", headers=auth_headers)).json()
    assert detail["evm_chain_id"] == 4663 and any(x["key"] == "pons_v2" for x in detail["launchpads"])


async def test_evm_tokens_positions_and_settings(app, client, auth_headers):
    from datetime import datetime, timezone
    from decimal import Decimal

    from yonixalpha_core.db.models import AuditLog, EvmToken, EvmTrade

    now = datetime.now(timezone.utc)
    tok = "0x1111111111111111111111111111111111111111"
    async with app.state.db_session_factory() as s:
        s.add(EvmToken(chain="bsc", token=tok, launchpad="fourmeme", name="Moon", symbol="MOON", created_at=now,
                       category="FRESH", stage="CURVE", venue={}, stats={"buys": 5}, last_trade_at=now,
                       extra={"launch_seen": True, "entry_decision": {"decision": "NO_TRADE",
                                                                      "blockers": [{"code": "LAUNCHPAD_NOT_VERIFIED"}]}},
                       safety_verdict="PASS"))
        s.add(EvmTrade(event_id=f"bsc:0xab:{1}", chain="bsc", launchpad="fourmeme", token=tok, trader="0x" + "2" * 40,
                       is_buy=True, token_amount=Decimal(10 ** 21), quote_amount=Decimal(10 ** 16), at=now))
        await s.commit()
    r = (await client.get("/api/evm/tokens?chain=bsc", headers=auth_headers)).json()
    assert r["categories"] == {"FRESH": 1} and r["tokens"][0]["entry_decision"]["decision"] == "NO_TRADE"
    d = (await client.get(f"/api/evm/tokens/bsc/{tok.upper().replace('0X', '0x')}", headers=auth_headers)).json()
    assert d["token"]["symbol"] == "MOON" and d["trades"][0]["side"] == "BUY" and d["trades"][0]["quote_amount"] == "0.01"
    assert (await client.get("/api/evm/tokens/bsc/0xdead", headers=auth_headers)).status_code == 404
    assert (await client.get("/api/evm/positions", headers=auth_headers)).json()["positions"] == []

    g = (await client.get("/api/evm/settings", headers=auth_headers)).json()
    assert g["settings"]["bsc"]["position_size"] == "0.02" and g["settings"]["robinhood"]["position_size"] == "0.005"
    bad = await client.put("/api/evm/settings", json={"bsc": {"position_size": "5"}}, headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/evm/settings", json={"bsc": {"position_size": "0.03"}, "fresh_min_buys": 7},
                          headers=auth_headers)
    assert ok.status_code == 200 and ok.headers.get("x-config-revision")
    s2 = (await client.get("/api/evm/settings", headers=auth_headers)).json()["settings"]
    assert s2["bsc"]["position_size"] == "0.03" and s2["bsc"]["max_total_exposure"] == "0.1" and s2["fresh_min_buys"] == 7
    async with app.state.db_session_factory() as s:
        from sqlalchemy import select
        assert (await s.execute(select(AuditLog).where(AuditLog.event_type == "evm_settings.update"))).scalars().first()
    assert (await client.get("/api/evm/tokens", headers={})).status_code == 401


async def test_copy_targets_profiles_and_events(app, client, auth_headers, monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal

    from yonixalpha_core.chains.evm import rpc_registry as evm_registry
    from yonixalpha_core.db.models import AuditLog, CopyEvent, EvmAddressKind, WalletProfile

    evm = "0x" + "a" * 40
    router = "0x" + "b" * 40

    class FakeNode:  # eth_getCode: the router is a contract, the wallet has no code
        async def get_code(self, address):
            return "0x6080604052" if address == router else "0x"

        async def aclose(self):
            pass

    async def fake_rpc_for(session, settings, chain):
        return FakeNode()

    monkeypatch.setattr(evm_registry, "rpc_for", fake_rpc_for)
    off = await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": evm}, headers=auth_headers)
    assert off.status_code == 409 and off.json()["detail"]["code"] == "CHAIN_DISABLED"  # SOLANA_ONLY (default)
    app.state.settings = app.state.settings.model_copy(update={"SYSTEM_PROFILE": "MULTI_CHAIN"})
    refused = await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": router}, headers=auth_headers)
    assert refused.status_code == 422 and "contract" in refused.json()["detail"]
    async with app.state.db_session_factory() as s:
        assert (await s.get(EvmAddressKind, ("bsc", router))).kind == "CONTRACT"
    bad = await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": "not-an-address-at-all-xxxxxxxxxxxx"},
                            headers=auth_headers)
    assert bad.status_code == 422
    bad = await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": evm, "settings": {"size_mode": "ALL_IN"}},
                            headers=auth_headers)
    assert bad.status_code == 422
    r = await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": evm, "label": "watch", "mode": "MIRROR",
                                                     "settings": {"fixed_size": "0.01", "chase_guard_pct": "0.1"}},
                          headers=auth_headers)
    assert r.status_code == 200 and r.headers.get("x-config-revision")
    t = r.json()
    assert t["mode"] == "MIRROR" and t["settings"]["fixed_size"] == "0.01" and t["settings"]["max_delay_seconds"] == 30
    assert t["account_kind"] == "WALLET" and t["warning"] is None
    assert (await client.post("/api/copy/targets", json={"chain": "bsc", "wallet": evm.upper().replace("0X", "0x")},
                              headers=auth_headers)).status_code == 409
    p = await client.patch(f"/api/copy/targets/{t['id']}", json={"mode": "NOTIFY", "settings": {"max_open_positions": 1}},
                           headers=auth_headers)
    assert p.status_code == 200 and p.json()["mode"] == "NOTIFY" and p.json()["settings"]["fixed_size"] == "0.01"

    now = datetime.now(timezone.utc)
    async with app.state.db_session_factory() as s:
        s.add(WalletProfile(chain="bsc", wallet=evm, metrics={"win_rate": 0.5}, labels=["SNIPER"], score=Decimal("0.61"),
                            source="evm_trades", trades=40, tokens=12, last_seen=now))
        s.add(WalletProfile(chain="bsc", wallet="0x" + "b" * 40, metrics={}, labels=[], score=None,
                            score_detail={"status": "INSUFFICIENT_DATA"}, source="evm_trades", trades=4, tokens=2, last_seen=now))
        import uuid
        s.add(CopyEvent(id=uuid.uuid4(), target_id=uuid.UUID(t["id"]), chain="bsc", wallet=evm, token="0x" + "1" * 40,
                        side="BUY", source_event_id="bsc:0xab:1", target_token_amount=Decimal(10 ** 21),
                        target_quote_amount=Decimal(10 ** 16), target_at=now, detected_at=now, decision="SKIPPED",
                        reason="LAUNCHPAD_NOT_VERIFIED", latency_ms={"detection": 2100, "total": 2300}))
        await s.commit()
    prof = (await client.get("/api/wallets/profiles?chain=bsc&sort=score", headers=auth_headers)).json()
    assert [x["score"] for x in prof["profiles"]] == ["0.6100", None] and prof["profiles"][0]["is_copy_target"]
    assert "rank" not in prof["profiles"][0] and "best" not in str(prof["profiles"]).lower()
    assert len((await client.get("/api/wallets/profiles?label=sniper", headers=auth_headers)).json()["profiles"]) == 1
    ev = (await client.get("/api/copy/events?chain=bsc", headers=auth_headers)).json()
    assert ev["events"][0]["reason"] == "LAUNCHPAD_NOT_VERIFIED" and ev["events"][0]["target_quote_amount"] == "0.01"
    tl = (await client.get("/api/copy/targets", headers=auth_headers)).json()["targets"]
    assert tl[0]["stats"] == {"events": {"SKIPPED": 1}, "open_positions": 0}
    assert (await client.get("/api/copy/positions", headers=auth_headers)).json()["positions"] == []
    assert (await client.delete(f"/api/copy/targets/{t['id']}", headers=auth_headers)).status_code == 200
    assert (await client.get("/api/copy/targets", headers=auth_headers)).json()["targets"] == []
    async with app.state.db_session_factory() as s:
        from sqlalchemy import select
        kinds = set((await s.execute(select(AuditLog.event_type))).scalars())
    assert {"copy_target.create", "copy_target.update", "copy_target.delete"} <= kinds


async def test_copy_position_link_latency_and_outcomes(app, client, auth_headers):
    """§32 link fields of a copied position, §33 latency stages (paper: the
    live-only stages are None, never 0) and the §35 outcome summary."""
    import uuid

    from yonixalpha_core.db.models import CopyEvent, CopyPosition, CopyTarget, PaperAccount

    tok, w = "0x" + "2" * 40, "0x" + "c" * 40
    async with app.state.db_session_factory() as s:
        t = CopyTarget(chain="bsc", wallet=w, mode="MIRROR", enabled=True, settings={})
        acct = PaperAccount(name="evm_copy_bsc", quote_currency="BNB", starting_balance=Decimal(1), cash_balance=Decimal(1))
        s.add_all([t, acct])
        await s.flush()
        p = PaperPosition(account_id=acct.id, engine="evm_copy_bsc", symbol="MOON", asset_id=tok, provider="paper", side="LONG",
                          entry_price=Decimal("0.0000011"), quantity=Decimal(20000), initial_quantity=Decimal(20000),
                          remaining_quantity=Decimal(10000), entry_cost_quote=Decimal("0.022"),
                          proceeds_quote=Decimal("0.024"), last_price=Decimal("0.000002"), take_profit=[], status="open",
                          entry_at=NOW, plan={})
        s.add(p)
        await s.flush()
        s.add(CopyPosition(position_id=p.id, target_id=t.id, chain="bsc", token=tok, target_tokens=Decimal(5 * 10 ** 23)))
        lat = {"detection": 2000, "analysis": 300, "risk": 100, "decision": 400, "execution": 100, "build": None,
               "sign": None, "submission": None, "landing": None, "confirmation": None,
               "live_only": ["build", "sign", "submission", "landing", "confirmation"], "total": 2500}
        s.add(CopyEvent(id=uuid.uuid4(), target_id=t.id, chain="bsc", wallet=w, token=tok, side="BUY",
                        source_event_id="bsc:0xfeed:1", target_token_amount=Decimal(10 ** 24),
                        target_quote_amount=Decimal(10 ** 18), target_at=NOW - timedelta(minutes=1),
                        detected_at=NOW - timedelta(seconds=58), decision="COPIED", reason="paper entry", latency_ms=lat,
                        position_id=p.id, outcome={"status": "EVALUATED", "class": "COPIED", "label": "WOULD_HAVE_WON",
                                                   "result_pct": 12.5}, outcome_at=NOW))
        s.add(CopyEvent(id=uuid.uuid4(), target_id=t.id, chain="bsc", wallet=w, token=tok, side="SELL",
                        source_event_id="bsc:0xbeef:2", target_token_amount=Decimal(5 * 10 ** 23),
                        target_quote_amount=Decimal(6 * 10 ** 17), target_at=NOW, detected_at=NOW, decision="COPIED"))
        for i, (cls, label, r) in enumerate([("MISSED", "WOULD_HAVE_WON", 40.0), ("MISSED", "WOULD_HAVE_LOST", -10.0)]):
            s.add(CopyEvent(id=uuid.uuid4(), target_id=t.id, chain="bsc", wallet=w, token="0x" + str(i + 3) * 40, side="BUY",
                            source_event_id=f"bsc:0x{i}:0", target_token_amount=Decimal(1), target_quote_amount=Decimal(1),
                            target_at=NOW, detected_at=NOW, decision="SKIPPED", reason="TOO_LATE: 40s",
                            outcome={"status": "EVALUATED", "class": cls, "label": label, "result_pct": r}, outcome_at=NOW))
        s.add(CopyEvent(id=uuid.uuid4(), target_id=t.id, chain="bsc", wallet=w, token="0x" + "9" * 40, side="BUY",
                        source_event_id="bsc:0x9:0", target_token_amount=Decimal(1), target_quote_amount=Decimal(1),
                        target_at=NOW, detected_at=NOW, decision="SKIPPED", reason="SAFETY_NOT_PASSED",
                        outcome={"status": "NO_PRICE_DATA", "class": "BLOCKED_BY_SAFETY"}, outcome_at=NOW))
        await s.commit()

    pos = (await client.get("/api/copy/positions", headers=auth_headers)).json()["positions"]
    lk = pos[0]["link"]
    assert lk["source_wallet"] == w and lk["source_transaction"] == "0xfeed" and lk["copy_mode"] == "MIRROR"
    assert Decimal(lk["copy_ratio"]) == Decimal("0.02") and Decimal(lk["target_entry"]) == Decimal("0.000001")
    assert Decimal(lk["target_exit"]) == Decimal("0.0000012") and Decimal(lk["our_exit"]) == Decimal("0.0000024")
    assert lk["price_displacement_pct"] == 10.0 and lk["slippage"] is None and lk["copy_latency"]["total"] == 2500
    assert Decimal(lk["pnl"]) == Decimal("0.022")

    ev = (await client.get("/api/copy/events?chain=bsc", headers=auth_headers)).json()
    assert ev["median_latency_ms_copied"]["decision"] == 400 and "landing" in ev["live_only_stages"]
    assert any(e["outcome"] and e["outcome"]["label"] == "WOULD_HAVE_WON" for e in ev["events"])

    out = (await client.get("/api/copy/outcomes?chain=bsc", headers=auth_headers)).json()
    by = {g["class"]: g for g in out["summary"]}
    assert by["MISSED"]["evaluated"] == 2 and by["MISSED"]["won"] == 1 and by["MISSED"]["won_rate"] == 0.5
    assert by["MISSED"]["avg_result_pct"] == 15.0 and by["MISSED"]["median_result_pct"] == 15.0
    assert by["COPIED"]["won"] == 1
    safety = by["BLOCKED_BY_SAFETY"]
    assert safety["evaluated"] == 0 and safety["no_price_data"] == 1 and safety["won_rate"] is None  # never 0 %
    assert out["horizon_min"] == 60 and "fees" in out["basis"]


async def test_wallet_validation_settings_and_discovery_stage_filter(app, client, auth_headers):
    from yonixalpha_core.db.models import WalletProfile

    g = (await client.get("/api/wallets/validation-settings", headers=auth_headers)).json()
    assert g["settings"]["min_closed"] == 10 and "never copied automatically" in g["note"]
    bad = await client.put("/api/wallets/validation-settings", json={"max_single_trade_share": 3}, headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/wallets/validation-settings", json={"min_closed": 25}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["settings"]["min_closed"] == 25 and "x-config-revision" in ok.headers
    assert (await client.get("/api/wallets/validation-settings", headers=auth_headers)).json()["settings"]["min_closed"] == 25
    async with app.state.db_session_factory() as s:
        for w, stage in (("0x" + "1" * 40, "VALIDATED"), ("0x" + "2" * 40, "COLLECTING_HISTORY")):
            s.add(WalletProfile(chain="bsc", wallet=w, metrics={"discovery": {"stage": stage}}, labels=[], source="evm_trades",
                                trades=5, tokens=2, last_seen=NOW))
        await s.commit()
    got = (await client.get("/api/wallets/profiles?stage=VALIDATED", headers=auth_headers)).json()["profiles"]
    assert [p["wallet"] for p in got] == ["0x" + "1" * 40]
    assert (await client.get("/api/wallets/profiles?stage=BEST", headers=auth_headers)).status_code == 422


async def test_evm_wallet_endpoint_is_watch_only_and_keyless(app, client, auth_headers):
    r = (await client.get("/api/evm/wallet", headers=auth_headers)).json()
    assert r["live"]["status"] == "NOT_CONFIGURED" and r["live"]["balances"] == {}
    assert "not implemented" in r["live"]["execution"]
    ov = (await client.get("/api/settings/overview", headers=auth_headers)).json()
    groups = {g["key"]: g for g in ov["groups"]}
    assert groups["evm_wallet"]["secrets"] == {"EVM_WALLET_PRIVATE_KEY": "not configured"}
    assert groups["bsc"]["tests"] == ["bsc_rpc"] and groups["robinhood"]["tests"] == ["robinhood_rpc"]
    from yonixalpha_core import env_updates
    assert "server only" in env_updates.validate("EVM_WALLET_PRIVATE_KEY", "0x" + "1" * 64)  # never editable from the dashboard
    assert env_updates.validate("BSC_RPC_URLS", "https://a.example/k,https://b.example") is None
    assert "https" in env_updates.validate("BSC_RPC_URLS", "https://a.example,http://b.example")


async def test_launch_coordination_settings_approval_and_summary(app, client, auth_headers):
    from yonixalpha_core import launch_coordination as lc
    from yonixalpha_core.db.models import EvmToken

    g = (await client.get("/api/evm/coordination-settings", headers=auth_headers)).json()
    assert g["settings"]["actions"]["PRIVILEGED_BUYERS"] == "NO_TRADE" and "MANUAL_APPROVAL" in g["actions"]
    bad = await client.put("/api/evm/coordination-settings", json={"actions": {"COMMON_FUNDER": "IGNORE"}},
                           headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/evm/coordination-settings", json={"actions": {"LAUNCH_BLOCK_BUNDLE": "MANUAL_APPROVAL"}},
                          headers=auth_headers)
    assert ok.status_code == 200 and "x-config-revision" in ok.headers
    s = ok.json()["settings"]
    assert s["actions"]["LAUNCH_BLOCK_BUNDLE"] == "MANUAL_APPROVAL" and s["actions"]["COMMON_FUNDER"] == "NO_TRADE"

    token, t0 = "0x" + "7" * 40, NOW - timedelta(minutes=10)
    cfg, _ = lc.parse_config(s)
    res = lc.analyse({"launchpad": "pons_v2", "launch_seen": True, "created_at": t0, "created_block": 5,
                      "creator": "0x" + "c" * 40},
                     [lc.Trade(f"0x{i:040x}", True, 10 ** 22, 10 ** 16, 5, t0) for i in range(1, 4)],
                     {"supply": 10 ** 27}, cfg)
    assert res["action"] == "MANUAL_APPROVAL"
    async with app.state.db_session_factory() as session:
        session.add(EvmToken(chain="robinhood", token=token, launchpad="pons_v2", created_at=t0, created_block=5,
                             venue={}, stats={}, extra={"launch_seen": True}, category="FRESH", stage="CURVE",
                             coordination=json.loads(json.dumps(res, default=str)), coordination_at=NOW))
        await session.commit()
    d = (await client.get(f"/api/evm/tokens/robinhood/{token}", headers=auth_headers)).json()["token"]
    assert d["coordination_action"] == "MANUAL_APPROVAL" and d["coordination"]["findings"][0]["code"] == "LAUNCH_BLOCK_BUNDLE"
    a = await client.post(f"/api/evm/tokens/robinhood/{token}/coordination-approval", headers=auth_headers)
    assert a.status_code == 200 and a.json()["approval"]["fingerprint"] == res["fingerprint"]
    d = (await client.get(f"/api/evm/tokens/robinhood/{token}", headers=auth_headers)).json()["token"]
    assert d["coordination_approval"]["findings"] == ["LAUNCH_BLOCK_BUNDLE"]
    assert (await client.delete(f"/api/evm/tokens/robinhood/{token}/coordination-approval",
                                headers=auth_headers)).json() == {"revoked": True}
    summary = (await client.get("/api/evm/coordination/summary?chain=robinhood", headers=auth_headers)).json()
    lp = summary["launchpads"][0]
    assert lp["launchpad"] == "pons_v2" and lp["detections"] == {"LAUNCH_BLOCK_BUNDLE": 1}
    assert lp["unknown_checks"]["COMMON_FUNDER"] == 1  # no explorer data: NOT_CONFIGURED, never "no common funder"
    async with app.state.db_session_factory() as session:  # a NO_TRADE assessment is never approvable
        row = await session.get(EvmToken, ("robinhood", token))
        row.coordination = {**row.coordination, "action": "NO_TRADE"}
        await session.commit()
    assert (await client.post(f"/api/evm/tokens/robinhood/{token}/coordination-approval",
                              headers=auth_headers)).status_code == 409


async def test_evm_observations_list_detail_and_settings(app, client, auth_headers):
    from yonixalpha_core.chains.evm import observation as ob
    from yonixalpha_core.db.models import EvmToken

    token, t0 = "0x" + "9" * 40, NOW - timedelta(minutes=70)
    async with app.state.db_session_factory() as session:
        session.add(EvmToken(chain="bsc", token=token, launchpad="flap", symbol="OBS", created_at=t0, created_block=5,
                             venue={}, stats={}, extra={"launch_seen": True}, category="FRESH", stage="CURVE"))
        await ob.ensure(session, "bsc", token, "FRESH", t0, "launch observed", ob.ObservationConfig())
        await session.commit()
        await ob.step(session, "bsc", NOW, ob.ObservationConfig())  # T0..T+60 snapshots, then EXPIRED_NO_ENTRY
        await session.commit()
    r = (await client.get("/api/evm/observations?chain=bsc", headers=auth_headers)).json()
    o = r["observations"][0]
    assert o["symbol"] == "OBS" and o["state"] == "EXPIRED" and o["expiry_reason"] == "EXPIRED_NO_ENTRY"
    assert o["snapshot_labels"] == ["T0", "T+5", "T+10", "T+20", "T+30", "T+60"] and o["last_snapshot"]["minutes"] == 60
    assert r["counts"] == {"FRESH": {"EXPIRED": 1}} and r["expired_held_by"] == {"NEVER_EVALUATED": 1}
    assert (await client.get("/api/evm/observations?state=BOGUS", headers=auth_headers)).status_code == 422
    d = (await client.get(f"/api/evm/tokens/bsc/{token}", headers=auth_headers)).json()
    assert d["observations"][0]["history"][-1]["state"] == "EXPIRED" and "T+30" in d["observations"][0]["snapshots"]
    g = (await client.get("/api/evm/observation-settings", headers=auth_headers)).json()
    assert g["settings"]["snapshots_min"] == [0, 5, 10, 20, 30, 60]
    bad = await client.put("/api/evm/observation-settings", json={"snapshots_min": [5, 10]}, headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/evm/observation-settings", json={"window_min": {"MOMENTUM": 90}}, headers=auth_headers)
    assert ok.status_code == 200 and "x-config-revision" in ok.headers
    assert ok.json()["settings"]["window_min"] == {"FRESH": 60, "MIGRATED": 60, "MOMENTUM": 90}


async def test_evm_streams_state_lead_and_settings(app, client, auth_headers):
    """Master §9 / §13: the streams page shows what data-evm published per
    stream, how many copy-target trades a stream saw before confirmed-trade
    detection (and by how much) over 24 hours, and validated settings; a
    refused BSC pending stream is an UPGRADE REQUIRED plan-health finding."""
    from yonixalpha_core.db.models import CopyEvent, CopyTarget

    async with app.state.db_session_factory() as s:
        t = CopyTarget(chain="robinhood", wallet="0x" + "ab" * 20, mode="NOTIFY", enabled=True, settings={})
        s.add(t)
        await s.flush()
        for i, lat in enumerate([{"detect": 900, "stream_source": "sequencer_feed", "stream_lead": 700},
                                 {"detect": 800, "stream_source": "sequencer_feed", "stream_lead": 500},
                                 {"detect": 950}]):
            s.add(CopyEvent(target_id=t.id, chain="robinhood", wallet=t.wallet, token="0x" + "cc" * 20, side="BUY",
                            source_event_id=f"ev{i}", target_token_amount=1, target_quote_amount=1, target_at=NOW,
                            detected_at=NOW, decision="NOTIFIED", latency_ms=lat))
        await s.commit()
    await app.state.redis.set("yx:evm:stream:robinhood:sequencer_feed", json.dumps(
        {"chain": "robinhood", "source": "sequencer_feed", "state": "CONNECTED", "messages": 120, "gaps": 0,
         "delay_s_median": 1.0}))
    await app.state.redis.set("yx:evm:stream:bsc:pending_tx", json.dumps(
        {"chain": "bsc", "source": "pending_tx", "state": "REFUSED", "url": "wss://bsc.example/***",
         "detail": "the provider refuses pending-transaction subscriptions: method not allowed"}))
    r = (await client.get("/api/evm/streams", headers=auth_headers)).json()
    rh = r["chains"]["robinhood"]
    assert rh["streams"]["sequencer_feed"]["state"] == "CONNECTED" and rh["expected"] == ["sequencer_feed"]
    assert rh["copy_events_24h"] == 3 and rh["seen_on_stream_24h"] == 2 and rh["by_source_24h"] == {"sequencer_feed": 2}
    assert rh["lead_ms_median"] == 700 and rh["lead_ms_p95"] is None  # too few samples for a p95
    assert r["chains"]["bsc"]["streams"]["pending_tx"]["state"] == "REFUSED" and r["chains"]["bsc"]["copy_events_24h"] == 0
    xc = rh["crosscheck"]  # master §68: nothing counted yet -> rates NOT AVAILABLE, never 0 %
    assert xc["stream_txs_in_logs"]["rate"] is None and xc["launches_seen_first_by_stream"]["logged"] == 0
    assert r["settings"]["robinhood_feed_url"] == "wss://feed.mainnet.chain.robinhood.com"
    bad = await client.put("/api/evm/stream-settings", json={"robinhood_feed_url": "http://x"}, headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/evm/stream-settings", json={"bsc_pending_enabled": False}, headers=auth_headers)
    assert ok.status_code == 200 and "x-config-revision" in ok.headers
    assert (await client.get("/api/evm/streams", headers=auth_headers)).json()["settings"]["bsc_pending_enabled"] is False
    ph = (await client.get("/api/rpc/plan-health", headers=auth_headers)).json()
    mem = [f for f in ph["findings"] if f["capability"] == "mempool / pending transactions"]
    assert mem and mem[0]["severity"] == "UPGRADE_REQUIRED" and mem[0]["chain"] == "bsc"


async def test_contract_profiles_are_hidden_unless_asked_for(app, client, auth_headers):
    """M10b: a router or bot credited with trades is not a wallet: its profile
    is hidden from Smart Wallets unless include_contracts is set."""
    from yonixalpha_core.db.models import WalletProfile

    async with app.state.db_session_factory() as s:
        for addr, kind in (("0x" + "c1" * 20, "CONTRACT"), ("0x" + "c2" * 20, "WALLET"), ("0x" + "c3" * 20, None)):
            m = {"discovery": {"stage": "REJECTED" if kind == "CONTRACT" else "COLLECTING_HISTORY"}}
            if kind:
                m["account"] = {"kind": kind}
            s.add(WalletProfile(chain="bsc", wallet=addr, metrics=m, labels=["CONTRACT"] if kind == "CONTRACT" else [],
                                source="evm_trades", trades=10, tokens=2))
        await s.commit()
    shown = (await client.get("/api/wallets/profiles?chain=bsc", headers=auth_headers)).json()["profiles"]
    assert sorted(p["wallet"][2:4] for p in shown) == ["c2", "c3"]  # unknown kind (older profiles) still shown
    every = (await client.get("/api/wallets/profiles?chain=bsc&include_contracts=true", headers=auth_headers)).json()
    assert len(every["profiles"]) == 3 and "routers" in every["note"]
