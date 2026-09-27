import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error

log = get_logger("engine-binance-futures.user_stream")

PRODUCTION_WS_BASE = "wss://fstream.binance.com/ws"
TESTNET_WS_BASE = "wss://stream.binancefuture.com/ws"

# Binance listenKeys are documented as valid for 60 minutes without a
# keepalive; pinging well before that margin. Not live-verified in this
# environment — see ARCHITECTURE_AUDIT.md.
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 30 * 60

MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class UserDataStreamClient:
    """Manages the full listenKey lifecycle (create, periodic keepalive,
    transparent renewal on disconnect) plus the WS connection itself.
    Never assumes the connection or the listenKey stay valid forever (spec
    section 22): a disconnect discards the cached listenKey and gets a
    fresh one on reconnect, since the old one may have expired or been
    invalidated server-side; a keepalive failure does the same.
    """

    rest_client: object  # BinanceFuturesClient — untyped here to avoid a hard import cycle risk; duck-typed in tests
    on_message: MessageHandler
    testnet: bool = True
    max_backoff_seconds: float = 30.0
    initial_backoff_seconds: float = 1.0
    keepalive_interval_seconds: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS
    ws_base_override: str | None = None  # tests point this at a local server instead of the real Binance host
    _listen_key: str | None = None

    @property
    def _ws_base(self) -> str:
        if self.ws_base_override:
            return self.ws_base_override
        return TESTNET_WS_BASE if self.testnet else PRODUCTION_WS_BASE

    async def _ensure_listen_key(self) -> str:
        if self._listen_key is None:
            self._listen_key = await self.rest_client.start_user_data_stream()
            log.info("user_stream.listen_key_created")
        return self._listen_key

    async def _keepalive_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.keepalive_interval_seconds)
            except asyncio.TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                await self.rest_client.keepalive_user_data_stream()
                log.info("user_stream.keepalive_ok")
            except Exception as exc:  # noqa: BLE001
                log.warning("user_stream.keepalive_failed", error=str(exc))
                self._listen_key = None  # force a fresh key next time the connect loop checks

    async def run(self, stop_event: asyncio.Event) -> None:
        keepalive_task = asyncio.create_task(self._keepalive_loop(stop_event))
        backoff = self.initial_backoff_seconds
        try:
            while not stop_event.is_set():
                try:
                    listen_key = await self._ensure_listen_key()
                except Exception as exc:  # noqa: BLE001
                    log.error("user_stream.listen_key_creation_failed", error=str(exc))
                    await alert_error("engine-binance-futures", "user_stream.listen_key_creation_failed", {"error": str(exc)})
                    await self._sleep_backoff(stop_event, backoff)
                    backoff = min(backoff * 2, self.max_backoff_seconds)
                    continue

                url = f"{self._ws_base}/{listen_key}"
                try:
                    async with websockets.connect(url) as ws:
                        log.info("user_stream.connected")
                        backoff = self.initial_backoff_seconds
                        while not stop_event.is_set():
                            raw = await ws.recv()
                            try:
                                message = json.loads(raw)
                            except json.JSONDecodeError:
                                log.warning("user_stream.invalid_json", raw_preview=str(raw)[:200])
                                continue
                            await self.on_message(message)
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd else None
                    log.warning("user_stream.disconnected", code=code)
                    self._listen_key = None
                except OSError as exc:
                    log.warning("user_stream.connect_failed", error=str(exc))
                    self._listen_key = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.error("user_stream.unexpected_error", error=str(exc))
                    await alert_error("engine-binance-futures", "user_stream.unexpected_error",
                                      {"error": f"{type(exc).__name__}: {exc}"})

                if stop_event.is_set():
                    break
                await self._sleep_backoff(stop_event, backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    async def _sleep_backoff(stop_event: asyncio.Event, backoff: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
