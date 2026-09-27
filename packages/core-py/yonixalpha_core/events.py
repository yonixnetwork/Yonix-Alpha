"""Realtime event bus, service heartbeats and notifications.

Events go over Redis pub/sub (channel `yx:events`) as JSON
{type, data, source, at}; the API's WebSocket relays them to dashboards.
Publishing is best-effort by design: a Redis hiccup must never break a
trading decision, so failures are logged and swallowed.

Heartbeats: every long-running service writes `yx:hb:<service>` with a TTL,
so a dashboard can tell CONNECTED from STALE from OFFLINE without trusting
the service's own claim. Notifications are stored in the DB (in-app feed)
and optionally sent to Telegram per kind.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import Notification, PlatformSetting
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import send_telegram_alert

log = get_logger("core.events")

CHANNEL = "yx:events"
COUNTS = "yx:events:counts"
HEARTBEAT_PREFIX = "yx:hb:"
HEARTBEAT_TTL_SECONDS = 180

EVENT_TYPES = {
    "balance.updated", "position.updated", "trade.created", "trade.updated", "trade.closed",
    "token.discovered", "token.updated", "migration.detected", "signal.created", "signal.updated",
    "risk.updated", "strategy.updated", "system.health.updated", "ml.prediction.updated", "ml.model.updated",
    "notification.created", "configuration.updated", "rpc.failover", "manual_trade.updated",
}

# Which notification kinds go to Telegram when no preference is stored.
DEFAULT_TELEGRAM_KINDS = {
    "approval_required", "entry", "stop_loss", "trailing_stop", "close", "connection_failure",
    "provider_failure", "ml_drift", "strategy_disabled",
}
NOTIFICATION_KINDS = [
    "qualified_token", "liquidity_confirmed", "signal", "approval_required", "entry", "tp1", "tp2", "tp3",
    "stop_loss", "trailing_stop", "close", "risk_rejection", "blacklist_rejection", "connection_failure",
    "ml_drift", "strategy_disabled", "provider_failure",
]
PREFS_KEY = "notification_prefs"


def _json(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


async def publish(redis: Redis | None, event_type: str, data: dict | None = None, source: str = "") -> None:
    if redis is None:
        return
    if event_type not in EVENT_TYPES:
        log.warning("events.unknown_type", type=event_type)
        return
    msg = {"type": event_type, "data": _json(data or {}), "source": source, "at": datetime.now(timezone.utc).isoformat()}
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.publish(CHANNEL, json.dumps(msg))
        pipe.hincrby(COUNTS, event_type, 1)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 - realtime is best-effort
        log.warning("events.publish_failed", type=event_type, error=str(exc))


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except (OSError, ValueError, IndexError):
        return None


async def heartbeat(redis: Redis | None, service: str, status: str = "ok", detail: dict | None = None) -> None:
    if redis is None:
        return
    body = {"at": datetime.now(timezone.utc).isoformat(), "status": status, "rss_mb": _rss_mb(), "detail": _json(detail or {})}
    try:
        await redis.set(f"{HEARTBEAT_PREFIX}{service}", json.dumps(body), ex=HEARTBEAT_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("events.heartbeat_failed", service=service, error=str(exc))


async def read_heartbeats(redis: Redis, services: list[str]) -> dict[str, dict | None]:
    raw = await redis.mget([f"{HEARTBEAT_PREFIX}{s}" for s in services])
    out: dict[str, dict | None] = {}
    for s, r in zip(services, raw):
        try:
            out[s] = json.loads(r) if r else None
        except ValueError:
            out[s] = None
    return out


async def telegram_enabled_for(session: AsyncSession, kind: str) -> bool:
    row = await session.get(PlatformSetting, PREFS_KEY)
    prefs = (row.value or {}) if row else {}
    if kind in prefs:
        return bool((prefs[kind] or {}).get("telegram"))
    return kind in DEFAULT_TELEGRAM_KINDS


async def notify(
    session: AsyncSession,
    redis: Redis | None,
    settings: Any,
    kind: str,
    title: str,
    body: str | None = None,
    severity: str = "info",
    data: dict | None = None,
) -> Notification:
    """Stores an in-app notification, publishes it, and sends it to Telegram
    when that kind is enabled. Caller commits the session."""
    n = Notification(kind=kind, severity=severity, title=title[:200], body=(body or "")[:1000] or None, data=_json(data or {}))
    session.add(n)
    await session.flush()
    await publish(redis, "notification.created",
                  {"id": str(n.id), "kind": kind, "severity": severity, "title": n.title, "body": n.body}, "notify")
    if settings is not None and await telegram_enabled_for(session, kind):
        await send_telegram_alert(settings, f"[{severity.upper()}] {title}" + (f"\n{body}" if body else ""))
    return n


HEARTBEAT_INTERVAL_SECONDS = 30


async def idle_while_disabled(settings: Any, service: str, reason: str, stop_event=None) -> None:
    """For a service whose required configuration is missing: stay up and
    report heartbeat status "disabled" (with the reason) until SIGTERM/SIGINT,
    instead of exiting. Under `restart: unless-stopped` an exit becomes an
    endless restart loop, and the health page could only say "no heartbeat";
    this way it shows NOT CONFIGURED with the missing setting."""
    import asyncio
    import signal

    from yonixalpha_core.db.redis import make_redis

    stop = stop_event or asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    redis = make_redis(settings)
    try:
        while not stop.is_set():
            await heartbeat(redis, service, status="disabled", detail={"reason": reason})
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await redis.aclose()


async def heartbeat_loop(settings: Any, service: str, stop_event, detail_fn=None) -> None:
    """Writes this service's heartbeat every 30 s until stop_event is set.
    Owns its own Redis client so services without Redis elsewhere can use
    it; `detail_fn` (sync or async) adds service-specific fields."""
    import asyncio

    from yonixalpha_core.db.redis import make_redis

    redis = make_redis(settings)
    try:
        while not stop_event.is_set():
            detail = None
            if detail_fn is not None:
                try:
                    detail = detail_fn()
                    if asyncio.iscoroutine(detail):
                        detail = await detail
                except Exception as exc:  # noqa: BLE001
                    detail = {"detail_error": str(exc)}
            await heartbeat(redis, service, detail=detail)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await redis.aclose()
