"""data-evm: 24/7 BSC and Robinhood Chain launchpad pipeline (discovery,
categories, safety, paper entries, position management, evidence).

Settings (platform_settings "evm_trading", trading controls, launchpad
modes) are read from the database on every pass, so dashboard changes apply
without a restart. Read-only on chain: nothing is signed or sent.
"""

import asyncio
import signal
import time

import httpx

from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.evm import EVM_LAUNCHPADS, adapter_for
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm.rpc import EvmRpcUnavailableError, make_rpc
from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.runtime_watch import run_watcher

from app.worker import RPC_OUTAGE_ALERT_SECONDS, SERVICE, ChainWorker, utcnow

log = get_logger("data-evm.main")
DISCOVERY_SECONDS = 3.0
SAFETY_SECONDS = 10.0
PRUNE_SECONDS = 6 * 3600


async def _system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await alert_error(SERVICE, event_type, detail)


async def chain_loop(worker: ChainWorker, stop: asyncio.Event) -> None:
    """Discovery + management every few seconds; safety and entries less
    often; evidence every 30 minutes; old trades pruned every 6 hours."""
    await worker.restore()
    last_safety = last_prune = 0.0
    rpc_down_since: float | None = None
    while not stop.is_set():
        now = utcnow()
        try:
            async with worker.session_factory() as session:
                s = await evm_settings.load(session)
            worker.status["discovery"] = await worker.discovery_pass(s, now)
            worker.status["positions"] = await worker.manage_pass(now)
            if time.monotonic() - last_safety >= SAFETY_SECONDS:
                last_safety = time.monotonic()
                worker.status["safety_checked"] = await worker.safety_pass(s, now)
                worker.status["entries"] = await worker.entry_pass(s, now)
            recorded = await worker.evidence_pass(now)
            if recorded:
                worker.status["evidence_rows"] = recorded
            if time.monotonic() - last_prune >= PRUNE_SECONDS:
                last_prune = time.monotonic()
                worker.status["pruned_trades"] = await worker.prune(now)
            await worker.rpc.publish_health(worker.redis)
            worker.status["ok_at"] = now.isoformat()
            rpc_down_since = None
        except EvmRpcUnavailableError as exc:
            worker.status["rpc_unavailable"] = str(exc)[:200]
            log.warning("data-evm.rpc_unavailable", chain=worker.chain, error=str(exc)[:200])
            # A short public-node cooldown loses nothing (cursors resume): alerted once it persists.
            rpc_down_since = rpc_down_since or time.monotonic()
            down_s = time.monotonic() - rpc_down_since
            if down_s >= RPC_OUTAGE_ALERT_SECONDS:
                await alert_error(SERVICE, f"{worker.chain}.rpc_unavailable",
                                  f"{str(exc)[:300]} (for {down_s:.0f}s)")
        except Exception as exc:  # noqa: BLE001 - the loop itself never dies
            log.error("data-evm.loop_failed", chain=worker.chain, error=str(exc)[:200])
            await alert_error(SERVICE, f"{worker.chain}.loop_failed", f"{type(exc).__name__}: {str(exc)[:300]}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=DISCOVERY_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    http = httpx.AsyncClient(timeout=10.0)  # block-explorer lookups (launch coordination)
    workers = []
    for chain in (Chain.BSC, Chain.ROBINHOOD):
        rpc = make_rpc(chain.value, settings)
        adapters = [adapter_for(k, rpc) for k in EVM_LAUNCHPADS if LAUNCHPADS[k].chain == chain]
        workers.append(ChainWorker(chain.value, rpc, adapters, session_factory, redis,
                                   etherscan_key=settings.ETHERSCAN_API_KEY, http=http))
    await _system_event(session_factory, "service_started", "info", {"chains": [w.chain for w in workers]})
    log.info("data-evm.started", chains=[w.chain for w in workers])
    try:
        await asyncio.gather(
            *(chain_loop(w, stop) for w in workers),
            heartbeat_loop(settings, SERVICE, stop, lambda: {w.chain: {**w.status, "rpc": w.rpc.health()} for w in workers}),
            run_watcher(SERVICE, settings, session_factory, stop, redis=redis, evm_rpcs={w.chain: w.rpc for w in workers}),
        )
    finally:
        await _system_event(session_factory, "service_stopped", "info")
        for w in workers:
            await w.rpc.aclose()
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
