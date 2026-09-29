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
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FEATURE_VERSION, FUTURES_FEATURES
from yonixalpha_core.safety import store
from yonixalpha_core.safety.liquidity import book_from_levels
from yonixalpha_core.strategies import grid
from yonixalpha_core.venues.common import Candle, Ticker, VenueError

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

NEW_ENDPOINTS = ["/api/analytics/performance", "/api/analytics/gold-btc", "/api/strategies", "/api/venues", "/api/summary",
                 "/api/notifications", "/api/notifications/prefs", "/api/system/health", "/api/system/observability",
                 "/api/ml/review", "/api/ml/predictions", "/api/ml/data-quality", "/api/ml/samples", f"/api/tokens/{MINT}",
                 "/api/paper/orders"]


async def test_new_endpoints_require_auth(client):
    for url in NEW_ENDPOINTS:
        assert (await client.get(url)).status_code == 401, url


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
                                plan={"venue": {"venue": "binance"}} if engine == "binance_futures" else {}))
        s.add(PaperPosition(account_id=acct.id, engine=engine, symbol="OPEN", asset_id="OPEN", provider="paper", side="LONG",
                            entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="open", entry_at=NOW))
        await s.commit()


async def test_performance_analytics_per_currency_and_excludes_open(app, client, auth_headers):
    await seed_closed(app, "binance_futures", "binance_futures", ["10", "-5", "0", "20"], strategy="meta_muse")
    await seed_closed(app, "solana", "solana_fresh", ["0.5"])
    r = await client.get("/api/analytics/performance", headers=auth_headers)
    assert r.status_code == 200
    accts = {a["account"]: a for a in r.json()["accounts"]}
    fut = accts["binance_futures"]
    o = fut["overall"]
    assert fut["currency"] == "USDT" and fut["open_positions_not_counted"] == 1
    assert (o["trades"], o["wins"], o["losses"], o["breakeven"]) == (4, 2, 1, 1)
    assert o["win_rate"] == 0.5 and Decimal(o["profit_factor"]) == 6 and Decimal(o["expectancy"]) == Decimal("6.25")
    assert Decimal(o["avg_win"]) == 15 and Decimal(o["avg_loss"]) == -5
    assert "meta_muse" in fut["by_strategy"] and "binance" in fut["by_venue"]
    assert accts["solana"]["currency"] == "SOL" and accts["solana"]["overall"]["trades"] == 1
    only = await client.get("/api/analytics/performance?account=binance_futures&strategy=nothing", headers=auth_headers)
    assert only.json()["accounts"][0]["overall"]["trades"] == 0


class FakeBinance:
    def __init__(self, fail=False):
        self.fail = fail

    async def klines(self, symbol, interval, limit=200, now=None):
        if self.fail:
            raise VenueError("binance: HTTP 451")
        t0 = NOW - timedelta(hours=limit + 1)
        base = 60000 if symbol == "BTCUSDT" else 2000
        return [Candle(t0 + timedelta(hours=i), Decimal(base), Decimal(base), Decimal(base), Decimal(base + (i % 7)), Decimal(1), True)
                for i in range(limit)]

    async def ticker(self, symbol):
        return Ticker(symbol, NOW, Decimal(100), Decimal(100), Decimal("0.0001"), None)

    async def book(self, symbol, limit=100):
        return book_from_levels([(Decimal("99.9"), Decimal(10))], [(Decimal("100.1"), Decimal(10))], Decimal(5))


