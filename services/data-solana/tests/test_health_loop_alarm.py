"""The 30-second getHealth loop feeds the shared RPC outage alarm and no
longer sends a Telegram error for every single failed check (2026-10-15)."""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

from app import main  # noqa: E402


class FakeRpc:
    def __init__(self, results):
        self.results = list(results)

    async def call(self, method):
        assert method == "getHealth"
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def health_snapshot(self):
        return [{"name": "helius", "source": "dashboard", "url": "https://mainnet.helius-rpc.com"}]


async def test_failures_go_to_the_alarm_and_are_stored_without_a_telegram_each(monkeypatch):
    calls, recorded, stop = [], [], asyncio.Event()

    class Alarm:
        def __init__(self, service, session_factory):
            calls.append(("init", service))

        async def failed(self, error, endpoints):
            calls.append(("failed", error, endpoints[0]["name"]))

        async def ok(self):
            calls.append(("ok",))
            stop.set()

        async def aclose(self):
            calls.append(("close",))

    async def record(session_factory, event_type, severity, detail=None, alert=True):
        recorded.append((event_type, severity, alert))

    monkeypatch.setattr(main, "RpcOutageAlarm", Alarm)
    monkeypatch.setattr(main, "_record_system_event", record)
    monkeypatch.setattr(main, "HEALTH_CHECK_INTERVAL_SECONDS", 0.01)
    rpc = FakeRpc([RuntimeError("all endpoints failed"), RuntimeError("all endpoints failed"), "ok"])
    await asyncio.wait_for(main._health_check_loop(rpc, None, stop), timeout=5)
    assert calls == [("init", "data-solana"), ("failed", "all endpoints failed", "helius"),
                     ("failed", "all endpoints failed", "helius"), ("ok",), ("close",)]
    assert recorded == [("rpc_health_check_failed", "error", False)] * 2
