"""copy-engine main loop: the wallet profile rebuild (minutes on a month of
Solana launch buyers) runs beside the loop, so watching the targets goes on
meanwhile, and only one rebuild runs at a time."""

import asyncio
from types import SimpleNamespace

from app import main

MULTI_CHAIN = SimpleNamespace(SYSTEM_PROFILE="MULTI_CHAIN", COPY_TRADING_ENABLED=True)


class SlowEngine:
    def __init__(self, run="FULL", open_copies=0, settings=MULTI_CHAIN):
        self.status = {}
        self.settings = settings
        self.solana_passes = 0
        self.rebuilds = 0
        self.release = asyncio.Event()
        self.run, self.open_copies = run, open_copies
        self.calls: dict[str, int] = {}

    def _n(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    async def policy(self):
        return {"run": self.run, "status": {"FULL": "ACTIVE", "THROTTLED": "THROTTLED"}.get(self.run, "SUSPENDED")}

    async def open_copy_positions(self):
        return self.open_copies

    async def watch_solana(self):
        self.solana_passes += 1
        return {}

    async def refresh_adapters(self):
        self._n("refresh_adapters")
        return {}

    async def watch_evm(self, chain):
        self._n("watch_evm")
        return {}

    async def manage_evm(self, chain):
        self._n("manage_evm")
        return {}

    async def evaluate_outcomes(self):
        self._n("evaluate_outcomes")
        return {}

    async def enrich(self):
        self._n("enrich")
        return {}

    async def rebuild_profiles(self):
        self.rebuilds += 1
        await self.release.wait()  # a rebuild that takes "minutes"
        return {"solana": 1}


async def test_watching_goes_on_while_the_profiles_are_rebuilt(monkeypatch):
    monkeypatch.setattr(main, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(main, "PROFILES_EVERY", 0.0)  # due on every pass: still one rebuild at a time
    engine, stop = SlowEngine(), asyncio.Event()
    task = asyncio.create_task(main.loop(engine, stop))
    await asyncio.sleep(0.3)
    assert engine.rebuilds == 1 and engine.solana_passes >= 10  # the loop did not wait for the rebuild
    assert "profiles" not in engine.status
    engine.release.set()
    await asyncio.sleep(0.1)
    assert engine.status["profiles"] == {"solana": 1} and engine.rebuilds >= 2  # the next one started after it ended
    engine.release.clear()
    stop.set()
    await asyncio.wait_for(task, 2)  # a rebuild still running is cancelled on shutdown


async def _run(engine, seconds=0.25):
    stop = asyncio.Event()
    task = asyncio.create_task(main.loop(engine, stop))
    await asyncio.sleep(seconds)
    stop.set()
    engine.release.set()
    await asyncio.wait_for(task, 2)


async def test_suspended_copy_trading_only_protects_open_copy_positions(monkeypatch):
    """SUSPENDED: no target watching, outcomes, profiles or enrichment; the
    copy positions already open are still managed (stop loss / exits), and
    their venues are registered for quoting only while any is open."""
    for k in ("TICK_SECONDS",):
        monkeypatch.setattr(main, k, 0.01)
    for k in ("EVM_EVERY", "MANAGE_EVERY", "OUTCOMES_EVERY", "PROFILES_EVERY", "ENRICH_EVERY", "ADAPTERS_EVERY"):
        monkeypatch.setattr(main, k, 0.0)
    idle = SlowEngine(run="PROTECT_ONLY", open_copies=0)
    await _run(idle)
    assert idle.solana_passes == 0 and idle.rebuilds == 0
    assert set(idle.calls) == {"manage_evm"} and idle.status["copy_trading"]["status"] == "SUSPENDED"
    holding = SlowEngine(run="PROTECT_ONLY", open_copies=2)
    await _run(holding)
    assert set(holding.calls) == {"manage_evm", "refresh_adapters"} and holding.solana_passes == 0


async def test_throttled_copy_trading_watches_less_often_without_analytics(monkeypatch):
    monkeypatch.setattr(main, "TICK_SECONDS", 0.01)
    for k in ("EVM_EVERY", "MANAGE_EVERY", "OUTCOMES_EVERY", "PROFILES_EVERY", "ENRICH_EVERY", "ADAPTERS_EVERY"):
        monkeypatch.setattr(main, k, 0.0)
    monkeypatch.setattr(main, "THROTTLED_EVM_EVERY", 3600.0)
    monkeypatch.setattr(main, "THROTTLED_OUTCOMES_EVERY", 3600.0)
    eng = SlowEngine(run="THROTTLED")
    await _run(eng)
    assert eng.calls["watch_evm"] == 2  # once per chain, then not again within the throttled interval
    assert eng.calls["evaluate_outcomes"] == 1 and "enrich" not in eng.calls and eng.rebuilds == 0
    assert eng.solana_passes >= 5 and eng.calls["manage_evm"] >= 10


async def test_solana_only_profile_never_watches_or_manages_evm_copies(monkeypatch):
    """SYSTEM_PROFILE=SOLANA_ONLY: copy trading (if switched on) watches
    Solana targets only; BSC / Robinhood are neither watched nor managed."""
    monkeypatch.setattr(main, "TICK_SECONDS", 0.01)
    for k in ("EVM_EVERY", "MANAGE_EVERY", "OUTCOMES_EVERY", "ADAPTERS_EVERY"):
        monkeypatch.setattr(main, k, 0.0)
    engine = SlowEngine(run="FULL", settings=SimpleNamespace(SYSTEM_PROFILE="SOLANA_ONLY", COPY_TRADING_ENABLED=True,
                                                             CHAIN_BSC_ENABLED=True))
    await _run(engine)
    assert engine.solana_passes > 0
    assert "watch_evm" not in engine.calls and "manage_evm" not in engine.calls