async def test_gold_btc_analytics_and_upstream_failure(app, client, auth_headers):
    app.state.venues["binance"] = FakeBinance()
    r = await client.get("/api/analytics/gold-btc", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ratio"] > 0 and len(body["points"]) == 500 and "no trading signal" in body["note"]
    await app.state.redis.flushdb()
    app.state.venues["binance"] = FakeBinance(fail=True)
    r = await client.get("/api/analytics/gold-btc", headers=auth_headers)
    assert r.status_code == 502 and "HTTP 451" in r.json()["detail"]


async def test_strategies_catalog_config_validation_and_modes(client, auth_headers):
    r = await client.get("/api/strategies", headers=auth_headers)
    names = {s["name"]: s for s in r.json()}
    assert {"solana_fresh", "solana_migration", "solana_momentum", "meta_muse", "confluence_matrix", "hyperliquid_grid",
            "gold_vs_btc", "binance_futures", "bybit_futures", "hyperliquid_perps"} <= set(names)
    assert names["gold_vs_btc"].get("mode") is None and "ANALYTICS ONLY" in names["gold_vs_btc"]["status"]
    assert "MT5 bridge" in names["confluence_matrix"]["status"] and "awaiting" in names["confluence_matrix"]["status"]

    bad = await client.put("/api/strategies/meta_muse/config", headers=auth_headers,
                           json={"config": {"fast": 30, "slow": 21, "stop_pct": "5", "secret": "x"}})
    errors = bad.json()["detail"]["errors"]
    assert bad.status_code == 422 and any("unknown" in e for e in errors) and any("stop_pct" in e for e in errors)
    ok = await client.put("/api/strategies/meta_muse/config", headers=auth_headers, json={"config": {"stop_pct": "0.015"}})
    assert ok.status_code == 200 and ok.json()["config"]["stop_pct"] == "0.015"
    ok = await client.put("/api/strategies/solana_fresh/config", headers=auth_headers,
                          json={"config": {"manual_stop_loss_pct": "0.2"}})
    assert ok.status_code == 200 and ok.json()["config"]["manual_stop_loss_pct"] == "0.2"
    assert (await client.put("/api/strategies/solana_fresh/config", headers=auth_headers,
                             json={"config": {"manual_stop_loss_pct": "2"}})).status_code == 422

    r = await client.put("/api/strategies/binance_futures/mode", headers=auth_headers, json={"mode": "OFF"})
    assert r.status_code == 200
    mm = (await client.get("/api/strategies/meta_muse", headers=auth_headers)).json()
    assert mm["mode"] == "PAPER" and mm["effective_mode"] == "OFF"  # most restrictive of strategy and venue
    assert (await client.put("/api/strategies/gold_vs_btc/mode", headers=auth_headers, json={"mode": "AUTO"})).status_code == 409


async def test_grid_start_stop_requests(app, client, auth_headers):
    r = await client.post("/api/strategies/hyperliquid_grid/start", headers=auth_headers)
    assert r.status_code == 200 and await app.state.redis.get(grid.COMMAND_KEY) == "start"
    await client.put("/api/strategies/hyperliquid_grid/mode", headers=auth_headers, json={"mode": "OFF"})
    assert (await client.post("/api/strategies/hyperliquid_grid/start", headers=auth_headers)).status_code == 409
    assert (await client.post("/api/strategies/hyperliquid_grid/stop", headers=auth_headers)).status_code == 200


async def test_venues_status_never_claims_unverified_accounts(app, client, auth_headers):
    r = await client.get("/api/venues", headers=auth_headers)
    v = {x["venue"]: x for x in r.json()}
    assert v["bybit"]["account"]["status"] == "NOT CONNECTED"
    assert v["binance"]["market_data"]["state"] == "UNKNOWN"
    assert v["binance"]["live_orders"]["state"] == "UNKNOWN" and v["mt5"]["live_orders"]["state"] == "UNKNOWN"
    assert (await client.get("/api/venues/bybit/account", headers=auth_headers)).status_code == 409
    app.state.venues["binance"] = FakeBinance()
    m = await client.get("/api/venues/binance/market?symbol=ETHUSDT", headers=auth_headers)
    assert m.status_code == 200 and m.json()["book"]["mid"] == "100.0"
    assert (await client.get("/api/venues/binance/market?symbol=eth;drop", headers=auth_headers)).status_code == 422
    acct = await client.get("/api/venues/binance/account", headers=auth_headers)
    assert acct.status_code == 200 and acct.json()["positions"] == []


async def test_health_states_from_evidence(app, client, auth_headers):
    redis = app.state.redis
    await events.heartbeat(redis, "decision-engine", detail={"venues": {"binance": {
        "last_ok_at": NOW.isoformat(), "consecutive_failures": 0, "last_error": None, "last_error_at": None, "calls": 3},
        "bybit": {"last_ok_at": None, "consecutive_failures": 4, "last_error": "bybit: HTTP 403", "last_error_at": NOW.isoformat(),
                  "calls": 4}}})
    async with app.state.db_session_factory() as s:
        s.add(SystemEvent(service="ml", event_type="service_started", severity="info"))
        await s.commit()
    r = await client.get("/api/system/health", headers=auth_headers)
    c = {x["name"]: x for x in r.json()["connections"]}
    assert c["postgres"]["state"] == "CONNECTED" and c["redis"]["state"] == "CONNECTED"
    assert c["decision-engine"]["state"] == "CONNECTED"
    assert c["ml"]["state"] == "UNAVAILABLE"  # ran before, heartbeat gone
    assert c["paper-trading"]["state"] == "UNKNOWN"  # never seen
    assert c["binance"]["state"] == "CONNECTED" and c["bybit"]["state"] == "UNAVAILABLE" and c["hyperliquid"]["state"] == "UNKNOWN"
    assert r.json()["overall"] == "UNAVAILABLE"
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
    await seed_closed(app, "binance_futures", "binance_futures", ["10"])
    r = await client.get("/api/summary", headers=auth_headers)
    b = r.json()
    fut = next(a for a in b["accounts"] if a["name"] == "binance_futures")
    assert fut["currency"] == "USDT" and Decimal(fut["realized_pnl_today"]) in (Decimal(10), Decimal(0))
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
    await seed_closed(app, "binance_futures", "binance_futures", [])
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
        a = RiskAssessment(idempotency_key="pending-1", engine="binance_futures", strategy="meta_muse", asset_id="ETHUSDT",
                           symbol="ETHUSDT", decision="REQUIRE_MANUAL_APPROVAL", status_label="x", executable=False,
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
                              json={"starting_balance": "5", "confirm": "binance_futures"})
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
        good = ModelVersion(name="gate_futures", version=1, status="challenger", feature_names=FUTURES_FEATURES,
                            training_sample_count=100, metrics={"promotable": True, "challenger": {"auc": 0.7}, "reference": {"x": 1}},
                            artifact=artifact)
        weak = ModelVersion(name="gate_futures", version=2, status="challenger", feature_names=FUTURES_FEATURES,
                            training_sample_count=100, metrics={"promotable": False}, artifact=artifact)
        s.add_all([good, weak])
        s.add(MLFeatureSnapshot(symbol="ETHUSDT", engine="binance_futures", features={}, feature_version=FEATURE_VERSION,
                                label=1, quality_status="ok"))
        await s.commit()
        good_id, weak_id = str(good.id), str(weak.id)
    review = {m["model"]: m for m in (await client.get("/api/ml/review", headers=auth_headers)).json()}
    gf = review["gate_futures"]
    assert gf["mode"] == "RULES ONLY" and gf["champion"] is None and gf["samples"]["labeled"] == 1
    assert "reference" not in gf["challenger"]["metrics"]
    assert (await client.post(f"/api/ml/models/{weak_id}/promote", headers=auth_headers, json={})).status_code == 409
    r = await client.post(f"/api/ml/models/{good_id}/promote", headers=auth_headers, json={"note": "reviewed"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    await app.state.redis.set(f"{DRIFT_FLAG_PREFIX}gate_futures", "1")
    review = {m["model"]: m for m in (await client.get("/api/ml/review", headers=auth_headers)).json()}
    assert review["gate_futures"]["mode"] == "ML IGNORED (drift)"
    assert (await client.post("/api/ml/gate_futures/retire", headers=auth_headers, json={"reason": "drift"})).status_code == 200
    assert (await client.post("/api/ml/gate_futures/retire", headers=auth_headers, json={"reason": "again"})).status_code == 404
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
    body = json.dumps([(await client.get(u, headers=auth_headers)).text for u in ("/api/venues", "/api/summary", "/api/system/health")])
    for key in ("API_SECRET", "api_secret", "JWT_SECRET", "password"):
        assert key not in body
    async with app.state.db_session_factory() as s:
        assert (await s.execute(select(Notification))).first() is None


async def test_positions_and_decisions_filter_by_strategy(app, client, auth_headers):
    await seed_closed(app, "binance_futures", "binance_futures", ["1"], strategy="meta_muse")
    await seed_closed(app, "binance_futures", "binance_futures", ["2", "3"], strategy="confluence_matrix")
    r = await client.get("/api/paper/positions?strategy=confluence_matrix&status=closed", headers=auth_headers)
    assert r.json()["total"] == 2
    r = await client.get("/api/control/assessments?strategy=meta_muse", headers=auth_headers)
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
    assert {x["key"] for x in (await client.get("/api/launchpads?chain=bsc", headers=auth_headers)).json()["launchpads"]} == {"fourmeme", "flap"}
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


async def test_copy_targets_profiles_and_events(app, client, auth_headers):
    from datetime import datetime, timezone
    from decimal import Decimal

    from yonixalpha_core.db.models import AuditLog, CopyEvent, WalletProfile

    evm = "0x" + "a" * 40
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
