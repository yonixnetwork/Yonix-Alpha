import asyncio
import signal

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert

from app.client import BinanceFuturesClient
from app.events import dispatch_user_stream_message
from app.orders import reconcile_pending_orders
from app.positions import sync_positions
from app.user_stream import UserDataStreamClient

log = get_logger("engine-binance-futures.main")

RECONCILE_INTERVAL_SECONDS = 60
POSITION_SYNC_INTERVAL_SECONDS = 30

SERVICE_NAME = "engine-binance-futures"


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"⚠️ [{SERVICE_NAME}] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


async def _reconcile_loop(client: BinanceFuturesClient, session_factory, stop_event: asyncio.Event) -> None:
    """Read-only against the exchange (queries by clientOrderId, never
    resubmits) — runs regardless of TRADING_ENABLED/LIVE_TRADING_ENABLED,
    since reflecting the true state of orders this process itself placed
    earlier is not a new trading action.
    """
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                reconciled = await reconcile_pending_orders(session, client)
            if reconciled:
                log.info("reconcile.updated", count=len(reconciled))
        except Exception as exc:  # noqa: BLE001
            log.error("reconcile.failed", error=str(exc))
            await _record_system_event(session_factory, "order_reconcile_failed", "error", {"error": str(exc)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RECONCILE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _position_sync_loop(client: BinanceFuturesClient, session_factory, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                await sync_positions(session, client)
        except Exception as exc:  # noqa: BLE001
            log.error("position_sync.failed", error=str(exc))
            await _record_system_event(session_factory, "position_sync_failed", "error", {"error": str(exc)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POSITION_SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
        log.warning("engine-binance-futures.disabled", reason="BINANCE_API_KEY/BINANCE_API_SECRET not set")
        return

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    async with httpx.AsyncClient() as http_client:
        client = BinanceFuturesClient(
            http_client, api_key=settings.BINANCE_API_KEY, api_secret=settings.BINANCE_API_SECRET, testnet=settings.BINANCE_TESTNET
        )

        async def handle_user_stream_message(message: dict) -> None:
            async with session_factory() as session:
                await dispatch_user_stream_message(session, message)

        user_stream = UserDataStreamClient(rest_client=client, on_message=handle_user_stream_message, testnet=settings.BINANCE_TESTNET)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await _record_system_event(session_factory, "service_started", "info", {"testnet": settings.BINANCE_TESTNET})
        log.info(
            "engine-binance-futures.started",
            testnet=settings.BINANCE_TESTNET,
            trading_enabled=settings.TRADING_ENABLED,
            live_trading_enabled=settings.LIVE_TRADING_ENABLED,
        )

        try:
            await asyncio.gather(
                heartbeat_loop(settings, "engine-binance-futures", stop_event, None),
                _reconcile_loop(client, session_factory, stop_event),
                _position_sync_loop(client, session_factory, stop_event),
                user_stream.run(stop_event),
            )
        finally:
            await _record_system_event(session_factory, "service_stopped", "info")
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
