import asyncio
import json

import pytest
import websockets

from app.ws.client import SolanaWsClient

pytestmark = pytest.mark.asyncio


async def test_reconnects_and_resubscribes_after_forced_disconnect():
    """Real local WebSocket server (not mocked) with two behaviors across
    two accepted connections: the first connection immediately closes right
    after receiving a subscribe message (simulating a mid-session drop),
    the second stays open and pushes one notification. Asserts the client
    (a) resubscribes on the first connection, (b) reconnects on its own,
    (c) resubscribes again on the second connection, (d) delivers the
    pushed message to the handler.
    """
    received_subscribes: list[dict] = []
    connection_count = 0
    server_ready = asyncio.Event()

    async def handler(websocket):
        nonlocal connection_count
        connection_count += 1
        this_connection = connection_count
        raw = await websocket.recv()
        received_subscribes.append(json.loads(raw))

        if this_connection == 1:
            await websocket.close(code=1006)
            return

        await websocket.send(json.dumps({"method": "slotNotification", "params": {"result": {"slot": 42}}}))
        await asyncio.sleep(2)  # keep the connection open long enough for the client to process

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        server_ready.set()

        received_messages: list[dict] = []

        async def on_message(message: dict) -> None:
            received_messages.append(message)

        client = SolanaWsClient(
            url_provider=lambda: f"ws://localhost:{port}",
            subscriptions=[{"jsonrpc": "2.0", "method": "slotSubscribe", "params": []}],
            on_message=on_message,
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.2,
        )

        stop_event = asyncio.Event()
        run_task = asyncio.create_task(client.run(stop_event))

        try:
            for _ in range(100):  # up to ~5s, polling rather than a fixed sleep
                if len(received_messages) >= 1 and connection_count >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(run_task, timeout=5)

    assert connection_count == 2, "client should have reconnected exactly once after the forced drop"
    assert len(received_subscribes) == 2, "client should resubscribe on every connection, including after reconnect"
    for sub in received_subscribes:
        assert sub["method"] == "slotSubscribe"
    assert received_messages == [{"method": "slotNotification", "params": {"result": {"slot": 42}}}]
