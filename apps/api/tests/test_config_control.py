"""The dashboard is the runtime control plane: a settings change through the
API is persisted, bumps the configuration revision (X-Config-Revision),
and a running service's watcher applies and acknowledges it; health shows
SYNCED. RPC providers are added/tested/encrypted from the API; manual BUY
queues a gate evaluation and manual SELL requests the normal exit."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from yonixalpha_core import manual_trade, runtime_config
from yonixalpha_core.db.models import PaperPosition, RpcProvider, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.testing.pump import MINT, seed_healthy_launch

from tests.conftest import TEST_ADMIN_PASSWORD


async def _ack(app, service="decision-engine"):
    w = runtime_config.RuntimeConfigWatcher(service, app.state.db_session_factory, app.state.redis)
    await w.sync()
    return w


async def test_fresh_tokens_off_on_reaches_the_runtime_without_restart(app, client, auth_headers):
    w = await _ack(app)
    r = await client.put("/api/control/modes/strategy/solana_fresh", json={"mode": "OFF"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    rev_off = int(r.headers["x-config-revision"])
    h = (await client.get("/api/config/health", headers=auth_headers)).json()
    de = next(s for s in h["services"] if s["service"] == "decision-engine")
    assert h["database"]["revision"] == rev_off and de["status"] == "OUT_OF_SYNC"  # not applied yet: never shown as applied
    await w.sync()  # the running service's watcher (event or ≤10 s poll)
    h = (await client.get("/api/config/health", headers=auth_headers)).json()
    de = next(s for s in h["services"] if s["service"] == "decision-engine")
    assert de["status"] == "SYNCED" and de["revision"] == rev_off
    assert h["effective_in_database"]["modes"]["solana_fresh"] == "OFF"
    async with app.state.db_session_factory() as s:  # what gate_eval reads on its next evaluation
        assert (await store.load_strategy_mode(s, "solana_fresh")).value == "OFF"

    r = await client.put("/api/control/modes/strategy/solana_fresh", json={"mode": "PAPER"}, headers=auth_headers)
    rev_on = int(r.headers["x-config-revision"])
    assert rev_on == rev_off + 1
    await w.sync()
    ack = json.loads(await app.state.redis.get(runtime_config.ACK_PREFIX + "decision-engine"))
    assert ack["revision"] == rev_on and ack["effective"]["modes"]["solana_fresh"] == "PAPER"


async def test_reads_and_actions_do_not_bump_the_revision(client, auth_headers):
    await client.get("/api/control/modes", headers=auth_headers)
    r = await client.get("/api/config/health", headers=auth_headers)
    assert r.json()["database"]["revision"] == 0 and "x-config-revision" not in r.headers


async def test_global_edit_warns_about_engine_overrides_and_duplicate_policy_is_effective(app, client, auth_headers):
    cur = (await client.get("/api/control/settings/solana_fresh", headers=auth_headers)).json()
    assert cur["scope_links"]["follows_global"] is True
    payload = {**cur["effective"], "skip_duplicate_names": False}
    r = await client.put("/api/control/settings/solana_fresh", json={"settings": payload}, headers=auth_headers)
    assert r.status_code == 200, r.text
    g = (await client.get("/api/control/settings/GLOBAL", headers=auth_headers)).json()
    assert {"scope": "solana_fresh", "version": 1, "mode": "overrides", "keys": ["skip_duplicate_names"],
            "values": {"skip_duplicate_names": False}} in g["scope_links"]["overridden_by"]
    assert "solana_momentum" in g["scope_links"]["follows_global"]
    async with app.state.db_session_factory() as s:  # the value the gate and assembler use
        eff, meta = await store.load_settings(s, "solana_fresh")
        assert eff.skip_duplicate_names is False and meta["scope"] == "solana_fresh"
        eff_m, _ = await store.load_settings(s, "solana_momentum")
        assert eff_m.skip_duplicate_names is True  # momentum still follows GLOBAL (shown on Configuration Health)
    h = (await client.get("/api/config/health", headers=auth_headers)).json()
    assert h["effective_in_database"]["risk_settings"]["solana_fresh"]["skip_duplicate_names"] is False


async def test_global_edit_reaches_engines_that_override_other_keys(app, client, auth_headers):
    """The reported problem: an engine saved once (e.g. by the Snipe panel)
    stopped following GLOBAL. Now it keeps only the keys it overrides."""
    cur = (await client.get("/api/control/settings/solana_fresh", headers=auth_headers)).json()
    r = await client.put("/api/control/settings/solana_fresh",
                         json={"settings": {**cur["effective"], "skip_duplicate_names": False}}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["version"]["settings"] == {"__overrides__": {"skip_duplicate_names": False}}
    g = (await client.get("/api/control/settings/GLOBAL", headers=auth_headers)).json()
    new_min = str(Decimal(str(g["effective"]["min_liquidity_quote"])) + 7)
    r = await client.put("/api/control/settings/GLOBAL",
                         json={"settings": {**g["effective"], "min_liquidity_quote": new_min}}, headers=auth_headers)
    assert r.status_code == 200, r.text
    async with app.state.db_session_factory() as s:
        eff, meta = await store.load_settings(s, "solana_fresh")
        assert eff.min_liquidity_quote == Decimal(new_min)  # the GLOBAL edit reached the engine
        assert eff.skip_duplicate_names is False  # its own override kept
        assert meta["overrides"] == ["skip_duplicate_names"] and meta["global_version"] == r.json()["version"]["version"]
    f = (await client.get("/api/control/settings/solana_fresh", headers=auth_headers)).json()
    assert f["scope_links"]["mode"] == "overrides" and f["scope_links"]["override_keys"] == ["skip_duplicate_names"]

    r = await client.post("/api/control/settings/solana_fresh/follow-global", headers=auth_headers)
    assert r.status_code == 200 and "x-config-revision" in r.headers
    async with app.state.db_session_factory() as s:
        eff, meta = await store.load_settings(s, "solana_fresh")
        assert eff.skip_duplicate_names is True and meta["scope"] == "GLOBAL"
    f = (await client.get("/api/control/settings/solana_fresh", headers=auth_headers)).json()
    assert f["scope_links"]["follows_global"] is True
    assert (await client.post("/api/control/settings/GLOBAL/follow-global", headers=auth_headers)).status_code == 422


async def test_save_for_all_engines_from_the_dashboard(app, client, auth_headers):
    cur = (await client.get("/api/control/settings/solana_momentum", headers=auth_headers)).json()
    await client.put("/api/control/settings/GLOBAL",
                     json={"settings": {**cur["effective"], "skip_duplicate_names": False}}, headers=auth_headers)
    await client.put("/api/control/settings/solana_momentum",
                     json={"settings": {**cur["effective"], "skip_duplicate_names": True, "min_name_length": 6}}, headers=auth_headers)
    async with app.state.db_session_factory() as s:  # the trap: GLOBAL says allowed, momentum still rejects
        assert (await store.load_settings(s, "solana_momentum"))[0].skip_duplicate_names is True
    g = (await client.get("/api/control/settings/GLOBAL", headers=auth_headers)).json()
    row = next(o for o in g["scope_links"]["overridden_by"] if o["scope"] == "solana_momentum")
    assert row["values"] == {"skip_duplicate_names": True, "min_name_length": 6}

    r = await client.post("/api/control/settings-all", json={"changes": {"skip_duplicate_names": False}}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["engines_updated"] == ["solana_momentum"] and "x-config-revision" in r.headers
    async with app.state.db_session_factory() as s:
        for engine in ("solana_fresh", "solana_migration", "solana_momentum"):
            assert (await store.load_settings(s, engine))[0].skip_duplicate_names is False, engine
        assert (await store.load_settings(s, "solana_momentum"))[0].min_name_length == 6
    m = (await client.get("/api/control/settings/solana_momentum", headers=auth_headers)).json()
    assert m["scope_links"]["override_keys"] == ["min_name_length"] and m["scope_links"]["global_values"]["min_name_length"] is not None

    r = await client.post("/api/control/settings/solana_momentum/follow-global", json={"keys": ["min_name_length"]}, headers=auth_headers)
    assert r.status_code == 200
    m = (await client.get("/api/control/settings/solana_momentum", headers=auth_headers)).json()
    assert m["scope_links"]["follows_global"] is True
    bad = await client.post("/api/control/settings-all", json={"changes": {"nope": 1}}, headers=auth_headers)
    assert bad.status_code == 422


def _mock_http(app, status=200):
    def handler(request):
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "result": 42})
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_add_test_edit_delete_rpc_from_the_dashboard(app, client, auth_headers):
    _mock_http(app)
    body = {"name": "Alchemy main", "provider_type": "alchemy", "rpc_url": "https://solana-mainnet.alchemy.example/v2/TOPSECRET",
            "priority": 150, "timeout_seconds": "8", "rate_limit_rps": "10", "password": TEST_ADMIN_PASSWORD}
    assert (await client.post("/api/rpc/providers", json={**body, "password": "wrong"}, headers=auth_headers)).status_code == 403
    bad = await client.post("/api/rpc/providers", json={**body, "rpc_url": "http://plain.example"}, headers=auth_headers)
    assert bad.status_code == 422
    r = await client.post("/api/rpc/providers", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["test"]["status"] == "CONNECTED" and "x-config-revision" in r.headers
    listing = await client.get("/api/rpc/providers", headers=auth_headers)
    assert "TOPSECRET" not in listing.text
    p = next(x for x in listing.json()["providers"] if x["name"] == "Alchemy main")
    assert p["configured"] and p["healthy"] == "YES" and not p["active"]  # tested, but no service used it yet
    async with app.state.db_session_factory() as s:
        row = (await s.execute(select(RpcProvider))).scalar_one()
        assert "TOPSECRET" not in row.rpc_url_enc
    t = await client.post(f"/api/rpc/providers/{p['id']}/test", headers=auth_headers)
    assert t.json()["status"] == "CONNECTED"
    _mock_http(app, 429)
    assert (await client.post(f"/api/rpc/providers/{p['id']}/test", headers=auth_headers)).json()["status"] == "RATE_LIMITED"
    e = await client.patch(f"/api/rpc/providers/{p['id']}", json={"priority": 20}, headers=auth_headers)
    assert e.status_code == 200 and "x-config-revision" in e.headers
    # It is the only enabled endpoint in this test environment: can't be disabled or deleted.
    assert (await client.patch(f"/api/rpc/providers/{p['id']}", json={"enabled": False}, headers=auth_headers)).status_code == 409
    assert (await client.delete(f"/api/rpc/providers/{p['id']}", headers=auth_headers)).status_code == 409
    assert (await client.patch(f"/api/rpc/providers/{p['id']}", json={"rpc_url": "https://x.example/k"},
                               headers=auth_headers)).status_code == 422  # URL change needs the password


async def test_manual_buy_and_sell_endpoints(app, client, auth_headers):
    redis = app.state.redis
    now = datetime.now(timezone.utc)
    await seed_healthy_launch(redis, now)
    assert (await client.post("/api/trade/buy", json={"mint": MINT}, headers=auth_headers)).status_code == 422  # no confirm
    unknown = await client.post("/api/trade/buy", json={"mint": "1" * 44, "confirm": True}, headers=auth_headers)
    assert unknown.status_code == 422
    pv = (await client.get("/api/trade/preview", params={"mint": MINT}, headers=auth_headers)).json()
    assert pv["route"] == "Pump.fun bonding curve" and pv["execution_mode"] == "PAPER" and pv["current_price_sol"]
    r = await client.post("/api/trade/buy", json={"mint": MINT, "engine": "solana_fresh", "source": "fresh", "confirm": True},
                          headers=auth_headers)
    assert r.status_code == 200, r.text
    req = r.json()
    assert req["status"] == "QUEUED" and await redis.lpop(manual_trade.QUEUE) == req["id"]
    st = (await client.get(f"/api/trade/requests/{req['id']}", headers=auth_headers)).json()
    assert st["status"] == "QUEUED" and "x-config-revision" not in r.headers  # an action, not configuration
    async with app.state.db_session_factory() as s:
        cand = (await s.execute(select(TradingCandidate))).scalar_one()
        assert cand.detail["manual_only"]
        p = PaperPosition(symbol="PIPE", provider="paper", side="LONG", status="open", engine="solana_fresh", asset_id=MINT,
                          entry_price=Decimal("0.00000003"), quantity=Decimal("1000"), entry_at=now)
        s.add(p)
        await s.commit()
        pid = p.id
    assert (await client.post(f"/api/trade/sell/{pid}", headers=auth_headers)).status_code == 422  # no confirm
    sold = await client.post(f"/api/trade/sell/{pid}?confirm=true", headers=auth_headers)
    assert sold.status_code == 200 and sold.json()["status"] == "SELL_REQUESTED"
    async with app.state.db_session_factory() as s:
        assert (await s.get(PaperPosition, pid)).exit_requested is True
    assert (await client.post(f"/api/trade/sell/{pid}?confirm=true", headers=auth_headers)).status_code == 409
