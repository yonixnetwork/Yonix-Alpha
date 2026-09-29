"""Runtime configuration revision, change events and per-service acknowledgement.

Dashboard-editable settings live in the database and every service reads
them from there on each evaluation cycle. What was missing is proof that a
saved change reached the running services, and a way for services that keep
configuration in memory (RPC endpoint lists) to reload it without a restart.

- Every accepted settings write bumps one database revision
  (platform_settings "config_revision") and publishes "configuration.updated"
  on the existing event bus (yx:events).
- Each service runs a RuntimeConfigWatcher: it wakes on that event (or at
  least every POLL_SECONDS), runs its reloaders when the revision changed,
  and writes an acknowledgement (yx:config:ack:<service>) with the revision
  it now runs, what it reloaded and the effective settings it read.
- The dashboard compares the database revision with every acknowledgement:
  SYNCED, OUT_OF_SYNC or NOT_REPORTING. A save is only shown as applied once
  the services acknowledge its revision.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events
from yonixalpha_core.db.models import PlatformSetting
from yonixalpha_core.logging import get_logger

log = get_logger("core.runtime_config")

REVISION_KEY = "config_revision"
ACK_PREFIX = "yx:config:ack:"
POLL_SECONDS = 10
ACK_TTL_SECONDS = 90  # an ack older than this reads NOT_REPORTING
EVENT = "configuration.updated"

# Services that read dashboard settings and acknowledge revisions, with what
# they apply. The API itself is the writer, so it isn't listed.
SERVICES: dict[str, str] = {
    "decision-engine": "strategy modes, risk settings, filters/blacklist/rules, live settings, manual buys, RPC endpoints",
    "engine-solana-discovery": "fresh-token observation settings, RPC endpoints",
    "engine-solana-momentum": "RPC endpoints",
    "engine-solana-migration": "RPC endpoints",
    "paper-trading": "position management, paper/live execution settings, RPC endpoints",
    "data-solana": "RPC endpoints",
    "data-evm": "EVM trading settings, trading controls and launchpad modes (read every pass)",
    "execution-futures": "futures live settings and strategy modes",
}
# Legacy containers (compose profile "legacy"), not started by the default
# deployment: Momentum and Migration run inside discovery + decision-engine.
OPTIONAL = {"engine-solana-momentum", "engine-solana-migration"}


async def current(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(PlatformSetting, REVISION_KEY)
    return dict(row.value) if row else {"revision": 0}


async def bump(session: AsyncSession, change: dict[str, Any], actor: str | None, source: str = "dashboard") -> dict[str, Any]:
    """Increments the revision inside the caller's transaction (row lock, so
    concurrent saves get distinct revisions). Announce after commit."""
    row = (await session.execute(select(PlatformSetting).where(PlatformSetting.key == REVISION_KEY).with_for_update())).scalar_one_or_none()
    previous = int(row.value.get("revision", 0)) if row else 0
    value = {"revision": previous + 1, "previous": previous, "changed_at": datetime.now(timezone.utc).isoformat(),
             "change": change, "actor": actor, "source": source}
    await session.execute(insert(PlatformSetting).values(key=REVISION_KEY, value=value)
                          .on_conflict_do_update(index_elements=[PlatformSetting.key], set_={"value": value}))
    return value


async def announce(redis: Redis | None, value: dict[str, Any]) -> None:
    await events.publish(redis, EVENT, value, "api")


async def read_acks(redis: Redis) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for service in SERVICES:
        raw = await redis.get(ACK_PREFIX + service)
        if raw:
            try:
                out[service] = json.loads(raw)
            except ValueError:
                continue
    return out


def sync_status(db_revision: int, acks: dict[str, dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Per service: SYNCED (acknowledged the database revision), OUT_OF_SYNC
    (running an older one, or its reload failed) or NOT_REPORTING (no fresh
    ack: stopped, disabled, or not deployed)."""
    now = now or datetime.now(timezone.utc)
    services = []
    for name, applies in SERVICES.items():
        ack = acks.get(name)
        age = None
        if ack and ack.get("at"):
            try:
                age = (now - datetime.fromisoformat(ack["at"])).total_seconds()
            except ValueError:
                age = None
        if (ack is None or age is None or age > ACK_TTL_SECONDS) and name in OPTIONAL:
            status = "NOT_DEPLOYED"
        elif ack is None or age is None or age > ACK_TTL_SECONDS:
            status = "NOT_REPORTING"
        elif not ack.get("ok", True):
            status = "OUT_OF_SYNC"
        elif int(ack.get("revision", -1)) >= db_revision:
            status = "SYNCED"
        else:
            status = "OUT_OF_SYNC"
        services.append({"service": name, "applies": applies, "status": status,
                         "revision": ack.get("revision") if ack else None, "loaded_at": ack.get("loaded_at") if ack else None,
                         "ack_age_seconds": round(age, 1) if age is not None else None,
                         "error": ack.get("error") if ack else None, "reloaded": ack.get("reloaded") if ack else None,
                         "effective": ack.get("effective") if ack else None})
    reporting = [s for s in services if s["status"] not in ("NOT_REPORTING", "NOT_DEPLOYED")]
    overall = ("SYNCED" if reporting and all(s["status"] == "SYNCED" for s in reporting)
               else "OUT_OF_SYNC" if any(s["status"] == "OUT_OF_SYNC" for s in services) else "NOT_REPORTING")
    return {"status": overall, "services": services}


