"""copy-engine main loop: the wallet profile rebuild (minutes on a month of
Solana launch buyers) runs beside the loop, so watching the targets goes on
meanwhile, and only one rebuild runs at a time."""

import asyncio

from app import main


class SlowEngine:
    def __init__(self):
        self.status = {}
        self.solana_passes = 0
        self.rebuilds = 0
        self.release = asyncio.Event()

    async def watch_solana(self):
        self.solana_passes += 1
        return {}

    async def refresh_adapters(self):
        return {}

    async def watch_evm(self, chain):
        return {}

    async def manage_evm(self, chain):
        return {}

    async def evaluate_outcomes(self):
        return {}

    async def enrich(self):
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
