"""Request ID and timing for every API request (audit 2026-10-07, 504s).

Each response carries X-Request-ID (the caller's, when it sent a safe one,
otherwise a new one) and X-Response-Time-Ms. A request that took longer than
SLOW_MS or ended in a 5xx is logged and kept in a short Redis list
(SLOW_KEY) that System Health shows, so a timeout names the endpoint, how
long it ran and how it ended instead of only "Request failed (504)".

Recorded: method, path, the names of the query parameters (never their
values), status, duration, request ID and time. Pure ASGI; the body is never
read or buffered.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

from yonixalpha_core.logging import get_logger

log = get_logger("api.request")

HEADER = b"x-request-id"
SLOW_MS = 2000
SLOW_KEY = "yx:api:slow_requests"
SLOW_KEEP = 200
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def request_id_of(scope) -> str | None:
    return (scope.get("state") or {}).get("request_id")


def _incoming_id(scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == HEADER:
            text = v.decode("latin-1")
            return text if _SAFE_ID.match(text) else None
    return None


def _param_names(query: bytes) -> list[str]:
    names = []
    for part in query.decode("latin-1").split("&"):
        name = part.split("=", 1)[0]
        if name and name not in names:
            names.append(name[:40])
    return names


class RequestTimingMiddleware:
    def __init__(self, app, redis_getter=None, clock=time.monotonic):
        self.app = app
        self._redis = redis_getter
        self._clock = clock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _incoming_id(scope) or uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid
        started = self._clock()
        status = {"code": 500}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                ms = int((self._clock() - started) * 1000)
                headers = [h for h in message.get("headers", []) if h[0] != HEADER]
                headers += [(HEADER, rid.encode()), (b"x-response-time-ms", str(ms).encode())]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            ms = int((self._clock() - started) * 1000)
            if ms >= SLOW_MS or status["code"] >= 500:
                await self._record(scope, rid, status["code"], ms)

    async def _record(self, scope, rid: str, code: int, ms: int) -> None:
        entry = {"request_id": rid, "method": scope.get("method"), "path": scope.get("path"),
                 "params": _param_names(scope.get("query_string") or b""), "status": code, "ms": ms,
                 "at": datetime.now(timezone.utc).isoformat()}
        log.warning("api.request.slow" if code < 500 else "api.request.error", **entry)
        redis = None
        try:
            redis = self._redis(scope) if self._redis else None
        except Exception:  # noqa: BLE001
            redis = None
        if redis is None:
            return
        try:
            await redis.lpush(SLOW_KEY, json.dumps(entry))
            await redis.ltrim(SLOW_KEY, 0, SLOW_KEEP - 1)
        except Exception:  # noqa: BLE001 - recording must never fail the request
            pass


async def recent(redis, limit: int = 50) -> list[dict]:
    out = []
    for raw in await redis.lrange(SLOW_KEY, 0, max(0, limit - 1)):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out
