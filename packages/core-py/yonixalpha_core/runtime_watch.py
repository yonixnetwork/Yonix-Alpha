"""One call per service to run the runtime-configuration watcher, with the
RPC / WebSocket reloaders when the service holds RPC endpoints in memory."""

import asyncio

from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.runtime_config import RuntimeConfigWatcher
from yonixalpha_core.solana import rpc_registry


async def run_watcher(service: str, settings, session_factory, stop_event: asyncio.Event, rpc=None,
                      ws_urls: "rpc_registry.WsUrls | None" = None, redis=None, evm_rpcs: dict | None = None) -> None:
    own_redis = redis is None
    redis = redis or make_redis(settings)
    reloaders, status = {}, {}
    if rpc is not None:
        if hasattr(rpc, "shared") and getattr(rpc, "shared", None) is None:
            # 429 cooldowns and the background budget are per provider key,
            # so every service shares them through Redis.
            rpc.shared = redis
        reloaders = rpc_registry.make_reloaders(service, rpc, settings, session_factory, redis, ws_urls)
        status = {"rpc": lambda: rpc_registry.rpc_status(rpc)}
    if evm_rpcs:  # BSC / Robinhood endpoints added or changed in the dashboard (chains/evm/rpc_registry)
        from yonixalpha_core.chains.evm import rpc_registry as evm_rpc_registry

        reloaders = {**reloaders, "evm_rpc": evm_rpc_registry.make_reloader(evm_rpcs, settings, session_factory)}
        status = {**status, "evm_rpc": lambda: {c: r.health() for c, r in evm_rpcs.items()}}
    watcher = RuntimeConfigWatcher(service, session_factory, redis, reloaders, status)
    try:
        await watcher.run(stop_event)
    finally:
        if own_redis:
            await redis.aclose()
