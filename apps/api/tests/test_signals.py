from decimal import Decimal

import pytest

from yonixalpha_core.db.models import StrategySignal

pytestmark = pytest.mark.asyncio


async def _seed_signal(app, **overrides) -> StrategySignal:
    defaults = dict(symbol="TESTMINT", decision="WAIT", confidence=Decimal("0.2"), risk_score=Decimal("0.5"), data_quality="degraded")
    defaults.update(overrides)
    async with app.state.db_session_factory() as session:
        signal = StrategySignal(**defaults)
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        return signal


async def test_list_signals_requires_auth(client):
    resp = await client.get("/api/signals")
    assert resp.status_code == 401


async def test_list_signals_returns_seeded_rows(app, client, auth_headers):
    await _seed_signal(app, decision="WAIT")
    await _seed_signal(app, decision="NO_TRADE")

    resp = await client.get("/api/signals", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2


async def test_list_signals_filters_by_decision(app, client, auth_headers):
    await _seed_signal(app, decision="WAIT")
    await _seed_signal(app, decision="NO_TRADE")

    resp = await client.get("/api/signals", params={"decision": "NO_TRADE"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["decision"] == "NO_TRADE"


async def test_list_signals_respects_limit(app, client, auth_headers):
    for _ in range(3):
        await _seed_signal(app)

    resp = await client.get("/api/signals", params={"limit": 2}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["limit"] == 2
