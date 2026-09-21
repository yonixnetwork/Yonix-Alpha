import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from yonixalpha_core.logging import get_logger

log = get_logger("data-binance.ws")

MessageHandler = Callable[[dict], Awaitable[None]]

PRODUCTION_WS_BASE = "wss://fstream.binance.com/stream"
TESTNET_WS_BASE = "wss://stream.binancefuture.com/stream"


def combined_stream_url(streams: list[str], testnet: bool = False) -> str:
    base = TESTNET_WS_BASE if testnet else PRODUCTION_WS_BASE
    return f"{base}?streams={'/'.join(streams)}"


@dataclass
class BinanceWsClient:
    """Reconnecting client for Binance's combined market-data stream.
    Unlike Solana's WS client, Binance streams are selected via the URL
    itself (no post-connect subscribe message needed for the combined
    endpoint), so reconnection just means "open the same URL again" — still
    never assumes the connection survives forever (spec section 22).
    """

    url: str
    on_message: MessageHandler
    max_backoff_seconds: float = 30.0
    initial_backoff_seconds: float = 1.0

    async def run(self, stop_event: asyncio.Event) -> None:
        backoff = self.initial_backoff_seconds
        while not stop_event.is_set():
            try:
                async with websockets.connect(self.url) as ws:
                    log.info("ws.connected", url=self.url)
                    backoff = self.initial_backoff_seconds

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
                log.warning("ws.disconnected", url=self.url, code=code, reason=reason)
            except OSError as exc:
                log.warning("ws.connect_failed", url=self.url, error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the reconnect loop alive on any unexpected error
                log.error("ws.unexpected_error", url=self.url, error=str(exc))

            if stop_event.is_set():
                break

            log.info("ws.reconnecting", backoff_seconds=backoff)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff_seconds)
