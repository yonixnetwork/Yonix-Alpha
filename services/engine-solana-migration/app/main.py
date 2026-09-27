import asyncio
import signal
from datetime import datetime, timezone

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop, idle_while_disabled
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.ws import SolanaWsClient

from app.candidates import record_migration_detected
from app.detect import configured_program_ids, extract_pool_initialization, has_parser

log = get_logger("engine-solana-migration.main")

HEALTH_CHECK_INTERVAL_SECONDS = 30


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service="engine-solana-migration", event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(),
            f"⚠️ [engine-solana-migration] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else ""),
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


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    program_ids = configured_program_ids()
    if not program_ids:
        log.warning(
            "engine-solana-migration.detection_inactive",
            reason="MIGRATION_AMM_PROGRAM_IDS not set — no AMM program IDs configured. "
            "See services/engine-solana-migration/README.md for why this ships empty by default.",
        )
    else:
        without_parser = [pid for pid in program_ids if not has_parser(pid)]
        if without_parser:
            log.warning("engine-solana-migration.no_parser_for_configured_programs", program_ids=without_parser)

    if not settings.SOLANA_RPC_URL:
        log.warning("engine-solana-migration.disabled", reason="SOLANA_RPC_URL not set")
        await idle_while_disabled(settings, "engine-solana-migration", "SOLANA_RPC_URL not set")
        return

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    async with httpx.AsyncClient() as http_client:
        rpc = RpcManager.create(
            client=http_client,
            primary_url=settings.SOLANA_RPC_URL,
            backup_url=settings.SOLANA_RPC_BACKUP_URL,
            extra_backup_urls=[settings.SOLANA_RPC_BACKUP_URL_2, settings.SOLANA_RPC_BACKUP_URL_3],
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        tasks = [_health_check_loop(rpc, session_factory, stop_event)]

        ws_urls = [u for u in (settings.SOLANA_WS_URL, settings.SOLANA_WS_BACKUP_URL) if u]
        watchable_program_ids = [pid for pid in program_ids if has_parser(pid)]

        if watchable_program_ids and ws_urls:

            async def handle_message(message: dict) -> None:
                if message.get("method") != "logsNotification":
                    return
                value = message.get("params", {}).get("result", {}).get("value", {})
                if value.get("err") is not None:
                    return
                signature = value.get("signature")
                if not signature:
                    return

                try:
                    tx_result = await rpc.call(
                        "getTransaction",
                        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("main.get_transaction_failed", signature=signature, error=str(exc))
                    return

                for program_id in watchable_program_ids:
                    migration_info = extract_pool_initialization(program_id, tx_result or {})
                    if migration_info is None:
                        continue
                    mint_address = migration_info.get("token_mint")
                    if not mint_address:
                        continue
                    occurred_at = (
                        datetime.fromtimestamp(migration_info["block_time"], tz=timezone.utc)
                        if migration_info.get("block_time")
                        else datetime.now(timezone.utc)
                    )
                    async with session_factory() as session:
                        await record_migration_detected(session, mint_address, migration_info, occurred_at)

            ws_index = {"i": 0}

            def next_ws_url() -> str:
                url = ws_urls[ws_index["i"] % len(ws_urls)]
                ws_index["i"] += 1
                return url

            ws_client = SolanaWsClient(
                url_provider=next_ws_url,
                subscriptions=[
                    {
                        "jsonrpc": "2.0",
                        "method": "logsSubscribe",
                        "params": [{"mentions": [pid]}, {"commitment": "confirmed"}],
                    }
                    for pid in watchable_program_ids
                ],
                on_message=handle_message,
            )
            tasks.append(ws_client.run(stop_event))
        else:
            log.warning("engine-solana-migration.ws_subscription_skipped", reason="no watchable programs configured")

        await _record_system_event(session_factory, "service_started", "info", {"watchable_program_ids": watchable_program_ids})
        log.info("engine-solana-migration.started", configured_programs=program_ids)

        try:
            await asyncio.gather(*tasks, heartbeat_loop(settings, "engine-solana-migration", stop_event))
        finally:
            await _record_system_event(session_factory, "service_stopped", "info")
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
