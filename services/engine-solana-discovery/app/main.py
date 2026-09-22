import asyncio
import signal
from datetime import datetime, timezone

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.token_program import TOKEN_PROGRAM_ID, extract_mint_creations, logs_mention_mint_init
from yonixalpha_core.solana.ws import SolanaWsClient

from app.candidates import record_discovered_token

log = get_logger("engine-solana-discovery.main")

HEALTH_CHECK_INTERVAL_SECONDS = 30


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
                return  # failed transactions can't have created anything worth tracking
            logs = value.get("logs", [])
            if not logs_mention_mint_init(logs):
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

            for mint_info in extract_mint_creations(tx_result or {}):
                occurred_at = (
                    datetime.fromtimestamp(mint_info["block_time"], tz=timezone.utc)
                    if mint_info.get("block_time")
                    else datetime.now(timezone.utc)
                )
                async with session_factory() as session:
                    await record_discovered_token(session, mint_info, occurred_at)

        ws_index = {"i": 0}

        def next_ws_url() -> str:
            url = ws_urls[ws_index["i"] % len(ws_urls)]
            ws_index["i"] += 1
            return url

        # NOTE: logsSubscribe's `mentions` filter against the Token Program
        # (one of the highest-traffic programs on Solana) is known across
        # the ecosystem to be rejected or rate-limited by some public RPC
        # providers, since it forces the node to scan every transaction's
        # logs. Not verified against any specific current provider's policy
        # in this environment — if this connects but never delivers
        # notifications, that is the likely cause; production use probably
        # needs a dedicated indexer/webhook provider (e.g. Helius) instead
        # of raw public-RPC logsSubscribe.
        ws_client = SolanaWsClient(
            url_provider=next_ws_url,
            subscriptions=[
                {
                    "jsonrpc": "2.0",
                    "method": "logsSubscribe",
                    "params": [{"mentions": [TOKEN_PROGRAM_ID]}, {"commitment": "confirmed"}],
                }
            ],
            on_message=handle_message,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        await _record_system_event(session_factory, "service_started", "info")
        log.info("engine-solana-discovery.started", token_program=TOKEN_PROGRAM_ID)

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
