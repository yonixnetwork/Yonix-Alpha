import asyncio
import json

import websockets

from app.ws.client import BinanceWsClient, combined_stream_url


def test_combined_stream_url_format():
    url = combined_stream_url(["btcusdt@kline_1m", "btcusdt@aggTrade"])
    assert url == "wss://fstream.binance.com/stream?streams=btcusdt@kline_1m/btcusdt@aggTrade"


def test_combined_stream_url_testnet():
    url = combined_stream_url(["btcusdt@kline_1m"], testnet=True)
    assert url.startswith("wss://stream.binancefuture.com/stream?streams=")


async def test_reconnects_after_forced_disconnect_and_delivers_messages():
    connection_count = 0

    async def handler(websocket):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await websocket.close(code=1006)
            return
        await websocket.send(json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT"}}))
        await asyncio.sleep(2)

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]

        received: list[dict] = []

        async def on_message(message: dict) -> None:
            received.append(message)

        client = BinanceWsClient(
            url=f"ws://localhost:{port}",
            on_message=on_message,
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.2,
        )

        stop_event = asyncio.Event()
        run_task = asyncio.create_task(client.run(stop_event))

        try:
            for _ in range(100):
                if received and connection_count >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(run_task, timeout=5)

    assert connection_count == 2, "client should reconnect exactly once after the forced drop"
    assert received == [{"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT"}}]
