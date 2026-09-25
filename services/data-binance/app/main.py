import asyncio
import os
import signal

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.writers import write_market_snapshot
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert

from app.ingest import normalize_stream_message
from app.rest.client import BinanceMarketDataClient
from app.ws.client import BinanceWsClient, combined_stream_url

log = get_logger("data-binance.main")

# Symbols and stream types to watch, e.g. "BTCUSDT,ETHUSDT" / "kline_1m,aggTrade,markPrice".
# Empty by default — the operator opts in once they've confirmed the exact
# stream names against current Binance docs (see app/ingest.py caveat).
SYMBOLS = [s.strip().lower() for s in os.getenv("BINANCE_SYMBOLS", "").split(",") if s.strip()]
STREAM_TYPES = [s.strip() for s in os.getenv("BINANCE_STREAM_TYPES", "kline_1m,aggTrade").split(",") if s.strip()]

REST_HEALTH_CHECK_INTERVAL_SECONDS = 30


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service="data-binance", event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"⚠️ [data-binance] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


async def _rest_health_check_loop(client: BinanceMarketDataClient, session_factory, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await client.ping()
            log.info("rest.health_check.ok", base_url=client.base_url)
        except Exception as exc:  # noqa: BLE001
            log.error("rest.health_check.failed", base_url=client.base_url, error=str(exc))
            await _record_system_event(session_factory, "rest_health_check_failed", "error", {"error": str(exc)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REST_HEALTH_CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


def _build_streams() -> list[str]:
    return [f"{symbol}@{stream_type}" for symbol in SYMBOLS for stream_type in STREAM_TYPES]


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    if not SYMBOLS:
        log.warning("data-binance.disabled", reason="BINANCE_SYMBOLS not set")
        return

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    async def handle_message(message: dict) -> None:
        event = normalize_stream_message(message)
        if event is None:
            return
        async with session_factory() as session:
            await write_market_snapshot(session, event)

    async with httpx.AsyncClient() as http_client:
        rest_client = BinanceMarketDataClient(http_client, testnet=settings.BINANCE_TESTNET)

        streams = _build_streams()
        ws_client = BinanceWsClient(
            url=combined_stream_url(streams, testnet=settings.BINANCE_TESTNET),
            on_message=handle_message,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await _record_system_event(session_factory, "service_started", "info", {"streams": streams})
        log.info("data-binance.started", streams=streams, testnet=settings.BINANCE_TESTNET)

        try:
            await asyncio.gather(
                heartbeat_loop(settings, "data-binance", stop_event, None),
                ws_client.run(stop_event),
                _rest_health_check_loop(rest_client, session_factory, stop_event),
            )
        finally:
            await _record_system_event(session_factory, "service_stopped", "info")
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
