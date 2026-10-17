"""Crypto Trading exchange-account trades API: login required, read views,
close needs the password, and a connection with trades cannot be deleted.
(The full order lifecycle is tested in packages/crypto-py/tests/test_execution.py
against simulated Binance and Bybit exchanges.)"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yonixalpha_crypto.db import ApiConnection, ExecGuard, ExecTrade, TradeAction

from tests.conftest import TEST_ADMIN_PASSWORD
from tests.test_crypto_api import crypto  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


async def seed(status="OPEN", label="tn"):
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(eng, expire_on_commit=False)
    async with sf() as s:
        c = ApiConnection(id=uuid.uuid4(), exchange="binance", environment="TESTNET", label=label, key_enc="x",
                          secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…0001", enabled=True,
                          status="OK", created_at=NOW, updated_at=NOW)
        s.add(c)
        await s.flush()
        t = ExecTrade(connection_id=c.id, exchange="binance", environment="TESTNET", strategy_id="practice", symbol="BTC/USDT",
                      status=status, protection="VERIFIED", protection_seq=1, qty_planned=Decimal("0.1"), qty=Decimal("0.0999"),
                      entry_price=Decimal(100), initial_stop=Decimal(98), stop=Decimal(98), stop_limit=Decimal("97.8"),
                      risk_budget=Decimal(5), planned_loss=Decimal("0.25"), plan={}, thesis={"reason": "test"},
                      realized_quote=Decimal(0), fees_quote=Decimal(0), created_at=NOW, updated_at=NOW)
        s.add(t)
        await s.flush()
        s.add(TradeAction(trade_id=t.id, at=NOW, action="PROTECTION_VERIFIED", reason="the exchange reports the stop as open"))
        s.add(ExecGuard(connection_id=c.id, entries_blocked=True, reason="order x sent with no answer", since=NOW, updated_at=NOW))
        await s.commit()
    await eng.dispose()
    return c, t


async def test_requires_login(client):
    for path in ("/api/crypto/exec/trades", "/api/crypto/exec/guards"):
        assert (await client.get(path)).status_code == 401


async def test_views_show_trades_actions_and_blocked_entries(client, auth_headers, crypto):  # noqa: F811
    c, t = await seed()
    rows = (await client.get("/api/crypto/exec/trades", headers=auth_headers)).json()
    assert rows[0]["id"] == str(t.id) and rows[0]["connection"] == "tn" and rows[0]["protection"] == "VERIFIED"
    assert rows[0]["last_action"]["action"] == "PROTECTION_VERIFIED"
    detail = (await client.get(f"/api/crypto/exec/trades/{t.id}", headers=auth_headers)).json()
    assert [a["action"] for a in detail["actions"]] == ["PROTECTION_VERIFIED"]
    g = (await client.get("/api/crypto/exec/guards", headers=auth_headers)).json()
    assert g[0]["entries_blocked"] is True and "no answer" in g[0]["reason"]
    assert (await client.get(f"/api/crypto/exec/trades/{uuid.uuid4()}", headers=auth_headers)).status_code == 404


async def test_close_needs_the_password_and_readable_keys(client, auth_headers, crypto):  # noqa: F811
    c, t = await seed()
    url = f"/api/crypto/exec/trades/{t.id}/close"
    assert (await client.post(url, json={"password": "wrong"}, headers=auth_headers)).status_code == 403
    r = await client.post(url, json={"password": TEST_ADMIN_PASSWORD}, headers=auth_headers)
    assert r.status_code == 503 and "decrypted" in r.json()["detail"]  # seeded keys are not real ciphertext
    _, closed = await seed(status="CLOSED", label="tn2")
    r = await client.post(f"/api/crypto/exec/trades/{closed.id}/close", json={"password": TEST_ADMIN_PASSWORD},
                          headers=auth_headers)
    assert r.status_code == 409


async def test_a_connection_with_trades_cannot_be_deleted(client, auth_headers, crypto):  # noqa: F811
    c, _ = await seed()
    r = await client.post(f"/api/crypto/connections/{c.id}/delete", json={"password": TEST_ADMIN_PASSWORD},
                          headers=auth_headers)
    assert r.status_code == 409 and "disable it instead" in r.json()["detail"]
