import asyncio
import json
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import events  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import Notification, PlatformSetting  # noqa: E402


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def test_published_events_reach_subscribers_and_are_counted(redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(events.CHANNEL)
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    await events.publish(redis, "trade.created", {"id": "x", "size": 1}, "test")
    await events.publish(redis, "not.a.real.event", {}, "test")
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg:
            break
    body = json.loads(msg["data"])
    assert body["type"] == "trade.created" and body["data"] == {"id": "x", "size": 1} and body["source"] == "test"
    assert await redis.hget(events.COUNTS, "trade.created") == "1"
    assert await redis.hget(events.COUNTS, "not.a.real.event") is None
    await pubsub.aclose()


async def test_publish_never_raises_without_redis():
    await events.publish(None, "trade.created", {})


async def test_heartbeat_expires_and_reads_back(redis):
    await events.heartbeat(redis, "svc-a", detail={"k": 1})
    hb = await events.read_heartbeats(redis, ["svc-a", "svc-b"])
    assert hb["svc-a"]["status"] == "ok" and hb["svc-a"]["detail"] == {"k": 1} and hb["svc-b"] is None
    assert 0 < await redis.ttl(f"{events.HEARTBEAT_PREFIX}svc-a") <= events.HEARTBEAT_TTL_SECONDS


async def test_notifications_are_stored_and_telegram_follows_preferences(db, redis, monkeypatch):
    sent = []

    async def fake_send(settings, text):
        sent.append(text)
        return True

    monkeypatch.setattr(events, "send_telegram_alert", fake_send)
    settings = SimpleNamespace()
    await events.notify(db, redis, settings, "approval_required", "Approve PIPE?", "risk HIGH")
    await events.notify(db, redis, settings, "tp1", "TP1 hit")  # not a Telegram default
    db.add(PlatformSetting(key=events.PREFS_KEY, value={"tp1": {"telegram": True}, "approval_required": {"telegram": False}}))
    await db.flush()
    await events.notify(db, redis, settings, "tp1", "TP1 again")
    await events.notify(db, redis, settings, "approval_required", "Approve again?")
    await db.commit()
    assert sent == ["[INFO] Approve PIPE?\nrisk HIGH", "[INFO] TP1 again"]
    assert len((await db.execute(select(Notification))).scalars().all()) == 4
    await asyncio.sleep(0)


async def test_disabled_service_idles_with_a_disabled_heartbeat_until_stopped(redis, monkeypatch):
    import yonixalpha_core.db.redis as redis_mod

    url = os.environ.get("REDIS_URL", "redis://localhost:6379/9")
    monkeypatch.setattr(redis_mod, "make_redis", lambda settings: from_url(url, decode_responses=True))
    stop = asyncio.Event()
    task = asyncio.create_task(events.idle_while_disabled(SimpleNamespace(), "data-solana", "SOLANA_RPC_URL not set", stop))
    for _ in range(50):
        hb = (await events.read_heartbeats(redis, ["data-solana"]))["data-solana"]
        if hb:
            break
        await asyncio.sleep(0.02)
    assert hb["status"] == "disabled" and hb["detail"]["reason"] == "SOLANA_RPC_URL not set"
    assert not task.done()  # stays up instead of exiting into a restart loop
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_a_third_start_within_the_hour_alerts_once_and_unknown_types_log_once(redis, monkeypatch):
    """Server 2026-10-06: copy-engine was killed for memory 318 times in a day
    and no alert went out; and every copy event logged an unknown-type warning."""
    sent = []

    async def alert_error(service, event, detail=None, **kw):
        sent.append((service, event, detail))
        return True

    monkeypatch.setattr("yonixalpha_core.notify.alert_error", alert_error)
    t0 = 1_000_000.0
    assert await events.note_start(redis, "copy-engine", now=t0) == 1
    assert await events.note_start(redis, "copy-engine", now=t0 + 240) == 2
    assert sent == []  # a deliberate restart (or two) is not a crash loop
    assert await events.note_start(redis, "copy-engine", now=t0 + 480) == 3
    assert sent and sent[0][:2] == ("copy-engine", "restarting_repeatedly") and sent[0][2]["starts_last_hour"] == 3
    assert await events.note_start(redis, "copy-engine", now=t0 + 720) == 4
    assert len(sent) == 1  # at most one alert an hour, across processes (the throttle is in Redis)
    assert await events.note_start(redis, "ml", now=t0) == 1 and len(sent) == 1  # per service
    assert await events.note_start(None, "x") == 0

    warned = []
    monkeypatch.setattr(events.log, "warning", lambda ev, **kw: warned.append((ev, kw.get("type"))))
    monkeypatch.setattr(events, "_unknown_logged", set())
    for _ in range(5):
        await events.publish(redis, "copy.unregistered", {})
    assert warned == [("events.unknown_type", "copy.unregistered")]
