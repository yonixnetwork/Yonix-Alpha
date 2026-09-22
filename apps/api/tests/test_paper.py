from datetime import datetime, timezone
from decimal import Decimal

import pytest

from yonixalpha_core.db.models import PaperPosition

pytestmark = pytest.mark.asyncio


async def _seed_position(app, status="open") -> PaperPosition:
    async with app.state.db_session_factory() as session:
        position = PaperPosition(
            symbol="TESTMINT",
            provider="jupiter",
            side="LONG",
            entry_price=Decimal("0.001"),
            quantity=Decimal("100000"),
            status=status,
            entry_at=datetime.now(timezone.utc),
        )
        session.add(position)
        await session.commit()
        await session.refresh(position)
        return position


async def test_list_positions_requires_auth(client):
    resp = await client.get("/api/paper/positions")
    assert resp.status_code == 401


async def test_list_positions_empty(client, auth_headers):
    resp = await client.get("/api/paper/positions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_list_positions_returns_seeded_row(app, client, auth_headers):
    position = await _seed_position(app)

    resp = await client.get("/api/paper/positions", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(position.id)
    assert body["items"][0]["symbol"] == "TESTMINT"
    assert body["items"][0]["status"] == "open"


async def test_list_positions_filters_by_status(app, client, auth_headers):
    await _seed_position(app, status="open")
    await _seed_position(app, status="closed")

    resp = await client.get("/api/paper/positions", params={"status": "closed"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "closed"
