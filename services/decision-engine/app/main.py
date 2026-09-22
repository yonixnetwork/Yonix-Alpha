import asyncio
import signal
from datetime import datetime, timezone

from sqlalchemy import select

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent, TradingCandidate
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.state_machine import CandidateState

from app.evaluate import evaluate_candidate

log = get_logger("decision-engine.main")

EVAL_INTERVAL_SECONDS = 15
SERVICE_NAME = "decision-engine"
EVALUABLE_STATES = [CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value]


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"⚠️ [{SERVICE_NAME}] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


async def _evaluation_loop(session_factory, redis, settings, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                result = await session.execute(select(TradingCandidate.id).where(TradingCandidate.state.in_(EVALUABLE_STATES)))
                candidate_ids = result.scalars().all()

            evaluated = 0
            for candidate_id in candidate_ids:
                # Each candidate gets its own session/transaction, so one
                # candidate's failure (a bad row, a transient DB error)
                # can't abort evaluation of the rest of the batch.
                try:
                    async with session_factory() as session:
                        candidate = await session.get(TradingCandidate, candidate_id)
                        if candidate is None or candidate.state not in EVALUABLE_STATES:
                            continue  # state changed since the query above
                        await evaluate_candidate(session, redis, settings, candidate, datetime.now(timezone.utc))
                    evaluated += 1
                except Exception as exc:  # noqa: BLE001
                    log.error("evaluate.candidate_failed", candidate_id=str(candidate_id), error=str(exc))
                    await _record_system_event(
                        session_factory, "candidate_evaluation_failed", "error", {"candidate_id": str(candidate_id), "error": str(exc)}
                    )

            if evaluated:
                log.info("evaluation_loop.completed", count=evaluated)
        except Exception as exc:  # noqa: BLE001
            log.error("evaluation_loop.failed", error=str(exc))
            await _record_system_event(session_factory, "evaluation_loop_failed", "error", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EVAL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await _record_system_event(
        session_factory,
        "service_started",
        "info",
        {"trading_enabled": settings.TRADING_ENABLED, "live_trading_enabled": settings.LIVE_TRADING_ENABLED},
    )
    log.info(
        "decision-engine.started",
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
    )

    try:
        await _evaluation_loop(session_factory, redis, settings, stop_event)
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
