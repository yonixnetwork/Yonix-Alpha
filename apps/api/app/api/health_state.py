"""Connection monitoring (spec §92): one state per dependency, derived only
from evidence — a live probe (Postgres, Redis), a service heartbeat with a
TTL, or the per-venue success/failure record services put in their
heartbeats. No evidence at all is UNKNOWN, never assumed CONNECTED.

CONNECTED  recent success, no current failures
DEGRADED   working but with failures / partial outage
STALE      last evidence too old to trust
OFFLINE    probe failed, heartbeat expired after the service was seen, or
           repeated failures with no recent success
UNKNOWN    nothing observed yet
"""

import time
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.events import read_heartbeats
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FEATURES_FOR_MODEL
from yonixalpha_core.solana import pump_stream

SERVICES = ["data-solana", "data-binance", "engine-solana-discovery", "engine-solana-migration", "engine-solana-momentum",
            "engine-binance-futures", "decision-engine", "ml", "paper-trading"]
HEARTBEAT_STALE_SECONDS = 90
VENUE_STALE_SECONDS = 300
VENUE_OFFLINE_FAILURES = 3
STREAM_STALE_SECONDS = 60
STREAM_OFFLINE_SECONDS = 300
STATES = ["CONNECTED", "DEGRADED", "STALE", "OFFLINE", "UNKNOWN"]
_RANK = {"CONNECTED": 0, "UNKNOWN": 1, "STALE": 2, "DEGRADED": 3, "OFFLINE": 4}


