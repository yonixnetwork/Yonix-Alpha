import asyncio
import signal
from datetime import datetime, timezone

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.safety import store
from sqlalchemy import select

from yonixalpha_core.db.models import PaperPosition
from yonixalpha_core.solana import pump_stream, pumpportal_ws
from yonixalpha_core.solana.pumpfun import PUMP_PROGRAM_ID
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.ws import SolanaWsClient

from app.funnel import run_funnel

log = get_logger("engine-solana-discovery.main")

HEALTH_CHECK_INTERVAL_SECONDS = 30
FUNNEL_INTERVAL_SECONDS = 10
STATS_LOG_EVERY = 6  # funnel runs


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service="engine-solana-discovery", event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(),
            f"⚠️ [engine-solana-discovery] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else ""),
        )


async def _health_check_loop(rpc: RpcManager, session_factory, stop_event: asyncio.Event) -> None:
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


async def _funnel_loop(redis, session_factory, stop_event: asyncio.Event) -> None:
    runs = 0
    while not stop_event.is_set():
        try:
            async with session_factory() as session:
                settings, _ = await store.load_settings(session, "solana_fresh")
            counts = await run_funnel(redis, session_factory, settings, datetime.now(timezone.utc))
            runs += 1
            if counts["promoted"] or counts["migrations"]:
                log.info("funnel.run", **counts)
            if runs % STATS_LOG_EVERY == 0:
                log.info("stream.stats", heartbeat=str(await pump_stream.heartbeat(redis)), **await pump_stream.stats(redis))
        except Exception as exc:  # noqa: BLE001
            log.error("funnel.failed", error=str(exc))
            await _record_system_event(session_factory, "funnel_failed", "error", {"error": str(exc)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=FUNNEL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    if not settings.SOLANA_RPC_URL:
        log.warning("engine-solana-discovery.disabled", reason="SOLANA_RPC_URL not set")
        return
    ws_urls = [u for u in (settings.SOLANA_WS_URL, settings.SOLANA_WS_BACKUP_URL) if u]
    if not ws_urls:
        log.warning("engine-solana-discovery.disabled", reason="SOLANA_WS_URL not set")
        return

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)

    async with httpx.AsyncClient() as http_client:
        rpc = RpcManager.create(
            client=http_client,
            primary_url=settings.SOLANA_RPC_URL,
            backup_url=settings.SOLANA_RPC_BACKUP_URL,
        )

        async def handle_message(message: dict) -> None:
            if message.get("method") != "logsNotification":
                return
            value = message.get("params", {}).get("result", {}).get("value", {})
            if value.get("err") is not None:
                return  # a failed transaction changed nothing on-chain
            # pump.fun emits its Anchor events as "Program data:" log lines,
            # so the notification alone carries every field we need — no
            # getTransaction round-trip per event.
            await pump_stream.ingest_logs(redis, value.get("logs", []), value.get("signature"), datetime.now(timezone.utc))

        ws_index = {"i": 0}

        def next_ws_url() -> str:
            url = ws_urls[ws_index["i"] % len(ws_urls)]
            ws_index["i"] += 1
            return url

        # Scoped to the pump.fun program only. The previous subscription to
        # the whole SPL Token program delivered every token transaction on
        # Solana and needed a getTransaction call per mint — far beyond a
        # free RPC plan and a 2 GB server.
        ws_client = SolanaWsClient(
            url_provider=next_ws_url,
            subscriptions=[
                {
                    "jsonrpc": "2.0",
                    "method": "logsSubscribe",
                    "params": [{"mentions": [PUMP_PROGRAM_ID]}, {"commitment": "confirmed"}],
                }
            ],
            on_message=handle_message,
        )

        async def held_mints() -> set[str]:
            async with session_factory() as session:
                rows = await session.execute(select(PaperPosition.asset_id).where(
                    PaperPosition.status.in_(("open", "pending_entry")),
                    PaperPosition.engine.in_(("solana_fresh", "solana_migration", "solana_momentum"))))
                return {m for m in rows.scalars() if m}

        # Independent cross-check of the on-chain stream (coverage, migrations)
        # and, with PUMPPORTAL_API_KEY, trade events for held mints.
        pumpportal = pumpportal_ws.PumpPortalFeed(redis, settings.PUMPPORTAL_API_KEY, held_mints)

        async def stream_stats() -> dict:
            return {**await pump_stream.stats(redis), "pumpportal_coverage": await pumpportal_ws.coverage(redis),
                    "pumpportal_heartbeat": await pumpportal_ws.heartbeat(redis)}

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await _record_system_event(session_factory, "service_started", "info", {"program": PUMP_PROGRAM_ID})
        log.info("engine-solana-discovery.started", program=PUMP_PROGRAM_ID)

        try:
            await asyncio.gather(
                heartbeat_loop(settings, "engine-solana-discovery", stop_event, stream_stats),
                ws_client.run(stop_event),
                pumpportal.run(stop_event),
                _health_check_loop(rpc, session_factory, stop_event),
                _funnel_loop(redis, session_factory, stop_event),
            )
        finally:
            await _record_system_event(session_factory, "service_stopped", "info")
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
