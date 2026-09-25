"""WebSocket relay: auth by first message, origin check, event delivery.
Uses Starlette's synchronous TestClient (its own event loop), so it runs the
real lifespan instead of the async `app` fixture."""

import json
import time

import pytest
import redis as sync_redis
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import CHANNEL
from yonixalpha_core.security import create_token

from app.main import create_app


@pytest.fixture
def client():
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def token(kind="access"):
    return create_token(get_settings(), "admin", kind)[0]


def test_rejects_connection_without_valid_auth_message(client):
    with client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": "garbage"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_rejects_refresh_token(client):
    with client.websocket_connect("/api/ws") as ws:
        ws.send_json({"type": "auth", "token": token("refresh")})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_rejects_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4403


def test_relays_bus_events_after_auth(client):
    r = sync_redis.from_url(get_settings().redis_url, decode_responses=True)
    with client.websocket_connect("/api/ws", headers={"origin": "http://localhost:3000"}) as ws:
        ws.send_json({"type": "auth", "token": token()})
        ready = ws.receive_json()
        assert ready["type"] == "ws.ready"
        msg = {"type": "trade.created", "data": {"id": "p1"}, "source": "test", "at": "now"}
        for _ in range(20):  # the relay subscribes right after ws.ready
            if r.publish(CHANNEL, json.dumps(msg)):
                break
            time.sleep(0.05)
        got = ws.receive_json()
        assert got == msg
