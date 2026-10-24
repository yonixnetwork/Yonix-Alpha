"""LIVE control from the dashboard: arming needs the password, the typed
phrase, a reason and every mandatory check passing on the server; the mode
buttons cannot arm LIVE; disarming is immediate; every attempt is audited
with who, when, from, to and why; no secret is returned."""

import json
from datetime import datetime, timezone

from pydantic import SecretStr
from solders.keypair import Keypair
from sqlalchemy import select

from yonixalpha_core import events, live_control, live_trading, scanner_health
from yonixalpha_core.db.models import AuditLog, RiskSettingsVersion, StrategyConfig
from yonixalpha_core.safety.settings import default_settings_for, settings_to_dict

from tests.conftest import TEST_ADMIN_PASSWORD

SECRET = json.dumps(list(bytes(Keypair())))
ARM = "/api/control/live-control/arm"


async def _events(app, prefix):
    async with app.state.db_session_factory() as s:
        return [(a.event_type, a.detail) for a in (await s.execute(
            select(AuditLog).where(AuditLog.event_type.like(prefix)).order_by(AuditLog.created_at))).scalars()]


async def test_arming_is_refused_while_the_ceiling_is_closed_and_every_refusal_is_audited(app, client, auth_headers):
    r = (await client.get("/api/control/live-control", headers=auth_headers)).json()
    assert r["state"] in ("PAPER", "OBSERVATION_ONLY") and "ceiling" in r["failed"]
    assert r["confirm_phrase"] == "ENABLE LIVE" and SECRET not in json.dumps(r)
    assert (await client.post(ARM, json={"password": TEST_ADMIN_PASSWORD, "confirm": "yes", "reason": "testing"},
                              headers=auth_headers)).status_code == 422
    assert (await client.post(ARM, json={"password": TEST_ADMIN_PASSWORD, "confirm": "ENABLE LIVE", "reason": ""},
                              headers=auth_headers)).status_code == 422
    wrong = await client.post(ARM, json={"password": "nope", "confirm": "ENABLE LIVE", "reason": "testing live"},
                              headers=auth_headers)
    assert wrong.status_code == 403
    refused = await client.post(ARM, json={"password": TEST_ADMIN_PASSWORD, "confirm": "ENABLE LIVE",
                                           "reason": "testing live"}, headers=auth_headers)
    assert refused.status_code == 409 and "Server ceiling" in refused.json()["detail"]["message"]
    assert (await client.get("/api/control/modes", headers=auth_headers)).json()["global_mode"] == "PAPER"
    ev = await _events(app, "live_control.%")
    assert [e for e, _ in ev] == ["live_control.password_rejected", "live_control.arm_refused"]
    assert "ceiling" in ev[1][1]["failed"] and ev[1][1]["reason"] == "testing live"


async def test_with_every_check_passing_live_is_armed_then_disarmed(app, client, auth_headers, monkeypatch):
    saved = app.state.settings
    app.state.settings = saved.model_copy(update={"TRADING_ENABLED": True, "LIVE_TRADING_ENABLED": True, "PAPER_TRADING": False,
                                                  "WALLET_PRIVATE_KEY": SecretStr(SECRET),
                                                  "SOLANA_RPC_URL": "https://rpc.example",
                                                  "SOLANA_WS_URL": "wss://rpc.example"})

    async def healthy(db, r, now=None):
        return {"overall": "CONNECTED", "stages": [], "rpc_last_hour": {"rate_limited_share": 0.0}}

    monkeypatch.setattr(scanner_health, "report", healthy)
    try:
        from app.api.deps import get_redis  # noqa: F401 - the same Redis the app uses
        from yonixalpha_core.config import get_settings
        from yonixalpha_core.db.redis import make_redis

        redis = make_redis(get_settings())
        now = datetime.now(timezone.utc).isoformat()
        await redis.set(live_trading.READY_KEY, json.dumps({"status": "ready", "at": now, "min_sol_reserve": "0.05",
                                                            "wallet_max_age_seconds": "120",
                                                            "genesis_hash": live_control.MAINNET_GENESIS}))
        await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": "1", "at": now}))
        await events.heartbeat(redis, "decision-engine")
        s = settings_to_dict(default_settings_for("GLOBAL"))
        s.update({"max_loss_per_trade_sol": "0.01", "max_position_size_quote": "0.1"})
        async with app.state.db_session_factory() as db:
            db.add(RiskSettingsVersion(scope="GLOBAL", version=1, settings=s))
            db.add(StrategyConfig(strategy="solana_fresh", mode="MANUAL", config={}))
            await db.commit()

        # the mode buttons can never arm LIVE, even with every check passing
        via_buttons = await client.put("/api/control/modes/global", json={"mode": "LIVE"}, headers=auth_headers)
        assert via_buttons.status_code == 409 and "live control" in via_buttons.json()["detail"]["message"]

        pre = (await client.get("/api/control/live-control", headers=auth_headers)).json()
        assert pre["failed"] == [], pre["failed"]
        armed = await client.post(ARM, json={"password": TEST_ADMIN_PASSWORD, "confirm": "ENABLE LIVE",
                                             "reason": "first supervised live session"}, headers=auth_headers)
        assert armed.status_code == 200, armed.text
        a = armed.json()
        assert a["global_mode"] == "LIVE" and a["state"] == "LIVE_ELIGIBLE"
        assert a["last_change"]["from"] == "PAPER" and a["last_change"]["to"] == "LIVE" and a["last_change"]["by"] == "admin"

        off = await client.post("/api/control/live-control/disarm", json={"reason": "end of session"}, headers=auth_headers)
        assert off.status_code == 200 and off.json()["global_mode"] == "PAPER"
        assert off.json()["last_change"]["reason"] == "end of session"
        ev = [e for e, _ in await _events(app, "live_control.%")]
        assert ev == ["live_control.armed", "live_control.disarmed"]
        await redis.aclose()
    finally:
        app.state.settings = saved


async def test_live_control_needs_login(client):
    assert (await client.get("/api/control/live-control")).status_code == 401
    assert (await client.post(ARM, json={})).status_code == 401
    assert (await client.post("/api/control/live-control/disarm", json={})).status_code == 401
