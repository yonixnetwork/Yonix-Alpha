import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from yonixalpha_core.logging import get_logger
from yonixalpha_core.redact import redact_text, redact_url
from yonixalpha_core.notify import alert_error

log = get_logger("data-solana.ws")

MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class SolanaWsClient:
    """Reconnecting Solana WebSocket subscription client. Never assumes a
    connection stays alive forever (spec section 22): on any disconnect it
    reconnects with exponential backoff and resends every subscription that
    was active, so a caller only has to declare subscriptions once.

    `url_provider` is a callable rather than a fixed string so it can be
    backed by the same primary/backup health tracking as RpcManager if the
    operator configures a backup WS URL — this client only needs "give me
    the URL to try right now", not the failover logic itself.
    """

    url_provider: Callable[[], str]
    subscriptions: list[dict]  # each a full logsSubscribe/slotSubscribe/etc request dict (minus "id")
    on_message: MessageHandler
    max_backoff_seconds: float = 30.0
    initial_backoff_seconds: float = 1.0
    _next_id: int = field(default=0, init=False)
    _ws: Any = field(default=None, init=False)

    def request_reconnect(self) -> bool:
        """Closes the current connection; run() reconnects at once to the
        next URL of url_provider (stream_guard: a silent or lossy stream).
        Returns False when nothing is connected."""
        ws = self._ws
        if ws is None:
            return False
        asyncio.get_running_loop().create_task(ws.close())
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = self.initial_backoff_seconds
        while not stop_event.is_set():
            url = self.url_provider()
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    log.info("ws.connected", url=redact_url(url))
                    await self._resubscribe(ws)
                    backoff = self.initial_backoff_seconds  # reset after a clean connect

                    while not stop_event.is_set():
                        raw = await ws.recv()
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("ws.message.invalid_json", raw_preview=str(raw)[:200])
                            continue
                        await self.on_message(message)
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                reason = exc.rcvd.reason if exc.rcvd else None
                log.warning("ws.disconnected", url=redact_url(url), code=code, reason=reason)
            except OSError as exc:
                log.warning("ws.connect_failed", url=redact_url(url), error=redact_text(str(exc), [url]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the reconnect loop alive on any unexpected error
                log.error("ws.unexpected_error", url=redact_url(url), error=redact_text(str(exc), [url]))
                await alert_error("solana-ws", "ws.unexpected_error",
                                  {"url": redact_url(url), "error": redact_text(f"{type(exc).__name__}: {exc}", [url])})

            self._ws = None
            if stop_event.is_set():
                break

            log.info("ws.reconnecting", backoff_seconds=backoff)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff_seconds)

    async def _resubscribe(self, ws) -> None:
        for sub in self.subscriptions:
            self._next_id += 1
            await ws.send(json.dumps({**sub, "id": self._next_id}))
            log.info("ws.subscribed", method=sub.get("method"), request_id=self._next_id)
