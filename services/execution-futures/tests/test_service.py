"""The execution-futures loop around futures_live (the lifecycle itself is
covered in services/decision-engine/tests/test_futures_live.py)."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from yonixalpha_core import external_bots, futures_live
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import ExecutionOrder
from yonixalpha_core.execution.registry import build_providers

from app.main import tick

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
LOCKED = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
LIVE_ON = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, BINANCE_TESTNET=True,
                          TELEGRAM_BOT_TOKEN=None, TELEGRAM_CHAT_ID=None)


def order(provider: str, key: str) -> ExecutionOrder:
    return ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="BTCUSDT", provider=provider, route="perp",
                          amount="0.01", amount_kind="base", slippage_pct=1, priority_fee_sol=0, status="PENDING",
                          idempotency_key=key)


async def test_locks_closed_cancel_futures_orders_only_and_report_disabled(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        s.add_all([order("binance_futures", "a"), order("pumpportal_local", "b")])
        await s.commit()
    providers = build_providers(httpx.AsyncClient(), LOCKED)
    ran = await tick(sf, redis_client, LOCKED, providers, {}, NOW, {})
    assert ran["cancelled"] == 1
    async with sf() as s:
        rows = {o.provider: o.status for o in (await s.execute(select(ExecutionOrder))).scalars()}
    assert rows == {"binance_futures": "CANCELLED", "pumpportal_local": "PENDING"}  # Solana worker's order untouched
    ready = json.loads(await redis_client.get(futures_live.READY_KEY.format(venue="binance")))
    assert ready["status"] == "disabled"
    ok, why = await futures_live.readiness(redis_client, LIVE_ON, "binance", NOW)
    assert not ok and "environment locks closed" in why


async def test_unconfigured_venues_report_not_configured_and_send_nothing(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    sent = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500)))
    providers = build_providers(client, LIVE_ON)
    ran = await tick(sf, redis_client, LIVE_ON, providers, {}, NOW, {})
    assert {r["venue"]: r["status"] for r in ran["reconcile"]} == {
        "binance": "not_configured", "bybit": "not_configured", "hyperliquid": "not_configured", "mt5": "not_configured"}
    assert sent == []
    ok, why = await futures_live.readiness(redis_client, LIVE_ON, "bybit", NOW)
    assert not ok and "credentials not set" in why


async def test_unreachable_exchange_is_unavailable_not_ready(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"code": -1, "msg": "down"})))
    env = SimpleNamespace(**vars(LIVE_ON), BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
    providers = {"binance": build_providers(client, env)["binance"]}
    ran = await tick(sf, redis_client, env, providers, {}, NOW, {})
    assert ran["reconcile"][0]["status"] == "unavailable"
    ok, why = await futures_live.readiness(redis_client, env, "binance", NOW)
    assert not ok and "not ready" in why


async def test_external_bots_polled_with_their_own_token(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json={"running": True, "paused": False, "position": {"side": "LONG", "size": 0.01},
                                         "last_signal": "LONG", "last_error": None, "uptime_seconds": 5})
    env = SimpleNamespace(**vars(LOCKED), META_MUSE_CONTROL_URL="http://127.0.0.1:8101", META_MUSE_TOKEN="tok")
    ran = await tick(sf, redis_client, env, {}, {}, NOW, {}, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert ran["bots"]["meta_muse"]["state"] == "CONNECTED" and ran["bots"]["goldvsbtc"]["state"] == "NOT CONFIGURED"
    assert seen == [("http://127.0.0.1:8101/status", "Bearer tok")]
    assert "holds a position" in await external_bots.conflict(redis_client, env, "meta_muse")
    assert await external_bots.conflict(redis_client, env, "confluence_matrix") is None


async def test_readiness_requires_fresh_balance(redis_client):
    live = futures_live.FuturesLiveSettings()
    await futures_live.publish_readiness(redis_client, "bybit", "ready", None, NOW, live, Decimal(500), "USDT")
    assert await futures_live.readiness(redis_client, LIVE_ON, "bybit", NOW) == (True, None)
    later = NOW.replace(minute=10)
    ok, why = await futures_live.readiness(redis_client, LIVE_ON, "bybit", later)
    assert not ok and "old" in why
