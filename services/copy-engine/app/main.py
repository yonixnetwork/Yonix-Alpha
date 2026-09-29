"""copy-engine: 24/7 paper copy trading on Solana, BSC and Robinhood Chain,
plus the periodic wallet-profile rebuild. Targets, modes and limits are read
from the database on every pass (no restart needed). Paper only: nothing is
signed or sent."""

import asyncio
import signal
import time
from datetime import datetime, timezone

from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.evm import EVM_LAUNCHPADS, adapter_for
from yonixalpha_core.chains.evm.rpc import make_rpc
from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.runtime_watch import run_watcher

from app.engine import SERVICE, CopyEngine

log = get_logger("copy-engine.main")
TICK_SECONDS = 1.0
EVM_EVERY = 2.0
MANAGE_EVERY = 3.0
ADAPTERS_EVERY = 60.0
PROFILES_EVERY = 600.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def loop(engine: CopyEngine, stop: asyncio.Event) -> None:
    last = {"evm": 0.0, "manage": 0.0, "adapters": 0.0, "profiles": 0.0}

    async def step(name: str, coro):
        try:
            engine.status[name] = await coro
        except Exception as exc:  # noqa: BLE001 - one failing step never stops the others
            engine.status[name] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            log.error("copy.step_failed", step=name, error=str(exc)[:200])
            await alert_error(SERVICE, f"{name}_failed", engine.status[name])

    while not stop.is_set():
        t = time.monotonic()
        await step("solana", engine.watch_solana())
        if t - last["adapters"] >= ADAPTERS_EVERY:
            last["adapters"] = t
            await step("adapters", engine.refresh_adapters())
        if t - last["evm"] >= EVM_EVERY:
            last["evm"] = t
            for chain in ("bsc", "robinhood"):
                await step(f"{chain}_watch", engine.watch_evm(chain))
        if t - last["manage"] >= MANAGE_EVERY:
            last["manage"] = t
            for chain in ("bsc", "robinhood"):
                await step(f"{chain}_positions", engine.manage_evm(chain))
        if t - last["profiles"] >= PROFILES_EVERY:
            last["profiles"] = t
            await step("profiles", engine.rebuild_profiles())
        engine.status["ok_at"] = utcnow().isoformat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    db = make_engine(settings)
    session_factory = make_session_factory(db)
    redis = make_redis(settings)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    rpcs = {c.value: make_rpc(c.value, settings) for c in (Chain.BSC, Chain.ROBINHOOD)}
    adapters = {c: {k: adapter_for(k, rpc) for k in EVM_LAUNCHPADS if LAUNCHPADS[k].chain.value == c}
                for c, rpc in rpcs.items()}
    engine = CopyEngine(session_factory, redis, adapters, utcnow)
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE, event_type="service_started", severity="info", detail={}))
        await session.commit()
    try:
        await asyncio.gather(
            loop(engine, stop),
            heartbeat_loop(settings, SERVICE, stop, lambda: dict(engine.status)),
            run_watcher(SERVICE, settings, session_factory, stop, redis=redis),
        )
    finally:
        for rpc in rpcs.values():
            await rpc.aclose()
        await redis.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(run())