async def effective_snapshot(session: AsyncSession) -> dict[str, Any]:
    """What a service reads from the database right now: global mode,
    strategy modes and, per Solana engine, which risk-settings scope and
    version applies (an engine with its own saved settings ignores GLOBAL)."""
    from yonixalpha_core.safety import store
    from yonixalpha_core.strategies.catalog import MODE_KEYS

    risk = {}
    for engine in ("solana_fresh", "solana_migration", "solana_momentum"):
        s, meta = await store.load_settings(session, engine)
        source = f"{meta['scope']} v{meta['version']}"
        if meta.get("overrides") and meta.get("global_version"):
            source += f" over GLOBAL v{meta['global_version']}"
        elif meta.get("legacy_full_copy"):
            source += " (full copy, ignores GLOBAL)"
        risk[engine] = {"source": source, "skip_duplicate_names": s.skip_duplicate_names}
    return {"global_mode": (await store.load_global_mode(session)).value,
            "modes": {k: (await store.load_strategy_mode(session, k)).value for k in MODE_KEYS},
            "risk_settings": risk}


Reloader = Callable[[], Awaitable[dict[str, Any] | None]]


class RuntimeConfigWatcher:
    """Runs in every service that applies dashboard settings. Database-read
    settings take effect on the service's next evaluation cycle by design;
    reloaders cover configuration a service holds in memory."""

    def __init__(self, service: str, session_factory, redis: Redis, reloaders: dict[str, Reloader] | None = None,
                 status: dict[str, Callable[[], dict]] | None = None):
        self.service, self.session_factory, self.redis = service, session_factory, redis
        self.reloaders = dict(reloaders or {})
        self.status = dict(status or {})  # live state reported with every ack (e.g. RPC health)
        self.revision: int | None = None
        self.loaded_at: str | None = None
        self.last_reloaded: dict[str, Any] = {}
        self.last_error: str | None = None

    async def sync(self, force: bool = False) -> int:
        async with self.session_factory() as session:
            rev = int((await current(session)).get("revision", 0))
            effective = await effective_snapshot(session)
        if force or rev != self.revision:
            errors, reloaded = [], {}
            for name, fn in self.reloaders.items():
                try:
                    reloaded[name] = await fn()
                except Exception as exc:  # noqa: BLE001 - one bad reloader must not stop the others
                    errors.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
            self.last_error = "; ".join(errors) or None
            if not errors:
                self.revision = rev
                self.loaded_at = datetime.now(timezone.utc).isoformat()
            self.last_reloaded = reloaded
            log.info("config.reloaded", service=self.service, revision=rev, ok=not errors, reloaded=list(reloaded))
        await self._ack(effective, rev)
        return rev

    async def _ack(self, effective: dict[str, Any], db_revision: int) -> None:
        ack = {"service": self.service, "revision": self.revision if self.revision is not None else -1,
               "db_revision_seen": db_revision, "loaded_at": self.loaded_at, "at": datetime.now(timezone.utc).isoformat(),
               "ok": self.last_error is None, "error": self.last_error, "reloaded": self.last_reloaded, "effective": effective,
               "status": {k: _safe(fn) for k, fn in self.status.items()}}
        try:
            await self.redis.set(ACK_PREFIX + self.service, json.dumps(ack, default=str), ex=ACK_TTL_SECONDS * 4)
        except Exception as exc:  # noqa: BLE001
            log.warning("config.ack_failed", service=self.service, error=type(exc).__name__)

    async def run(self, stop_event: asyncio.Event) -> None:
        pubsub = None
        while not stop_event.is_set():
            try:
                if pubsub is None:
                    pubsub = self.redis.pubsub()
                    await pubsub.subscribe(events.CHANNEL)
                await self.sync(force=self.revision is None)
                deadline = asyncio.get_running_loop().time() + POLL_SECONDS
                while not stop_event.is_set():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=min(remaining, 1.0))
                    if msg and _is_config_event(msg.get("data")):
                        break
            except Exception as exc:  # noqa: BLE001 - the watcher must survive Redis/DB hiccups
                log.warning("config.watch_failed", service=self.service, error=f"{type(exc).__name__}: {exc}"[:200])
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    pubsub = None
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass


def _safe(fn: Callable[[], dict]) -> dict | None:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}


def _is_config_event(data: Any) -> bool:
    if not data:
        return False
    try:
        return json.loads(data).get("type") == EVENT
    except (ValueError, TypeError, AttributeError):
        return False
