"""execution-futures: LIVE execution for the derivatives/FX strategies.

One process, four duties, each isolated so a failure in one never stops
the others:
- order worker (every POLL_SECONDS): runs PENDING futures orders through
  their venue's provider (exits before entries);
- position manager (every MANAGE_SECONDS): marks open LIVE futures
  positions at the venue's book mid and applies the shared exit logic;
- reconciler (every RECONCILE_SECONDS, and first thing at start-up):
  exchange balance, orders left SUBMITTED, flat-on-exchange positions,
  missing protection; publishes each venue's readiness, which the
  decision engine requires before creating any LIVE order;
- live Hyperliquid grid (grid_live, with the manager): post-only orders,
  confirmed fills, exchange-side stop on the net position;
- external bots (every BOTS_SECONDS): reads the standalone bots' control
  APIs so the decision engine can refuse LIVE while one trades the same
  strategy.

With the environment locks closed (TRADING_ENABLED, LIVE_TRADING_ENABLED,
PAPER_TRADING=false) nothing is sent: every venue reports "disabled" and
any PENDING futures order is cancelled, never executed.
"""

import asyncio
import signal
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from yonixalpha_core import external_bots, futures_live, grid_live
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.execution.base import OrderState
from yonixalpha_core.execution.registry import FUTURES_PROVIDERS, build_providers
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.safety.store import live_trading_permitted
from yonixalpha_core.venues.common import VenueError, venue_health_snapshot
from yonixalpha_core.venues.registry import build_venues

log = get_logger("execution-futures.main")

SERVICE_NAME = "execution-futures"
POLL_SECONDS = 2
MANAGE_SECONDS = 5
RECONCILE_SECONDS = 30
BOTS_SECONDS = 30


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(get_settings(), f"⚠️ [{SERVICE_NAME}] {severity.upper()}: {event_type}"
                                  + (f"\n{detail}" if detail else ""))


def price_fn_for(venues: dict):
    async def price(venue: str, symbol: str) -> Decimal | None:
        adapter = venues.get(venue)
        if adapter is None:
            return None
        try:
            if venue == "mt5":
                return await adapter.mid(symbol)
            return (await adapter.book(symbol)).mid
        except VenueError as exc:
            log.warning("price.unavailable", venue=venue, symbol=symbol, error=str(exc)[:160])
            return None
    return price


async def cancel_pending(session_factory, redis, app_settings, providers, reason: str) -> int:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        orders = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "PENDING",
            ExecutionOrder.provider.in_(FUTURES_PROVIDERS)))).scalars().all()
        for order in orders:
            await futures_live.apply_state(session, redis, app_settings, providers, order,
                                           OrderState("", "REJECTED", error=reason), now)
            order.status = "CANCELLED"
        await session.commit()
    return len(orders)


async def process_pending(session_factory, redis, app_settings, providers) -> int:
    async with session_factory() as session:
        ids = (await session.execute(select(ExecutionOrder.id).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "PENDING",
            ExecutionOrder.provider.in_(FUTURES_PROVIDERS))
            .order_by((ExecutionOrder.reason == "entry").asc(), ExecutionOrder.created_at))).scalars().all()  # exits first
    for order_id in ids:
        status = await futures_live.process_order(session_factory, redis, app_settings, providers, order_id)
        log.info("order.processed", order_id=str(order_id), status=status)
    return len(ids)


async def tick(session_factory, redis, app_settings, providers, venues, now, state: dict, http_client=None) -> dict:
    """One pass of every duty that is due. Returns what ran (for tests/logs)."""
    ran: dict = {}
    if not live_trading_permitted(app_settings):
        async with session_factory() as session:
            live = await futures_live.load_settings(session)
        for venue in providers:
            await futures_live.publish_readiness(redis, venue, "disabled", "environment locks closed", now, live)
        n = await cancel_pending(session_factory, redis, app_settings, providers, "live execution disabled (environment locks)")
        ran["cancelled"] = n
    else:
        if now.timestamp() - state.get("reconciled", 0) >= RECONCILE_SECONDS:
            reports = []
            for venue, provider in providers.items():
                try:
                    reports.append(await futures_live.reconcile_venue(session_factory, redis, app_settings, venue, provider, now))
                except Exception as exc:  # noqa: BLE001 - one venue must not block the others
                    log.error("reconcile.failed", venue=venue, error=f"{type(exc).__name__}: {exc}")
                    await _record_system_event(session_factory, "futures_reconcile_failed", "error",
                                               {"venue": venue, "error": str(exc)[:300]})
            state["reconciled"] = now.timestamp()
            ran["reconcile"] = reports
        ran["orders"] = await process_pending(session_factory, redis, app_settings, providers)
        if now.timestamp() - state.get("managed", 0) >= MANAGE_SECONDS:
            ran["manage"] = await futures_live.manage(session_factory, providers, price_fn_for(venues), now)
            state["managed"] = now.timestamp()
            hl = providers.get("hyperliquid")
            if hl is not None and hl.configured and "hyperliquid" in venues:
                try:
                    ran["grid"] = await grid_live.run(session_factory, redis, app_settings, hl, venues["hyperliquid"].mid, now)
                except Exception as exc:  # noqa: BLE001 - the grid must not stop the other duties
                    log.error("grid.failed", error=f"{type(exc).__name__}: {exc}")
                    await _record_system_event(session_factory, "grid_live_failed", "error", {"error": str(exc)[:300]})
    if http_client is not None and now.timestamp() - state.get("bots", 0) >= BOTS_SECONDS:
        ran["bots"] = await external_bots.poll_all(http_client, app_settings, redis, now)
        state["bots"] = now.timestamp()
    return ran


async def loop(session_factory, redis, app_settings, providers, venues, http_client, stop_event: asyncio.Event) -> None:
    state: dict = {}
    while not stop_event.is_set():
        try:
            await tick(session_factory, redis, app_settings, providers, venues, datetime.now(timezone.utc), state, http_client)
        except Exception as exc:  # noqa: BLE001 - keep running; the error is reported
            log.error("loop.failed", error=f"{type(exc).__name__}: {exc}")
            await _record_system_event(session_factory, "execution_futures_loop_failed", "error", {"error": str(exc)[:300]})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    stop_event = asyncio.Event()
    ev_loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        ev_loop.add_signal_handler(sig, stop_event.set)
    redis = make_redis(settings)
    http_client = httpx.AsyncClient()
    providers = build_providers(http_client, settings)
    venues = build_venues(http_client, settings)
    await _record_system_event(session_factory, "service_started", "info",
                               {"configured": [v for v, p in providers.items() if p.configured],
                                "live_permitted": live_trading_permitted(settings)})
    log.info("execution-futures.started", configured=[v for v, p in providers.items() if p.configured])
    try:
        await asyncio.gather(
            loop(session_factory, redis, settings, providers, venues, http_client, stop_event),
            heartbeat_loop(settings, SERVICE_NAME, stop_event, lambda: {"venues": venue_health_snapshot()}),
        )
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
