"""RPC outage alarm (2026-10-15): one Telegram alert after 5 minutes with no
working Solana RPC, hourly reminders, one "working again" message, shared by
the four Solana services through Redis."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import rpc_outage as ro  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import SystemEvent  # noqa: E402

T0 = 1_791_000_000.0
ENDPOINTS = [{"name": "helius", "source": "dashboard", "url": "https://mainnet.helius-rpc.com", "rate_limited": True,
              "consecutive_failures": 7},
             {"name": "SOLANA_RPC_URL", "source": "env", "url": "https://rpc.example", "auth_failed": True}]
ERROR = "all endpoints failed: https://mainnet.helius-rpc.com/?api-key=FAKE-TEST-KEY returned 429"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    r = from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.delete(ro.KEY_FAILING, ro.KEY_OPEN, *await r.keys(ro.KEY_ALERT + ":*"))
    sent: list[str] = []

    async def send(text):
        sent.append(text)
        return True

    clock = Clock()

    def alarm(service):
        return ro.RpcOutageAlarm(service, make_session_factory(engine), redis=r, send=send, clock=clock)

    yield alarm, clock, sent, make_session_factory(engine)
    await r.delete(ro.KEY_FAILING, ro.KEY_OPEN, *await r.keys(ro.KEY_ALERT + ":*"))
    await r.aclose()
    await engine.dispose()


async def tick(alarms, clock, seconds, ok=False):
    """Advance the clock in 30 s health-check steps, feeding every alarm."""
    for _ in range(int(seconds // 30)):
        clock.t += 30
        for a in alarms:
            await (a.ok() if ok else a.failed(ERROR, ENDPOINTS))


async def test_alert_after_five_minutes_then_hourly_then_recovered(env):
    alarm, clock, sent, sf = env
    a = alarm("engine-solana-migration")
    await a.failed(ERROR, ENDPOINTS)
    await tick([a], clock, 240)
    assert sent == []  # 4 minutes: not yet
    await tick([a], clock, 60)
    assert len(sent) == 1, sent
    msg = sent[0]
    assert msg.startswith("[CRITICAL] YONIXALPHA: no working Solana RPC for 5 min")
    assert "still down" not in msg and "Services affected: engine-solana-migration" in msg
    assert "- helius [dashboard] https://mainnet.helius-rpc.com - rate limited (429), 7 failures in a row" in msg
    assert "key refused (401/403)" in msg and "Dashboard > RPC providers" in msg
    assert "FAKE-TEST-KEY" not in msg and "api-key" not in msg  # the key in the error URL is gone
    await tick([a], clock, 30 * 60)
    assert len(sent) == 1  # no repeat inside the hour
    await tick([a], clock, 30 * 60)
    assert len(sent) == 2 and "still down" in sent[1] and "for 65 min" in sent[1]
    await tick([a], clock, 30, ok=True)
    assert len(sent) == 3 and sent[2].startswith("[RECOVERED]") and "outage lasted 65 min" in sent[2]
    await tick([a], clock, 300, ok=True)
    assert len(sent) == 3
    async with sf() as s:
        rows = (await s.execute(select(SystemEvent).order_by(SystemEvent.created_at))).scalars().all()
    kinds = [(r.event_type, r.severity) for r in rows]
    assert kinds == [("rpc_outage", "critical"), ("rpc_outage", "critical"), ("rpc_outage_recovered", "info")]
    assert "FAKE-TEST-KEY" not in str([r.detail for r in rows])


async def test_a_short_blip_sends_nothing(env):
    alarm, clock, sent, sf = env
    a = alarm("data-solana")
    await tick([a], clock, 270)
    await tick([a], clock, 30, ok=True)
    await tick([a], clock, 240)
    assert sent == []  # the second outage started over at zero
    async with sf() as s:
        assert (await s.execute(select(SystemEvent))).scalars().all() == []


async def test_four_services_share_one_alert_and_one_recovery(env):
    alarm, clock, sent, _ = env
    names = ["data-solana", "engine-solana-discovery", "engine-solana-migration", "engine-solana-momentum"]
    alarms = [alarm(n) for n in names]
    await tick(alarms, clock, 600)
    assert len(sent) == 1, sent
    assert f"Services affected: {', '.join(names)}" in sent[0]
    await tick(alarms, clock, 3600)
    assert len(sent) == 2 and "still down" in sent[1]
    await tick(alarms[:3], clock, 30, ok=True)
    assert len(sent) == 2  # momentum is still down: no "working again" yet
    await tick(alarms[3:], clock, 30, ok=True)
    assert len(sent) == 3 and "working again on all services" in sent[2]
    # the next outage alerts again from the start
    await tick(alarms, clock, 330)
    assert len(sent) == 4 and "still down" not in sent[3]


async def test_without_redis_each_service_still_alerts(env):
    _, clock, sent, sf = env

    class Down:
        async def hset(self, *a, **k):
            raise ConnectionError("redis down")

        async def hdel(self, *a, **k):
            raise ConnectionError("redis down")

    async def send(text):
        sent.append(text)
        return True

    a = ro.RpcOutageAlarm("engine-solana-momentum", sf, redis=Down(), send=send, clock=clock)
    await tick([a], clock, 330)
    assert len(sent) == 1 and "engine-solana-momentum" in sent[0]
    await tick([a], clock, 30, ok=True)
    assert len(sent) == 2 and sent[1].startswith("[RECOVERED]")


async def test_a_failing_telegram_never_breaks_the_health_loop(env):
    _, clock, _, sf = env

    async def boom(text):
        raise RuntimeError("telegram down")

    a = ro.RpcOutageAlarm("data-solana", sf, redis=None, send=boom, clock=clock)
    a._redis_client = lambda: None  # no Redis either
    await tick([a], clock, 330)
    assert await a.ok() is False


async def test_an_outage_open_across_a_restart_is_closed_by_the_first_success(env):
    alarm, clock, sent, _ = env
    before = alarm("engine-solana-discovery")
    await tick([before], clock, 330)
    assert len(sent) == 1  # alerted, then the services were redeployed mid-outage
    after = alarm("engine-solana-discovery")  # a new process
    clock.t += 200  # the old failing entry is no longer fresh
    assert await after.ok() is True
    assert sent[1].startswith("[RECOVERED]") and "outage lasted 8 min" in sent[1]
    assert await after.ok() is False  # once only
    # the next outage starts from zero: a first alert, not a reminder
    await tick([after], clock, 330)
    assert len(sent) == 3 and "still down" not in sent[2] and "for 5 min" in sent[2]


async def test_a_healthy_start_sends_nothing(env):
    alarm, clock, sent, _ = env
    a = alarm("data-solana")
    for _ in range(3):
        clock.t += 30
        assert await a.ok() is False
    assert sent == []
