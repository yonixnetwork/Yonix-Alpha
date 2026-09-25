"""Connection monitoring: one state per dependency, derived only from
evidence — a live probe (Postgres, Redis), a service heartbeat with a TTL,
the per-venue success/failure record services put in their heartbeats, the
execution workers' readiness reports, or the external bots' cached
status. Nothing is ever assumed CONNECTED.

CONNECTED       recent success, no current failures
DEGRADED        working but with failures / partial outage
STALE           last evidence too old to trust
UNAVAILABLE     probe failed, heartbeat expired after the service was seen,
                or repeated failures with no recent success
NOT CONFIGURED  the credentials/URL it needs are not set (or live
                execution is switched off by the environment locks); it
                does not count against the overall state
UNKNOWN         configured, but nothing observed yet
"""

import json
import time
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.events import read_heartbeats
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FEATURES_FOR_MODEL
from yonixalpha_core import external_bots, futures_live
from yonixalpha_core.solana import pump_stream, pumpportal_ws

SERVICES = ["data-solana", "data-binance", "engine-solana-discovery", "engine-solana-migration", "engine-solana-momentum",
            "engine-binance-futures", "decision-engine", "ml", "paper-trading", "execution-futures"]
VENUE_REPORTERS = ("decision-engine", "paper-trading", "execution-futures")
PUMPPORTAL_STALE_SECONDS = 120
PUMPPORTAL_OFFLINE_SECONDS = 600
MIN_COVERAGE_SAMPLE = 20
MIN_COVERAGE = 0.8
HEARTBEAT_STALE_SECONDS = 90
VENUE_STALE_SECONDS = 300
VENUE_OFFLINE_FAILURES = 3
STREAM_STALE_SECONDS = 60
STREAM_OFFLINE_SECONDS = 300
NOT_CONFIGURED = "NOT CONFIGURED"
STATES = ["CONNECTED", "DEGRADED", "STALE", "UNAVAILABLE", NOT_CONFIGURED, "UNKNOWN"]
_RANK = {"CONNECTED": 0, "UNKNOWN": 1, "STALE": 2, "DEGRADED": 3, "UNAVAILABLE": 4}


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
    counted = [s for s in states if s != NOT_CONFIGURED]  # a disabled module never blocks the others
    if not counted:
        return "UNKNOWN"
    return max(counted, key=lambda s: _RANK[s])


async def _probe_db(db: AsyncSession) -> dict:
    t = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return conn("postgres", "infrastructure", "UNAVAILABLE", f"query failed: {type(exc).__name__}")
    return conn("postgres", "infrastructure", "CONNECTED", "SELECT 1 ok", latency_ms=round((time.perf_counter() - t) * 1000, 1))


