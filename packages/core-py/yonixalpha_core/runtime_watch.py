"""One call per service to run the runtime-configuration watcher, with the
RPC / WebSocket reloaders when the service holds RPC endpoints in memory."""

import asyncio

from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.runtime_config import RuntimeConfigWatcher
from yonixalpha_core.solana import rpc_registry


async def run_watcher(service: str, settings, session_factory, stop_event: asyncio.Event, rpc=None,
                      ws_urls: "rpc_registry.WsUrls | None" = None, redis=None) -> None:
    own_redis = redis is None
    redis = redis or make_redis(settings)
    reloaders, status = {}, {}
    if rpc is not None:
        reloaders = rpc_registry.make_reloaders(service, rpc, settings, session_factory, redis, ws_urls)
        status = {"rpc": lambda: rpc_registry.rpc_status(rpc)}
    watcher = RuntimeConfigWatcher(service, session_factory, redis, reloaders, status)
    try:
        await watcher.run(stop_event)
    finally:
        if own_redis:
            await redis.aclose()
