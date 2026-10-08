"""copy-engine: 24/7 paper copy trading on Solana, BSC and Robinhood Chain,
plus the periodic wallet-profile rebuild and the paper outcome of every
target buy (copied or not) once its horizon has passed. Targets, modes and
limits are read from the database on every pass (no restart needed). Paper
only: nothing is signed or sent."""

import asyncio
import signal
import time
from datetime import datetime, timezone

from yonixalpha_core import system_profile
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
ENRICH_EVERY = 600.0  # Nansen / MadeOnSol (off unless switched on, inside a daily call budget)
OUTCOMES_EVERY = 60.0
# THROTTLED copy trading (yonixalpha_core.operating_mode)
THROTTLED_EVM_EVERY = 10.0
THROTTLED_OUTCOMES_EVERY = 600.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def evm_chains(engine: CopyEngine) -> list[str]:
    """The EVM chains the system profile runs (none in SOLANA_ONLY): copy
    trading on Solana does not watch BSC / Robinhood targets there."""
    settings = getattr(engine, "settings", None) or get_settings()
    return [c for c in system_profile.EVM_CHAINS if system_profile.chain_enabled(settings, c)]


async def loop(engine: CopyEngine, stop: asyncio.Event) -> None:
    # -inf: every step is due on the first pass, however long the host has been up
    last = {k: float("-inf") for k in ("evm", "manage", "adapters", "profiles", "outcomes", "enrich")}
    background: dict[str, asyncio.Task] = {}

    async def step(name: str, coro):
        try:
            engine.status[name] = await coro
        except Exception as exc:  # noqa: BLE001 - one failing step never stops the others
            engine.status[name] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            log.error("copy.step_failed", step=name, error=str(exc)[:200])
            await alert_error(SERVICE, f"{name}_failed", engine.status[name])

    def start_background(name: str, make) -> bool:
        """A long step runs beside the loop, one at a time, so watching the
        targets never waits for it: the profile rebuild takes minutes once
        launch_buyers holds a month of launches (about a million rows)."""
        task = background.get(name)
        if task is not None and not task.done():
            return False
        background[name] = asyncio.create_task(step(name, make()))
        return True

    while not stop.is_set():
        t = time.monotonic()
        # Copy trading status (yonixalpha_core.operating_mode): SUSPENDED runs
        # nothing but the protection of copy positions already open.
        pol = await engine.policy()
        run = pol["run"]
        engine.status["copy_trading"] = pol
        throttled = run == "THROTTLED"
        if run != "PROTECT_ONLY":
            await step("solana", engine.watch_solana())
        if t - last["adapters"] >= ADAPTERS_EVERY:
            last["adapters"] = t
            # quotes for open copy positions need their launchpad venues registered
            if run != "PROTECT_ONLY" or await engine.open_copy_positions():
                await step("adapters", engine.refresh_adapters())
        if run != "PROTECT_ONLY" and t - last["evm"] >= (THROTTLED_EVM_EVERY if throttled else EVM_EVERY):
            last["evm"] = t
            for chain in evm_chains(engine):
                await step(f"{chain}_watch", engine.watch_evm(chain))
        if t - last["manage"] >= MANAGE_EVERY:  # never paused: stop loss / trailing / exits of open copies
            last["manage"] = t
            for chain in evm_chains(engine):
                await step(f"{chain}_positions", engine.manage_evm(chain))
        if run != "PROTECT_ONLY" and t - last["outcomes"] >= (THROTTLED_OUTCOMES_EVERY if throttled else OUTCOMES_EVERY):
            last["outcomes"] = t
            await step("outcomes", engine.evaluate_outcomes())
        if run == "FULL" and t - last["profiles"] >= PROFILES_EVERY and start_background("profiles", engine.rebuild_profiles):
            last["profiles"] = t
        if run == "FULL" and t - last["enrich"] >= ENRICH_EVERY:
            last["enrich"] = t
            await step("enrichment", engine.enrich())
        engine.status["ok_at"] = utcnow().isoformat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    for task in background.values():
        task.cancel()
    await asyncio.gather(*background.values(), return_exceptions=True)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    off = system_profile.disabled_reason(settings, SERVICE)
    if off:  # COPY_TRADING_ENABLED=false: deploy.sh does not start this worker; started anyway, it only idles
        await system_profile.idle_while_disabled(settings, SERVICE, off)
        return
    db = make_engine(settings)
    session_factory = make_session_factory(db)
    redis = make_redis(settings)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    rpcs = {c.value: make_rpc(c.value, settings) for c in (Chain.BSC, Chain.ROBINHOOD)}
    adapters = {c: {k: adapter_for(k, rpc) for k in EVM_LAUNCHPADS if LAUNCHPADS[k].chain.value == c}
                for c, rpc in rpcs.items()}
    engine = CopyEngine(session_factory, redis, adapters, utcnow, etherscan_key=settings.ETHERSCAN_API_KEY, settings=settings)
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE, event_type="service_started", severity="info", detail={}))
        await session.commit()
    try:
        await asyncio.gather(
            loop(engine, stop),
            heartbeat_loop(settings, SERVICE, stop, lambda: dict(engine.status)),
            run_watcher(SERVICE, settings, session_factory, stop, redis=redis, evm_rpcs=rpcs),
        )
    finally:
        for rpc in rpcs.values():
            await rpc.aclose()
        await redis.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(run())