async def _probe_redis(redis: Redis) -> dict:
    t = time.perf_counter()
    try:
        await redis.ping()
        info = await redis.info("memory")
    except Exception as exc:  # noqa: BLE001
        return conn("redis", "infrastructure", "UNAVAILABLE", f"ping failed: {type(exc).__name__}")
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
        return "UNAVAILABLE", f"{fails} consecutive failures", extra
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
            state = "UNAVAILABLE" if s in seen else "UNKNOWN"
            out.append(conn(s, "service", state, "no heartbeat" + (" (service has run before)" if s in seen else "")))
            continue
        age = _age(hb.get("at"), now)
        state = "STALE" if age is None or age > HEARTBEAT_STALE_SECONDS else ("CONNECTED" if hb.get("status") == "ok" else "DEGRADED")
        out.append(conn(s, "service", state, f"heartbeat {int(age or 0)} s ago", rss_mb=hb.get("rss_mb"), last_seen_at=hb.get("at")))

    # Solana RPC: endpoint health as reported by the services that own RPC clients.
    rpc = [e for s in ("data-solana", "engine-solana-momentum") for e in (((hbs.get(s) or {}).get("detail") or {}).get("rpc") or [])]
    if not rpc:
        state, detail = (NOT_CONFIGURED, "SOLANA_RPC_URL (or HELIUS_API_KEY) not set") if not settings.SOLANA_RPC_URL \
            else ("UNKNOWN", "no RPC-owning service heartbeat")
    elif all(e.get("disabled") for e in rpc):
        state, detail = "UNAVAILABLE", "every RPC endpoint is disabled after failures"
    elif any(e.get("consecutive_failures") for e in rpc):
        state, detail = "DEGRADED", "some RPC endpoints are failing"
    else:
        state, detail = "CONNECTED", f"{len(rpc)} endpoint report(s) healthy"
    out.append(conn("solana_rpc", "solana", state, detail, endpoints=rpc))

    hb = await pump_stream.heartbeat(redis)
    age = (now - hb).total_seconds() if hb else None
    if age is None:
        state = NOT_CONFIGURED if not settings.SOLANA_WS_URL else "UNAVAILABLE"
        detail = "no stream message received" if settings.SOLANA_WS_URL else "SOLANA_WS_URL (or HELIUS_API_KEY) not set"
    else:
        state = "CONNECTED" if age <= STREAM_STALE_SECONDS else ("STALE" if age <= STREAM_OFFLINE_SECONDS else "UNAVAILABLE")
        detail = f"last pump.fun log {int(age)} s ago"
    cov = await pumpportal_ws.coverage(redis)
    if state == "CONNECTED" and cov["coverage"] is not None and cov["announced"] >= MIN_COVERAGE_SAMPLE \
            and cov["coverage"] < MIN_COVERAGE:
        state, detail = "DEGRADED", f"{detail}; only {cov['coverage']:.0%} of PumpPortal-announced tokens seen on chain"
    out.append(conn("solana_ws", "solana", state, detail, last_message_at=hb.isoformat() if hb else None))

    rpc_conn = out[-2]
    helius = bool(getattr(settings, "HELIUS_API_KEY", None)) or "helius" in (settings.SOLANA_RPC_URL or "")
    out.append(conn("helius", "solana", rpc_conn["state"] if helius else NOT_CONFIGURED,
                    ("Helius is the configured RPC: " + rpc_conn["detail"]) if helius else "HELIUS_API_KEY not set and the "
                    "RPC URL is not a Helius endpoint"))

    pp = await pumpportal_ws.heartbeat(redis)
    pp_age = time.time() - pp if pp else None
    if pp_age is None:
        state, detail = "UNKNOWN", "no PumpPortal message received (engine-solana-discovery not running?)"
    else:
        state = ("CONNECTED" if pp_age <= PUMPPORTAL_STALE_SECONDS else "STALE" if pp_age <= PUMPPORTAL_OFFLINE_SECONDS
                 else "UNAVAILABLE")
        detail = f"last data message {int(pp_age)} s ago" + (
            f"; on-chain coverage {cov['coverage']:.0%} of {cov['announced']}" if cov["coverage"] is not None else "")
    out.append(conn("pumpportal", "solana", state, detail, coverage=cov,
                    metered_trades="PUMPPORTAL_API_KEY set" if getattr(settings, "PUMPPORTAL_API_KEY", None)
                    else "no key: free subscriptions only"))
    wallet = getattr(settings, "WALLET_PRIVATE_KEY", None)
    out.append(conn("pumpportal_trade", "execution", NOT_CONFIGURED if not wallet else "UNKNOWN",
                    "WALLET_PRIVATE_KEY not set: Local Transaction API unused" if not wallet
                    else "Local Transaction API needs no key; evidence only from live orders (see Live Execution)"))

    for venue in ("binance", "bybit", "hyperliquid", "jupiter", "mt5"):
        records = [((hbs.get(s) or {}).get("detail") or {}).get("venues", {}).get(venue) for s in VENUE_REPORTERS]
        state, detail, extra = _venue_state([r for r in records if r], now)
        if venue == "mt5" and not getattr(settings, "MT5_BRIDGE_URL", None):
            state, detail = NOT_CONFIGURED, "MT5_BRIDGE_URL / MT5_BRIDGE_TOKEN not set"
        if venue == "jupiter":
            detail += " (api.jup.ag with JUPITER_API_KEY)" if getattr(settings, "JUPITER_API_KEY", None) else \
                " (keyless lite-api.jup.ag, deprecated by Jupiter)"
        out.append(conn(venue, "exchange" if venue != "jupiter" else "solana", state, detail, **extra))

    # Live execution per venue: the execution-futures worker's own readiness report.
    for venue in ("binance", "bybit", "hyperliquid", "mt5"):
        raw = await redis.get(futures_live.READY_KEY.format(venue=venue))
        r = json.loads(raw) if raw else None
        if r is None:
            state, detail = "UNKNOWN", "execution-futures has not reported (service not running?)"
        elif r["status"] in ("not_configured", "disabled"):
            state, detail = NOT_CONFIGURED, r.get("reason") or r["status"]
        elif r["status"] == "unavailable":
            state, detail = "UNAVAILABLE", r.get("reason") or "unavailable"
        else:
            age = _age(r.get("at"), now)
            if age is not None and age > VENUE_STALE_SECONDS:
                state, detail = "STALE", f"last report {int(age)} s ago"
            elif r["status"] == "ready":
                state, detail = "CONNECTED", f"balance {r.get('balance')} {r.get('quote') or ''} synced {int(age or 0)} s ago"
            else:
                state, detail = "DEGRADED", r.get("reason") or r["status"]
        out.append(conn(f"{venue}_execution", "execution", state, detail))

    for name, bot in external_bots.BOTS.items():
        if not external_bots.configured(settings, bot):
            out.append(conn(f"control_api:{name}", "control_api", NOT_CONFIGURED, f"{bot.url_setting} not set"))
            continue
        st = await external_bots.cached(redis, name)
        if st is None:
            out.append(conn(f"control_api:{name}", "control_api", "UNKNOWN", "not polled yet (execution-futures not running?)"))
        else:
            out.append(conn(f"control_api:{name}", "control_api", st["state"],
                            st.get("error") or f"running={st.get('running')} position={'yes' if st.get('position') else 'no'}",
                            checked_at=st.get("checked_at")))

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