def _age(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


def conn(name: str, category: str, state: str, detail: str, **extra) -> dict[str, Any]:
    return {"name": name, "category": category, "state": state, "detail": detail, **extra}


def worst(states: list[str]) -> str:
    """Worst state; an UNKNOWN dependency keeps the overall from reading
    CONNECTED (nothing is claimed healthy without evidence)."""
    if not states:
        return "UNKNOWN"
    return max(states, key=lambda s: _RANK[s])


async def _probe_db(db: AsyncSession) -> dict:
    t = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return conn("postgres", "infrastructure", "OFFLINE", f"query failed: {type(exc).__name__}")
    return conn("postgres", "infrastructure", "CONNECTED", "SELECT 1 ok", latency_ms=round((time.perf_counter() - t) * 1000, 1))


async def _probe_redis(redis: Redis) -> dict:
    t = time.perf_counter()
    try:
        await redis.ping()
        info = await redis.info("memory")
    except Exception as exc:  # noqa: BLE001
        return conn("redis", "infrastructure", "OFFLINE", f"ping failed: {type(exc).__name__}")
    return conn("redis", "infrastructure", "CONNECTED", f"memory {info.get('used_memory_human')} / max {info.get('maxmemory_human')}",
                latency_ms=round((time.perf_counter() - t) * 1000, 1))


def _venue_state(records: list[dict], now: datetime) -> tuple[str, str, dict]:
    if not records:
        return "UNKNOWN", "no calls observed by any running service", {}
    last_ok = max((r.get("last_ok_at") for r in records if r.get("last_ok_at")), default=None)
    fails = max(int(r.get("consecutive_failures") or 0) for r in records)
    last_error = next((r.get("last_error") for r in sorted(records, key=lambda r: r.get("last_error_at") or "", reverse=True)
                       if r.get("last_error")), None)
    age = _age(last_ok, now)
    extra = {"last_ok_at": last_ok, "consecutive_failures": fails, "last_error": last_error}
    if fails >= VENUE_OFFLINE_FAILURES and (age is None or age > VENUE_STALE_SECONDS):
        return "OFFLINE", f"{fails} consecutive failures", extra
    if fails > 0:
        return "DEGRADED", f"{fails} recent failure(s): {last_error}", extra
    if age is None:
        return "UNKNOWN", "no successful call yet", extra
    if age > VENUE_STALE_SECONDS:
        return "STALE", f"last success {int(age)} s ago", extra
    return "CONNECTED", f"last success {int(age)} s ago", extra


async def connections(db: AsyncSession, redis: Redis, settings: Any) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = [await _probe_db(db), await _probe_redis(redis)]
    try:
        hbs = await read_heartbeats(redis, SERVICES)
    except Exception:  # noqa: BLE001
        hbs = {s: None for s in SERVICES}
    seen = set((await db.execute(select(SystemEvent.service).where(SystemEvent.service.in_(SERVICES)).distinct())).scalars())

    for s in SERVICES:
        hb = hbs.get(s)
        if hb is None:
            state = "OFFLINE" if s in seen else "UNKNOWN"
            out.append(conn(s, "service", state, "no heartbeat" + (" (service has run before)" if s in seen else "")))
            continue
        age = _age(hb.get("at"), now)
        state = "STALE" if age is None or age > HEARTBEAT_STALE_SECONDS else ("CONNECTED" if hb.get("status") == "ok" else "DEGRADED")
        out.append(conn(s, "service", state, f"heartbeat {int(age or 0)} s ago", rss_mb=hb.get("rss_mb"), last_seen_at=hb.get("at")))

    # Solana RPC: endpoint health as reported by the services that own RPC clients.
    rpc = [e for s in ("data-solana", "engine-solana-momentum") for e in (((hbs.get(s) or {}).get("detail") or {}).get("rpc") or [])]
    if not rpc:
        state, detail = ("UNKNOWN", "not configured (SOLANA_RPC_URL unset)") if not settings.SOLANA_RPC_URL \
            else ("UNKNOWN", "no RPC-owning service heartbeat")
    elif all(e.get("disabled") for e in rpc):
        state, detail = "OFFLINE", "every RPC endpoint is disabled after failures"
    elif any(e.get("consecutive_failures") for e in rpc):
        state, detail = "DEGRADED", "some RPC endpoints are failing"
    else:
        state, detail = "CONNECTED", f"{len(rpc)} endpoint report(s) healthy"
    out.append(conn("solana_rpc", "solana", state, detail, endpoints=rpc))

    hb = await pump_stream.heartbeat(redis)
    age = (now - hb).total_seconds() if hb else None
    if age is None:
        state = "UNKNOWN" if not settings.SOLANA_WS_URL else "OFFLINE"
        detail = "no stream message received" + ("" if settings.SOLANA_WS_URL else " (SOLANA_WS_URL unset)")
    else:
        state = "CONNECTED" if age <= STREAM_STALE_SECONDS else ("STALE" if age <= STREAM_OFFLINE_SECONDS else "OFFLINE")
        detail = f"last pump.fun log {int(age)} s ago"
    out.append(conn("solana_ws", "solana", state, detail, last_message_at=hb.isoformat() if hb else None))

    for venue in ("binance", "bybit", "hyperliquid"):
        records = [((hbs.get(s) or {}).get("detail") or {}).get("venues", {}).get(venue)
                   for s in ("decision-engine", "paper-trading")]
        state, detail, extra = _venue_state([r for r in records if r], now)
        out.append(conn(venue, "exchange", state, detail, **extra))

    drift = [n for n in FEATURES_FOR_MODEL if await redis.exists(f"{DRIFT_FLAG_PREFIX}{n}")]
    ml_hb = next(c for c in out if c["name"] == "ml")
    if drift:
        out.append(conn("ml_models", "ml", "DEGRADED", f"drift detected: {', '.join(drift)} (ignored by decisions)"))
    else:
        out.append(conn("ml_models", "ml", ml_hb["state"] if ml_hb["state"] != "CONNECTED" else "CONNECTED",
                        "no drift flags" if ml_hb["state"] == "CONNECTED" else ml_hb["detail"]))

    try:
        clients = int(await redis.get("yx:ws:clients") or 0)
    except Exception:  # noqa: BLE001
        clients = None
    out.append(conn("websocket", "realtime", "CONNECTED" if clients is not None else "UNKNOWN",
                    f"{clients} dashboard client(s) connected" if clients is not None else "unknown", clients=clients))
    return out
