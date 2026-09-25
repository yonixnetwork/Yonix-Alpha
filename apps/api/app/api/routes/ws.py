"""Realtime WebSocket: relays the Redis event bus (yonixalpha_core.events)
to authenticated dashboards.

Protocol: the client connects, then sends {"type": "auth", "token":
"<access token>"} within 5 s. Only after the token validates does the
server start relaying {type, data, source, at} messages. The Origin header
must be one of the configured CORS origins, which blocks cross-site
WebSocket hijacking from a malicious page. Pings every 20 s keep proxies
from idling the connection out.
"""

import asyncio
import json

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from yonixalpha_core.events import CHANNEL
from yonixalpha_core.logging import get_logger
from yonixalpha_core.security import decode_token

router = APIRouter()
log = get_logger("api.ws")

AUTH_TIMEOUT_SECONDS = 5
PING_SECONDS = 20
CLIENTS_KEY = "yx:ws:clients"


@router.websocket("/ws")
async def events_socket(ws: WebSocket) -> None:
    settings = ws.app.state.settings
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=AUTH_TIMEOUT_SECONDS)
        payload = decode_token(settings, str(first.get("token", ""))) if first.get("type") == "auth" else None
        if not payload or payload.get("type") != "access":
            raise PermissionError
    except (asyncio.TimeoutError, jwt.PyJWTError, PermissionError, ValueError, WebSocketDisconnect, AttributeError):
        await ws.close(code=4401)
        return

    redis = ws.app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    await redis.incr(CLIENTS_KEY)
    await ws.send_json({"type": "ws.ready", "data": {"user": payload.get("sub")}})

    async def relay() -> None:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                await ws.send_text(msg["data"] if isinstance(msg["data"], str) else json.dumps(msg["data"]))

    async def ping() -> None:
        while True:
            await asyncio.sleep(PING_SECONDS)
            await ws.send_json({"type": "ws.ping"})

    async def inbound() -> None:
        # Drain client messages; a close or error ends the session.
        while True:
            await ws.receive_text()

    tasks = [asyncio.create_task(relay()), asyncio.create_task(ping()), asyncio.create_task(inbound())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
        await redis.decr(CLIENTS_KEY)
