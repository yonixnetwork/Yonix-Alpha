import asyncio
import signal
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import PaperPosition, SystemEvent, TradingCandidate
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.venues.common import venue_health_snapshot
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.solana.market_data import JupiterClient, RateBudget
from yonixalpha_core.solana.rpc import RpcManager, with_priority
from yonixalpha_core.runtime_watch import run_watcher
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.venues.registry import build_venues

from app.entry import try_open_position
from app.gate_manage import manage_gate_positions, track_outcomes
from yonixalpha_core import opportunities
from yonixalpha_core.solana.followups import track_observation_followups
from app.grid_engine import run_grid
from app.live_worker import live_worker_loop
from app.manage import evaluate_open_position
from app.pricing import latest_price

log = get_logger("paper-trading.main")

LOOP_INTERVAL_SECONDS = 15
# Exit quotes for migrated positions only; conservative, see decision-engine.
JUPITER_REQUESTS_PER_MINUTE = 20
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
        # Gate-opened positions (engine set) are managed by app/gate_manage.py.
        result = await session.execute(
            select(PaperPosition.id).where(PaperPosition.status == "open", PaperPosition.engine.is_(None))
        )
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


async def _paper_trading_loop(
    session_factory, stop_event: asyncio.Event, per_leg_cost_bps: Decimal = Decimal(0), redis=None, jupiter=None,
    venues=None, app_settings=None, rpc=None,
) -> None:
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
        if redis is not None:
            try:
                counts = await manage_gate_positions(session_factory, redis, jupiter, now, venues, app_settings, rpc)
                if counts.get("failed"):
                    await _record_system_event(session_factory, "gate_manage_failed", "error", counts)
                elif counts["closed"]:
                    log.info("gate_loop.completed", **counts)
                async with session_factory() as session:
                    await track_outcomes(session, redis, now)
                async with session_factory() as session:
                    await track_observation_followups(session, redis, with_priority(rpc, "background"), now)
                async with session_factory() as session:
                    await opportunities.track(session, redis, now)
                if venues is not None:
                    grid_status = await run_grid(session_factory, redis, app_settings, venues, now)
                    if grid_status.get("fills"):
                        log.info("grid.step", **{k: str(v) for k, v in grid_status.items()})
            except Exception as exc:  # noqa: BLE001
                log.error("gate_loop.failed", error=str(exc))
                await _record_system_event(session_factory, "gate_loop_failed", "error", {"error": str(exc)})

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

    redis = make_redis(settings)
    http_client = httpx.AsyncClient()
    jupiter = JupiterClient(http_client, settings.JUPITER_API_KEY, RateBudget(JUPITER_REQUESTS_PER_MINUTE))
    venues = build_venues(http_client, settings)
    # Pool pricing for migrated positions and the LIVE worker both need RPC;
    # without it pumpswap positions report "unpriced" and LIVE is disabled.
    rpc = (RpcManager.create(client=http_client, primary_url=settings.SOLANA_RPC_URL, backup_url=settings.SOLANA_RPC_BACKUP_URL,
                             extra_backup_urls=[settings.SOLANA_RPC_BACKUP_URL_2, settings.SOLANA_RPC_BACKUP_URL_3])
           if settings.SOLANA_RPC_URL else None)
    try:
        await asyncio.gather(
            _paper_trading_loop(session_factory, stop_event, settings.PAPER_TRADING_PER_LEG_COST_BPS, redis, jupiter,
                                venues, settings, rpc),
            live_worker_loop(session_factory, redis, settings, rpc, http_client, stop_event, jupiter=jupiter),
            heartbeat_loop(settings, "paper-trading", stop_event, lambda: {"venues": venue_health_snapshot()}),
            run_watcher("paper-trading", settings, session_factory, stop_event, rpc=rpc, redis=redis),
        )
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
