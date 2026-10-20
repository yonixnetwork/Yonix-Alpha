"""Momentum controls from the web app (2026-10-20): OFF / PAPER / AUTO are
persisted, audited and validated server-side; AUTO needs confirmation and
never authorizes real money by itself; switching OFF never sells; the
emergency stop is a separate, confirmed action."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, PaperPosition
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import StrategyMode

NOW = datetime.now(timezone.utc).replace(microsecond=0)
URL = "/api/strategies/solana_momentum"


def open_pos(engine: str) -> PaperPosition:
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine=engine, entry_price=Decimal(1),
                         quantity=Decimal(1), initial_quantity=Decimal(1), take_profit=[], status="open",
                         execution_mode="PAPER", entry_cost_quote=Decimal("0.02"), entry_at=NOW - timedelta(minutes=3))


async def test_auto_needs_confirmation_and_runs_on_paper_without_live_authorization(app, client, auth_headers):
    r = await client.get(URL, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["momentum"]["version"] == "1" and body["momentum"]["enabled_checks"] == []
    assert body["execution"]["global_mode"] == "PAPER"

    refused = await client.put(f"{URL}/mode", json={"mode": "AUTO"}, headers=auth_headers)
    assert refused.status_code == 409 and "confirm_auto" in refused.text
    assert (await client.get(URL, headers=auth_headers)).json()["mode"] != "AUTO"  # nothing changed
    also = await client.put("/api/control/modes/strategy/solana_momentum", json={"mode": "AUTO"}, headers=auth_headers)
    assert also.status_code == 409  # the control-center endpoint enforces the same rule

    ok = await client.put(f"{URL}/mode", json={"mode": "AUTO", "confirm_auto": True}, headers=auth_headers)
    assert ok.status_code == 200, ok.text
    ex = ok.json()["execution"]
    assert ok.json()["mode"] == "AUTO" and ex["effective_execution"] == "PAPER" and ex["live_authorized"] is False
    assert "live execution unavailable" in ex["reason"]

    # persisted where the workers read it (a fresh session, as after a restart)
    async with app.state.db_session_factory() as s:
        assert await store.load_strategy_mode(s, "solana_momentum") == StrategyMode.AUTO
        audit = (await s.execute(select(AuditLog).where(AuditLog.event_type == "strategy_mode.changed")
                                 .order_by(AuditLog.created_at.desc()))).scalars().first()
    assert audit.detail["to"] == "AUTO" and audit.detail["auto_confirmed"] is True and audit.user_id is not None
    assert (await client.get(URL, headers=auth_headers)).json()["mode"] == "AUTO"
    # staying on AUTO needs no second confirmation; PAPER / OFF never need one
    assert (await client.put(f"{URL}/mode", json={"mode": "AUTO"}, headers=auth_headers)).status_code == 200
    assert (await client.put(f"{URL}/mode", json={"mode": "PAPER"}, headers=auth_headers)).json()["execution"][
        "effective_execution"] == "PAPER"
    assert (await client.put(f"{URL}/mode", json={"mode": "BOGUS"}, headers=auth_headers)).status_code == 422


async def test_off_keeps_open_positions_managed_and_emergency_stop_is_separate(app, client, auth_headers):
    mom, fresh = open_pos("solana_momentum"), open_pos("solana_fresh")
    async with app.state.db_session_factory() as s:
        s.add_all([mom, fresh])
        await s.commit()
    off = await client.put(f"{URL}/mode", json={"mode": "OFF"}, headers=auth_headers)
    assert off.status_code == 200 and off.json()["open_positions"] == 1
    assert off.json()["execution"]["effective_execution"] == "NONE" and "never sells" in off.json()["execution"]["reason"]
    async with app.state.db_session_factory() as s:
        assert (await s.get(PaperPosition, mom.id)).exit_requested is False  # switching OFF sells nothing

    bad = await client.post(f"{URL}/emergency-stop", json={"confirm": "yes", "close_positions": True}, headers=auth_headers)
    assert bad.status_code == 422
    stop = await client.post(f"{URL}/emergency-stop", json={"confirm": "STOP solana_momentum", "close_positions": True,
                                                            "reason": "test"}, headers=auth_headers)
    assert stop.status_code == 200, stop.text
    assert stop.json()["mode"] == "OFF" and stop.json()["exit_requested"] == 1
    async with app.state.db_session_factory() as s:
        assert (await s.get(PaperPosition, mom.id)).exit_requested is True  # sold by the position loop, normal exit path
        assert (await s.get(PaperPosition, fresh.id)).exit_requested is False  # other strategies untouched
        ev = (await s.execute(select(AuditLog).where(AuditLog.event_type == "strategy.emergency_stop"))).scalar_one()
    assert ev.detail["exit_requested"] == 1 and ev.detail["close_positions"] is True


async def test_v2_checks_are_switched_on_from_the_config_and_version_the_strategy(client, auth_headers):
    r = await client.put(f"{URL}/config", json={"config": {"mv2_exhaustion": True, "mv2_max_return_5m": "0.3"}},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["momentum"]["version"] == "2:exh" and r.json()["config"]["mv2_max_return_5m"] == "0.3"
    bad = await client.put(f"{URL}/config", json={"config": {"mv2_max_return_5m": "99"}}, headers=auth_headers)
    assert bad.status_code == 422
    insight = await client.get(f"{URL}/insight", headers=auth_headers)
    assert insight.status_code == 200 and insight.json()["results"] == [] and "notes" in insight.json()
    assert (await client.get(f"{URL}/insight")).status_code == 401
