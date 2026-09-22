import asyncio
import signal
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import PaperPosition, SystemEvent, TradingCandidate
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.state_machine import CandidateState

from app.entry import try_open_position
from app.manage import evaluate_open_position
from app.pricing import latest_price

log = get_logger("paper-trading.main")

LOOP_INTERVAL_SECONDS = 15
SERVICE_NAME = "paper-trading"


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"⚠️ [{SERVICE_NAME}] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


async def _open_qualified_candidates(session_factory, now: datetime) -> int:
    async with session_factory() as session:
        result = await session.execute(select(TradingCandidate.id).where(TradingCandidate.state == CandidateState.QUALIFIED.value))
        candidate_ids = result.scalars().all()

    opened = 0
    for candidate_id in candidate_ids:
        # Per-candidate isolation, matching services/decision-engine's own
        # loop: one bad row must not abort the rest of the batch, and must
        # never prevent _manage_open_positions below from running at all.
        try:
            async with session_factory() as session:
                candidate = await session.get(TradingCandidate, candidate_id)
                if candidate is None or candidate.state != CandidateState.QUALIFIED.value:
                    continue  # state changed since the query above
                position = await try_open_position(session, candidate, now)
                if position is not None:
                    opened += 1
        except Exception as exc:  # noqa: BLE001
            log.error("entry.candidate_failed", candidate_id=str(candidate_id), error=str(exc))
            await _record_system_event(
                session_factory, "paper_entry_failed", "error", {"candidate_id": str(candidate_id), "error": str(exc)}
            )
    return opened


async def _manage_open_positions(session_factory, now: datetime, per_leg_cost_bps: Decimal = Decimal(0)) -> int:
    async with session_factory() as session:
        result = await session.execute(select(PaperPosition.id).where(PaperPosition.status == "open"))
        position_ids = result.scalars().all()

    closed = 0
    for position_id in position_ids:
        # Per-position isolation. Without it, a single position that raises
        # while closing (a zero cost basis, a bad row, a transient DB error)
        # aborts the whole batch — meaning every *other* open position's
        # stop-loss silently stops being evaluated, every cycle, for as long
        # as the bad row exists. That is the worst failure mode this service
        # has, so it is contained here rather than left to the loop above.
        try:
            async with session_factory() as session:
                position = await session.get(PaperPosition, position_id)
                if position is None or position.status != "open":
                    continue
                price = await latest_price(session, position.symbol, now)
                if price is None:
                    log.info("manage.no_price_available", symbol=position.symbol, position_id=str(position_id))
                    continue
                if await evaluate_open_position(session, position, price, now, per_leg_cost_bps=per_leg_cost_bps):
                    closed += 1
        except Exception as exc:  # noqa: BLE001
            log.error("manage.position_failed", position_id=str(position_id), error=str(exc))
            await _record_system_event(
                session_factory, "paper_manage_failed", "error", {"position_id": str(position_id), "error": str(exc)}
            )
    return closed


async def _paper_trading_loop(session_factory, stop_event: asyncio.Event, per_leg_cost_bps: Decimal = Decimal(0)) -> None:
    while not stop_event.is_set():
        now = datetime.now(timezone.utc)
        try:
            opened = await _open_qualified_candidates(session_factory, now)
            closed = await _manage_open_positions(session_factory, now, per_leg_cost_bps)
            if opened or closed:
                log.info("loop.completed", opened=opened, closed=closed)
        except Exception as exc:  # noqa: BLE001
            log.error("loop.failed", error=str(exc))
            await _record_system_event(session_factory, "paper_trading_loop_failed", "error", {"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LOOP_INTERVAL_SECONDS)
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
    log.info("paper-trading.started")

    try:
        await _paper_trading_loop(session_factory, stop_event, settings.PAPER_TRADING_PER_LEG_COST_BPS)
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
