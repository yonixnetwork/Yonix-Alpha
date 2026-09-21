import asyncio
import os
import signal

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.writers import write_market_snapshot
from yonixalpha_core.logging import configure_logging, get_logger

from app.ingest import normalize_logs_notification, normalize_slot_notification
from app.rpc.manager import RpcManager
from app.ws.client import SolanaWsClient

log = get_logger("data-solana.main")

# Additional program/account addresses to watch via logsSubscribe, one per
# line, comma-separated. Left empty by default — see app/ingest.py for why
# this service doesn't ship a hardcoded launch-platform program ID.
WATCHED_ADDRESSES = [a.strip() for a in os.getenv("SOLANA_WATCHED_ADDRESSES", "").split(",") if a.strip()]

HEALTH_CHECK_INTERVAL_SECONDS = 30


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service="data-solana", event_type=event_type, severity=severity, detail=detail))
        await session.commit()


async def _health_check_loop(rpc: RpcManager, session_factory, stop_event: asyncio.Event) -> None:
    """Periodically calls getHealth against the currently-preferred endpoint
    so RPC failover state is exercised even during a quiet WS period, and so
    an operator has a System Event trail of RPC health over time.
    """
    while not stop_event.is_set():
        try:
            await rpc.call("getHealth")
            log.info("rpc.health_check.ok", endpoints=rpc.health_snapshot())
        except Exception as exc:  # noqa: BLE001
            log.error("rpc.health_check.failed", error=str(exc), endpoints=rpc.health_snapshot())
            await _record_system_event(
                session_factory, "rpc_health_check_failed", "error", {"error": str(exc), "endpoints": rpc.health_snapshot()}
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEALTH_CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


def _build_subscriptions() -> list[dict]:
    subs: list[dict] = [{"jsonrpc": "2.0", "method": "slotSubscribe", "params": []}]
    for address in WATCHED_ADDRESSES:
        subs.append(
            {
                "jsonrpc": "2.0",
                "method": "logsSubscribe",
                "params": [{"mentions": [address]}, {"commitment": "confirmed"}],
            }
        )
    return subs


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    if not settings.SOLANA_RPC_URL:
        log.warning("data-solana.disabled", reason="SOLANA_RPC_URL not set")
        return

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    async def handle_message(message: dict) -> None:
        method = message.get("method", "")
        event = None
        if method == "slotNotification":
            event = normalize_slot_notification(message)
        elif method == "logsNotification":
            event = normalize_logs_notification(message)
        if event is None:
            return
        async with session_factory() as session:
            await write_market_snapshot(session, event)

    async with httpx.AsyncClient() as http_client:
        rpc = RpcManager.create(
            client=http_client,
            primary_url=settings.SOLANA_RPC_URL,
            backup_url=settings.SOLANA_RPC_BACKUP_URL,
        )

        ws_urls = [u for u in (settings.SOLANA_WS_URL, settings.SOLANA_WS_BACKUP_URL) if u]
        if not ws_urls:
            log.warning("data-solana.ws_disabled", reason="SOLANA_WS_URL not set")
            await engine.dispose()
            return

        ws_index = {"i": 0}

        def next_ws_url() -> str:
            url = ws_urls[ws_index["i"] % len(ws_urls)]
            ws_index["i"] += 1
            return url

        ws_client = SolanaWsClient(
            url_provider=next_ws_url,
            subscriptions=_build_subscriptions(),
            on_message=handle_message,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await _record_system_event(session_factory, "service_started", "info")
        log.info("data-solana.started", watched_addresses=WATCHED_ADDRESSES)

        try:
            await asyncio.gather(
                ws_client.run(stop_event),
                _health_check_loop(rpc, session_factory, stop_event),
            )
        finally:
            await _record_system_event(session_factory, "service_stopped", "info")
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
