import asyncio
import json

import pytest
import websockets

from app.user_stream import UserDataStreamClient

pytestmark = pytest.mark.asyncio


class FakeRestClient:
    def __init__(self):
        self.listen_keys_created = 0
        self.keepalive_calls = 0

    async def start_user_data_stream(self) -> str:
        self.listen_keys_created += 1
        return f"listen-key-{self.listen_keys_created}"

    async def keepalive_user_data_stream(self) -> None:
        self.keepalive_calls += 1


async def test_connects_using_listen_key_from_rest_client_and_receives_messages():
    connection_paths = []

    async def handler(websocket):
        connection_paths.append(websocket.request.path)
        await websocket.send(json.dumps({"e": "ACCOUNT_UPDATE", "a": {"P": []}}))
        await asyncio.sleep(2)

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rest_client = FakeRestClient()
        received = []

        async def on_message(message):
            received.append(message)

        client = UserDataStreamClient(
            rest_client=rest_client,
            on_message=on_message,
            ws_base_override=f"ws://localhost:{port}",
        )

        stop_event = asyncio.Event()
        run_task = asyncio.create_task(client.run(stop_event))
        try:
            for _ in range(60):
                if received:
                    break
                await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(run_task, timeout=5)

    assert connection_paths == ["/listen-key-1"]
    assert rest_client.listen_keys_created == 1
    assert received == [{"e": "ACCOUNT_UPDATE", "a": {"P": []}}]


async def test_reconnect_after_forced_disconnect_gets_a_fresh_listen_key():
    connection_count = 0

    async def handler(websocket):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await websocket.close(code=1006)
            return
        await websocket.send(json.dumps({"e": "ACCOUNT_UPDATE", "a": {"P": []}}))
        await asyncio.sleep(2)

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rest_client = FakeRestClient()
        received = []

        async def on_message(message):
            received.append(message)

        client = UserDataStreamClient(
            rest_client=rest_client,
            on_message=on_message,
            ws_base_override=f"ws://localhost:{port}",
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

    assert connection_count == 2
    # A disconnect discards the cached listenKey -- reconnect must request a fresh one.
    assert rest_client.listen_keys_created == 2


async def test_keepalive_loop_calls_rest_client_periodically():
    rest_client = FakeRestClient()

    async def on_message(message):
        pass

    client = UserDataStreamClient(rest_client=rest_client, on_message=on_message, keepalive_interval_seconds=0.05)
    stop_event = asyncio.Event()
    task = asyncio.create_task(client._keepalive_loop(stop_event))

    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert rest_client.keepalive_calls >= 2


async def test_keepalive_failure_forces_fresh_listen_key_on_next_connect():
    class FailingKeepaliveClient(FakeRestClient):
        async def keepalive_user_data_stream(self) -> None:
            self.keepalive_calls += 1
            raise RuntimeError("keepalive failed")

    rest_client = FailingKeepaliveClient()

    async def on_message(message):
        pass

    client = UserDataStreamClient(rest_client=rest_client, on_message=on_message, keepalive_interval_seconds=0.05)
    client._listen_key = "stale-key"

    stop_event = asyncio.Event()
    task = asyncio.create_task(client._keepalive_loop(stop_event))
    await asyncio.sleep(0.15)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert client._listen_key is None


async def test_ws_base_defaults_to_testnet_when_no_override():
    client = UserDataStreamClient(rest_client=FakeRestClient(), on_message=lambda m: None, testnet=True)
    assert client._ws_base == "wss://stream.binancefuture.com/ws"


async def test_ws_base_defaults_to_production_when_no_override():
    client = UserDataStreamClient(rest_client=FakeRestClient(), on_message=lambda m: None, testnet=False)
    assert client._ws_base == "wss://fstream.binance.com/ws"
