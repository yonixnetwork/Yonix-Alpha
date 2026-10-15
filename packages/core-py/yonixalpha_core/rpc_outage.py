"""One clear Telegram alert when a service has had no working Solana RPC for
a while (2026-10-15, after the Oct 13-14 outage: about 22 hours with every
endpoint failing and only throttled per-check errors to show for it).

Each Solana service feeds its 30-second getHealth result into an
RpcOutageAlarm:

- the first alert is sent once failures have lasted THRESHOLD_SECONDS (5 min)
  without a single success, then again every REPEAT_SECONDS (1 hour) while
  the outage lasts;
- the four services share one alert through Redis: one message per hour,
  naming every service that is failing, not one message per service;
- when the last failing service works again, one "working again" message
  says how long the outage lasted;
- every alert and recovery is also stored as a SystemEvent (rpc_outage,
  critical; rpc_outage_recovered, info) by each affected service.

If Redis cannot be reached, each service sends its own message: a duplicate
is better than silence. Nothing here ever raises into the health loop, and
every URL in a message is reduced to scheme://host (keys live in RPC URLs).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import scrub

log = get_logger("core.rpc_outage")

THRESHOLD_SECONDS = 300
REPEAT_SECONDS = 3600
FRESH_SECONDS = 180  # a failing-service entry older than this is a stopped service, not an outage

KEY_FAILING = "yonixalpha:rpc_outage:failing"  # hash service -> {"since", "last"}
KEY_ALERT = "yonixalpha:rpc_outage:alert"  # + ":<start>:<hour slot>": whoever claims it sends that message
KEY_OPEN = "yonixalpha:rpc_outage:open"  # an alert went out; the last to recover says so
OPEN_TTL_SECONDS = 7 * 24 * 3600

FIX_HINT = ("Fix: Dashboard > RPC providers. At least one Solana provider must be Enabled and its Test "
            "must show CONNECTED. If a provider shows rate limited (429) or a 403, check its plan or credits.")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _minutes(seconds: float) -> int:
    return max(1, int(seconds // 60))


def describe_endpoints(endpoints: list[dict] | None) -> str:
    """One line per endpoint from RpcManager.health_snapshot() (its URLs are
    already scheme://host): name, where it is configured, and its state."""
    lines = []
    for e in endpoints or []:
        state = []
        if e.get("rate_limited"):
            state.append("rate limited (429)")
        if e.get("auth_failed"):
            state.append("key refused (401/403)")
        if e.get("disabled"):
            state.append("paused after failures")
        if e.get("consecutive_failures"):
            state.append(f"{e['consecutive_failures']} failures in a row")
        name = e.get("name") or e.get("label") or "?"
        lines.append(f"- {name} [{e.get('source') or '?'}] {e.get('url') or ''} - {', '.join(state) or 'no recent error'}")
    return "\n".join(lines) or "- no endpoint configured"


class RpcOutageAlarm:
    def __init__(self, service: str, session_factory=None, *, redis: Any = None,
                 send: Callable[[str], Awaitable[bool]] | None = None,
                 clock: Callable[[], float] = time.time,
                 threshold_seconds: int = THRESHOLD_SECONDS, repeat_seconds: int = REPEAT_SECONDS) -> None:
        self.service = service
        self.session_factory = session_factory
        self._redis = redis
        self._own_redis = False
        self._send = send
        self.clock = clock
        self.threshold = threshold_seconds
        self.repeat = repeat_seconds
        self.since: float | None = None  # first failure of the current outage
        self.next_alert_at: float | None = None
        self.next_record_at: float | None = None
        self._closed_leftover = False

    # -- wiring ------------------------------------------------------------
    def _redis_client(self):
        if self._redis is None:
            try:
                from yonixalpha_core.config import get_settings
                from yonixalpha_core.db.redis import make_redis

                self._redis = make_redis(get_settings())
                self._own_redis = True
            except Exception as exc:  # noqa: BLE001
                log.warning("rpc_outage.redis_unavailable", error=type(exc).__name__)
                return None
        return self._redis

    async def _telegram(self, text: str) -> bool:
        text = scrub(text)[:3500]
        try:
            if self._send is not None:
                return bool(await self._send(text))
            from yonixalpha_core.config import get_settings
            from yonixalpha_core.notify import send_telegram_alert

            return await send_telegram_alert(get_settings(), text)
        except Exception as exc:  # noqa: BLE001 - alerting must never take a service down
            log.warning("rpc_outage.telegram_failed", error=type(exc).__name__)
            return False

    async def _record(self, event_type: str, severity: str, detail: dict) -> None:
        if self.session_factory is None:
            return
        try:
            from yonixalpha_core.db.models import SystemEvent

            detail = json.loads(scrub(json.dumps(detail, default=str)))
            async with self.session_factory() as session:
                session.add(SystemEvent(service=self.service, event_type=event_type, severity=severity, detail=detail))
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("rpc_outage.record_failed", event=event_type, error=type(exc).__name__)

    async def aclose(self) -> None:
        if self._own_redis and self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    # -- shared state ------------------------------------------------------
    async def _failing_services(self, r, now: float) -> dict[str, dict]:
        """Services whose last failure is recent, with when their outage began."""
        out = {}
        for svc, raw in (await r.hgetall(KEY_FAILING)).items():
            try:
                v = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if now - float(v.get("last", 0)) <= FRESH_SECONDS:
                out[svc] = v
        return out

    # -- inputs from the health loop --------------------------------------
    async def failed(self, error: str, endpoints: list[dict] | None = None) -> bool:
        """A getHealth call failed. Returns whether this call sent an alert."""
        try:
            return await self._failed(error, endpoints)
        except Exception as exc:  # noqa: BLE001
            log.warning("rpc_outage.failed_handler_error", error=type(exc).__name__)
            return False

    async def ok(self) -> bool:
        """A getHealth call succeeded. Returns whether this call sent the
        "working again" message."""
        try:
            return await self._ok()
        except Exception as exc:  # noqa: BLE001
            log.warning("rpc_outage.ok_handler_error", error=type(exc).__name__)
            return False

    async def _failed(self, error: str, endpoints: list[dict] | None) -> bool:
        now = self.clock()
        if self.since is None:
            self.since = now
        r = self._redis_client()
        if r is not None:
            try:
                await r.hset(KEY_FAILING, self.service, json.dumps({"since": self.since, "last": now}))
                await r.expire(KEY_FAILING, OPEN_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                log.warning("rpc_outage.redis_error", error=type(exc).__name__)
                r = None
        down_for = now - self.since
        if down_for < self.threshold:
            return False

        if self.next_record_at is None or now >= self.next_record_at:
            self.next_record_at = now + self.repeat
            await self._record("rpc_outage", "critical", {
                "since": _utc(self.since), "minutes": _minutes(down_for), "error": error[:500],
                "endpoints": [{k: e.get(k) for k in ("name", "source", "url", "rate_limited", "auth_failed",
                                                     "disabled", "consecutive_failures", "last_error")}
                              for e in endpoints or []]})

        if self.next_alert_at is not None and now < self.next_alert_at:
            return False
        services = {self.service: {"since": self.since}}
        started = self.since
        claimed = True
        if r is not None:
            try:
                services = await self._failing_services(r, now) or services
                raw = await r.get(KEY_OPEN)
                started = float(json.loads(raw)["since"]) if raw else min(float(v.get("since", now))
                                                                          for v in services.values())
            except Exception as exc:  # noqa: BLE001
                log.warning("rpc_outage.redis_error", error=type(exc).__name__)
                r = None
        # The outage is cut into hour slots from its start: slot 0 is the first
        # alert, each later slot one reminder. Whoever claims a slot sends it.
        slot = max(0, int((now - started - self.threshold) // self.repeat))
        self.next_alert_at = started + self.threshold + (slot + 1) * self.repeat
        if r is not None:
            try:
                claimed = bool(await r.set(f"{KEY_ALERT}:{int(started)}:{slot}", self.service, nx=True,
                                           ex=2 * self.repeat))
                if claimed:
                    await r.set(KEY_OPEN, json.dumps({"since": started}), nx=True, ex=OPEN_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                log.warning("rpc_outage.redis_error", error=type(exc).__name__)
        if not claimed:
            return False  # another service sends this hour's message
        first = slot == 0
        text = "\n".join([
            f"[CRITICAL] YONIXALPHA: no working Solana RPC for {_minutes(now - started)} min"
            f" (since {_utc(started)})" + ("" if first else ", reminder: still down"),
            f"Services affected: {', '.join(sorted(services))}",
            f"Endpoints seen by {self.service}:",
            describe_endpoints(endpoints),
            f"Last error: {error[:300]}",
            "While this lasts no new tokens are found or checked, and open positions may not be priced or exited on time.",
            FIX_HINT,
            f"Next reminder in {self.repeat // 60} min while the outage lasts.",
        ])
        sent = await self._telegram(text)
        log.error("rpc_outage.alert", minutes=_minutes(now - started), services=sorted(services), sent=sent)
        return sent

    async def _ok(self) -> bool:
        if self.since is None:
            if self._closed_leftover:
                return False
            # First success after a (re)start: an outage that was open when the
            # services restarted (a deploy mid-outage) is closed here, so its
            # "working again" message is not lost and the next outage starts
            # from zero. Only once per process, so a healthy loop costs nothing.
            self._closed_leftover = True
            self.since = self.clock()
        now = self.clock()
        down_for = now - self.since
        crossed = down_for >= self.threshold
        self.since = self.next_alert_at = self.next_record_at = None
        if crossed:
            await self._record("rpc_outage_recovered", "info", {"minutes": _minutes(down_for)})
        r = self._redis_client()
        if r is None:
            if not crossed:
                return False
            return await self._telegram(f"[RECOVERED] YONIXALPHA: Solana RPC working again for {self.service}"
                                        f" after {_minutes(down_for)} min")
        try:
            await r.hdel(KEY_FAILING, self.service)
            if await self._failing_services(r, now):
                return False  # others still down: the last one to recover says so
            raw = await r.getdel(KEY_OPEN)
            if raw is None:
                return False  # no alert went out for this outage
            started = float(json.loads(raw).get("since", now - down_for))
        except Exception as exc:  # noqa: BLE001
            log.warning("rpc_outage.redis_error", error=type(exc).__name__)
            if not crossed:
                return False
            started = now - down_for
        log.info("rpc_outage.recovered", minutes=_minutes(now - started))
        return await self._telegram(f"[RECOVERED] YONIXALPHA: Solana RPC working again on all services"
                                    f" (outage lasted {_minutes(now - started)} min, since {_utc(started)})")
