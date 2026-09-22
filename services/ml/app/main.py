import asyncio
import signal

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.logging import configure_logging, get_logger

from app.train import train_and_maybe_register

log = get_logger("ml.main")

# Training is infrequent by nature (a labeled dataset changes on the order
# of closed positions, not seconds) — an hourly check is plenty, and cheap
# to run even while it's doing nothing but recording
# training_skipped_insufficient_samples, which per docs/ML.md is the
# expected outcome for a long time.
TRAIN_INTERVAL_SECONDS = 3600
SERVICE_NAME = "ml"


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()


async def _training_loop(session_factory, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                outcome = await train_and_maybe_register(session)
            log.info("training_loop.completed", status=outcome.status, available_samples=outcome.available_samples)
            await _record_system_event(
                session_factory,
                f"training_{outcome.status}",
                "info",
                {"available_samples": outcome.available_samples, "metrics": outcome.metrics},
            )
        except Exception as exc:  # noqa: BLE001
            log.error("training_loop.failed", error=str(exc))
            await _record_system_event(session_factory, "training_loop_failed", "error", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TRAIN_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await _record_system_event(session_factory, "service_started", "info")
    log.info("ml.started")

    try:
        await _training_loop(session_factory, stop_event)
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
