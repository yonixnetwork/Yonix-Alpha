"""Runtime configuration: every dashboard save bumps one revision, running
services reload what they hold in memory (RPC endpoints) and acknowledge
the revision, and the dashboard can prove SYNCED / OUT_OF_SYNC. Dashboard
RPC providers are encrypted at rest, merged with .env endpoints by
priority, swapped into a running RpcManager without a restart, and tested
with a classified result."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest_asyncio
from redis.asyncio import from_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import runtime_config, secretbox
from yonixalpha_core.db.base import Base, make_session_factory
from yonixalpha_core.db.models import RpcProvider
from yonixalpha_core.redact import redact_url
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.solana import rpc_registry
from yonixalpha_core.solana.rpc import RpcManager

SETTINGS = SimpleNamespace(JWT_SECRET="x" * 40, CONFIG_ENCRYPTION_KEY=None,
                           SOLANA_RPC_URL="https://env-primary.example/?api-key=ENVKEY", SOLANA_RPC_BACKUP_URL=None,
                           SOLANA_RPC_BACKUP_URL_2=None, SOLANA_RPC_BACKUP_URL_3=None,
                           SOLANA_WS_URL="wss://env-primary.example/?api-key=ENVKEY", SOLANA_WS_BACKUP_URL=None)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/14"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.aclose()


# --- revision + acknowledgement ------------------------------------------------

async def test_bump_increments_and_announces(session_factory, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("yx:events")
    async with session_factory() as s:
        a = await runtime_config.bump(s, {"kind": "mode"}, "admin")
        await s.commit()
    async with session_factory() as s:
        b = await runtime_config.bump(s, {"kind": "risk_settings"}, "admin")
        await s.commit()
        assert (await runtime_config.current(s))["revision"] == 2
    assert (a["revision"], b["revision"], b["previous"]) == (1, 2, 1)
    await runtime_config.announce(redis, b)
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg:
            break
    assert msg and json.loads(msg["data"])["type"] == "configuration.updated"
    await pubsub.aclose()


async def test_watcher_reloads_acks_and_reports_effective_settings(session_factory, redis):
    calls = []

    async def reloader():
        calls.append(1)
        return {"ok": True}

    w = runtime_config.RuntimeConfigWatcher("decision-engine", session_factory, redis, {"rpc": reloader},
                                            {"rpc": lambda: {"endpoints": []}})
    assert await w.sync(force=True) == 0 and len(calls) == 1
    # Fresh Tokens OFF → ON from the dashboard: DB write + revision bump.
    async with session_factory() as s:
        await store.set_strategy_mode(s, "solana_fresh", StrategyMode.OFF, None)
        await runtime_config.bump(s, {"kind": "mode", "strategy": "solana_fresh"}, "admin")
        await s.commit()
    await w.sync()
    ack = json.loads(await redis.get(runtime_config.ACK_PREFIX + "decision-engine"))
    assert ack["revision"] == 1 and ack["effective"]["modes"]["solana_fresh"] == "OFF" and len(calls) == 2
    async with session_factory() as s:
        await store.set_strategy_mode(s, "solana_fresh", StrategyMode.AUTO, None)
        await runtime_config.bump(s, {"kind": "mode", "strategy": "solana_fresh"}, "admin")
        await s.commit()
    await w.sync()
    ack = json.loads(await redis.get(runtime_config.ACK_PREFIX + "decision-engine"))
    assert ack["revision"] == 2 and ack["effective"]["modes"]["solana_fresh"] == "AUTO" and len(calls) == 3
    await w.sync()  # no change: reloaders not re-run, ack refreshed
    assert len(calls) == 3


async def test_watcher_wakes_on_the_event_without_waiting_for_the_poll(session_factory, redis):
    w = runtime_config.RuntimeConfigWatcher("paper-trading", session_factory, redis)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    for _ in range(50):
        if w.revision is not None:
            break
        await asyncio.sleep(0.05)
    async with session_factory() as s:
        v = await runtime_config.bump(s, {"kind": "mode"}, "admin")
        await s.commit()
    await runtime_config.announce(redis, v)
    for _ in range(60):  # well under POLL_SECONDS
        if w.revision == 1:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert w.revision == 1


def test_sync_status():
    now = datetime.now(timezone.utc)
    fresh, old = now.isoformat(), (now - timedelta(seconds=runtime_config.ACK_TTL_SECONDS + 5)).isoformat()
    acks = {"decision-engine": {"revision": 5, "at": fresh, "ok": True},
            "paper-trading": {"revision": 4, "at": fresh, "ok": True},
            "data-solana": {"revision": 5, "at": old, "ok": True},
            "engine-solana-discovery": {"revision": 5, "at": fresh, "ok": False, "error": "rpc: boom"}}
    st = {s["service"]: s["status"] for s in runtime_config.sync_status(5, acks, now)["services"]}
    assert st["decision-engine"] == "SYNCED" and st["paper-trading"] == "OUT_OF_SYNC"
    assert st["data-solana"] == "NOT_REPORTING" and st["engine-solana-discovery"] == "OUT_OF_SYNC"
    assert st["data-evm"] == "NOT_REPORTING" and st["copy-engine"] == "NOT_REPORTING"
    assert "execution-futures" not in st  # removed with the futures venues
    # Legacy engines the production compose does not run: not an alarm.
    assert st["engine-solana-momentum"] == "NOT_DEPLOYED" and st["engine-solana-migration"] == "NOT_DEPLOYED"
    assert runtime_config.sync_status(5, acks, now)["status"] == "OUT_OF_SYNC"
    acks["engine-solana-momentum"] = {"revision": 5, "at": fresh, "ok": True}  # started anyway: tracked normally
    assert {s["service"]: s["status"] for s in runtime_config.sync_status(5, acks, now)["services"]}[
        "engine-solana-momentum"] == "SYNCED"


# --- RPC providers ---------------------------------------------------------------

def test_encryption_round_trip_never_plaintext():
    token = secretbox.encrypt(SETTINGS, "https://x.example/?api-key=SECRET")
    assert "SECRET" not in token and secretbox.decrypt(SETTINGS, token) == "https://x.example/?api-key=SECRET"
    assert secretbox.decrypt(SimpleNamespace(JWT_SECRET="y" * 40), token) is None  # other key: unreadable, not garbage


async def _add(s, name, url, priority, enabled=True, rps=None):
    s.add(RpcProvider(name=name, rpc_url_enc=secretbox.encrypt(SETTINGS, url), rpc_display=redact_url(url), priority=priority,
                      enabled=enabled, timeout_seconds=Decimal("5"), rate_limit_rps=rps))


async def test_effective_list_merges_env_and_dashboard_by_priority(session_factory):
    async with session_factory() as s:
        await _add(s, "alchemy", "https://alchemy.example/v2/ALCHEMY-SECRET-KEY-1", 150)
        await _add(s, "chainstack", "https://chainstack.example/CHAINSTACK-SECRET-KEY-2", 50)
        await _add(s, "off", "https://off.example/K3", 10, enabled=False)
        await s.commit()
        labels = [r["label"] for r in await rpc_registry.effective_rpc(s, SETTINGS)]
        assert labels == ["db:chainstack", "env:primary", "db:alchemy"]
        raw = (await s.execute(select(RpcProvider.rpc_url_enc))).scalars().all()
        # Encrypted at rest. (Long markers: a 2-character one can occur by chance
        # in random base64 ciphertext and made this check flaky.)
        assert all("ALCHEMY-SECRET" not in r and "CHAINSTACK-SECRET" not in r for r in raw)
        assert sorted(secretbox.decrypt(SETTINGS, r) for r in raw)[:2] == [
            "https://alchemy.example/v2/ALCHEMY-SECRET-KEY-1", "https://chainstack.example/CHAINSTACK-SECRET-KEY-2"]
        # The dashboard disables / reorders the .env endpoint (URL stays in .env).
        from sqlalchemy.dialects.postgresql import insert
        from yonixalpha_core.db.models import PlatformSetting
        await s.execute(insert(PlatformSetting).values(key=rpc_registry.ENV_OVERRIDES_KEY,
                                                       value={"env:primary": {"priority": 500}}))
        await s.commit()
        assert [r["label"] for r in await rpc_registry.effective_rpc(s, SETTINGS)] == ["db:chainstack", "db:alchemy", "env:primary"]


async def test_reloader_swaps_endpoints_into_a_running_manager_and_fails_over(session_factory, redis):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "env-primary.example":
            return httpx.Response(429)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 7})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=SETTINGS.SOLANA_RPC_URL)
    reloaders = rpc_registry.make_reloaders("decision-engine", rpc, SETTINGS, session_factory, redis)
    await reloaders["rpc"]()
    assert [e.label for e in rpc.endpoints] == ["env:primary"]
    # ADD RPC from the dashboard, then the revision reload: no restart.
    async with session_factory() as s:
        await _add(s, "backup", "https://backup.example/K", 250, rps=5)
        await s.commit()
    await reloaders["rpc"]()
    assert [e.label for e in rpc.endpoints] == ["env:primary", "db:backup"] and rpc.endpoints[1].rps == 5
    assert await rpc.call("getSlot") == 7
    assert await rpc.call("getSlot") == 7  # primary is rate-limited (cooldown): backup directly
    assert seen == ["env-primary.example", "backup.example", "backup.example"]
    snap = {e["label"]: e for e in rpc_registry.rpc_status(rpc)["endpoints"]}
    assert snap["env:primary"]["rate_limited"] and snap["db:backup"]["active"] and snap["db:backup"]["successes"] == 2
    assert "ENVKEY" not in json.dumps(rpc_registry.rpc_status(rpc))


async def test_failover_is_recorded(session_factory, redis):
    state = {"primary_ok": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "env-primary.example" and not state["primary_ok"]:
            return httpx.Response(503)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=SETTINGS.SOLANA_RPC_URL)
    async with session_factory() as s:
        await _add(s, "backup", "https://backup.example/K", 250)
        await s.commit()
    await rpc_registry.make_reloaders("data-solana", rpc, SETTINGS, session_factory, redis)["rpc"]()
    await rpc.call("getSlot")
    state["primary_ok"] = False
    await rpc.call("getSlot")
    entry = json.loads((await redis.lrange(rpc_registry.FAILOVER_LOG, 0, 0))[0])
    assert (entry["from"], entry["to"], entry["service"]) == ("env:primary", "db:backup", "data-solana")
    assert "HTTP 503" in entry["reasons"]


async def test_connection_test_is_classified():
    def client(status=200, body=None, exc=None):
        def h(request):
            if exc:
                raise exc
            return httpx.Response(status, json=body if body is not None else {"jsonrpc": "2.0", "id": 1, "result": 123})
        return httpx.AsyncClient(transport=httpx.MockTransport(h))

    ok = await rpc_registry.test_rpc(client(), "https://a.example/k")
    assert ok["status"] == "CONNECTED" and ok["latency_ms"] is not None and "slot 123" in ok["detail"]
    assert (await rpc_registry.test_rpc(client(401), "https://a.example/k"))["status"] == "AUTHENTICATION_FAILED"
    assert (await rpc_registry.test_rpc(client(429), "https://a.example/k"))["status"] == "RATE_LIMITED"
    assert (await rpc_registry.test_rpc(client(502), "https://a.example/k"))["status"] == "UNAVAILABLE"
    t = await rpc_registry.test_rpc(client(exc=httpx.ReadTimeout("t")), "https://a.example/k", timeout=1)
    assert t["status"] == "TIMEOUT"
    bad = await rpc_registry.test_rpc(client(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32401, "message": "invalid api key"}}),
                                      "https://a.example/k")
    assert bad["status"] == "AUTHENTICATION_FAILED"
    assert (await rpc_registry.test_rpc(client(), "http://insecure.example"))["status"] == "INVALID_CONFIGURATION"
    assert (await rpc_registry.test_rpc(client(), "https://nohost"))["status"] == "INVALID_CONFIGURATION"


async def test_first_success_after_skipping_a_failing_primary_is_a_failover(session_factory, redis):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "env-primary.example":
            return httpx.Response(429)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = RpcManager.create(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), primary_url=SETTINGS.SOLANA_RPC_URL)
    async with session_factory() as s:
        await _add(s, "backup", "https://backup.example/K", 250)
        await s.commit()
    await rpc_registry.make_reloaders("decision-engine", rpc, SETTINGS, session_factory, redis)["rpc"]()
    await rpc.call("getSlot")
    entry = json.loads((await redis.lrange(rpc_registry.FAILOVER_LOG, 0, 0))[0])
    assert (entry["from"], entry["to"]) == ("env:primary", "db:backup") and "HTTP 429" in entry["reasons"]
