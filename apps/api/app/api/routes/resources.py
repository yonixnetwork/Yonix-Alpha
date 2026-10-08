"""Low-resource operation (2026-10-08): host resources, the system resource
mode and the copy-trading status (yonixalpha_core.resources /
operating_mode).

    GET  /system/resources        host, Postgres, Redis and per-service usage,
                                  the resource level and what is paused
    PUT  /system/resource-mode    NORMAL | LOW_RESOURCE | EMERGENCY
    GET  /copy/trading-status     the copy status and whether it may resume
    POST /copy/trading-status     SUSPENDED always; ACTIVE / THROTTLED only
                                  while the resume check passes (never
                                  because the button was pressed)
Everything here is read-only except the two writes, which are audited.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.health_state import SERVICES
from app.api.util import audit, jsonable, user_id
from yonixalpha_core import operating_mode, resources
from yonixalpha_core.config import Settings
from yonixalpha_core.events import read_heartbeats

router = APIRouter(tags=["resources"])

CPU_PREV_KEY = "yx:resources:cpu_prev"  # service -> previous heartbeat (at, cpu_s), for a CPU share
PG_SETTINGS = ("shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem", "max_connections",
               "max_parallel_workers_per_gather", "jit", "random_page_cost")


class ModeIn(BaseModel):
    mode: str = Field(..., pattern="^(NORMAL|LOW_RESOURCE|EMERGENCY)$")
    note: str | None = Field(default=None, max_length=300)


class CopyStatusIn(BaseModel):
    status: str = Field(..., pattern="^(ACTIVE|THROTTLED|SUSPENDED)$")
    note: str | None = Field(default=None, max_length=300)


async def _db_latency_ms(db: AsyncSession) -> float | None:
    try:
        t0 = time.monotonic()
        await db.execute(text("SELECT 1"))
        return round((time.monotonic() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        return None


async def _postgres(db: AsyncSession) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        rows = (await db.execute(text(
            "SELECT coalesce(state, 'background'), count(*), count(*) FILTER (WHERE state = 'active' "
            "AND now() - query_start > interval '2 seconds') FROM pg_stat_activity "
            "WHERE datname = current_database() GROUP BY 1"))).all()
        out["connections"] = {state: n for state, n, _ in rows}
        out["statements_over_2s"] = sum(slow for *_, slow in rows)
        out["database_size_mb"] = round((await db.execute(text(
            "SELECT pg_database_size(current_database())"))).scalar_one() / 2**20)
        out["settings"] = {k: v for k, v in (await db.execute(text(
            "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(:n)"),
            {"n": list(PG_SETTINGS)})).all()}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out


async def _redis(redis: Redis) -> dict[str, Any]:
    try:
        info = await redis.info("memory")
        return {"used_mb": round(info.get("used_memory", 0) / 2**20, 1),
                "peak_mb": round(info.get("used_memory_peak", 0) / 2**20, 1),
                "maxmemory_mb": round(info.get("maxmemory", 0) / 2**20, 1) or None,
                "maxmemory_policy": info.get("maxmemory_policy"), "keys": await redis.dbsize()}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


async def _workers(redis: Redis) -> list[dict[str, Any]]:
    """Each service's resident memory and CPU share from its own heartbeat
    (yonixalpha_core.events): CPU share = CPU seconds used between this and
    the previous heartbeat seen here, over the time between them."""
    hbs = await read_heartbeats(redis, SERVICES)
    prev_raw = await redis.hgetall(CPU_PREV_KEY)
    prev = {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in (prev_raw or {}).items()}
    out = []
    for s in SERVICES:
        hb = hbs.get(s)
        if not hb:
            out.append({"service": s, "heartbeat": None})
            continue
        row = {"service": s, "heartbeat": hb.get("at"), "rss_mb": hb.get("rss_mb"), "cpu_s": hb.get("cpu_s"),
               "cpu_pct": None}
        p = prev.get(s) or {}
        if hb.get("cpu_s") is not None and hb.get("at"):
            at = datetime.fromisoformat(hb["at"]).timestamp()
            if p.get("at") == at:  # same heartbeat as last time: the share computed then
                row["cpu_pct"] = p.get("pct")
            else:
                if p.get("at") and at > p["at"] and hb["cpu_s"] >= p.get("cpu_s", 0):
                    row["cpu_pct"] = round(100 * (hb["cpu_s"] - p["cpu_s"]) / (at - p["at"]), 1)
                await redis.hset(CPU_PREV_KEY, s, json.dumps({"at": at, "cpu_s": hb["cpu_s"], "pct": row["cpu_pct"]}))
        out.append(row)
    return out


async def _copy_view(db: AsyncSession, settings: Settings, st: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    latency = await _db_latency_ms(db)
    safe, why = resources.copy_resume_check(sample, latency, settings)
    status = st["copy_trading_effective"]
    return {"status": status, "configured": st["copy_trading"], "source": st["copy_trading_source"],
            "label": f"{status} — LOW SERVER RESOURCES" if status == "SUSPENDED" else status,
            "reason": operating_mode.SUSPENDED_REASON if status == "SUSPENDED" else None,
            "changed_at": st.get("changed_at"), "changed_by": st.get("changed_by"),
            "resume": {"safe": safe and st["resource_mode"] != "EMERGENCY",
                       "recommendation": "OK" if safe and st["resource_mode"] != "EMERGENCY" else "WAIT",
                       "blocked_by": why + (["EMERGENCY resource mode"] if st["resource_mode"] == "EMERGENCY" else []),
                       "auto_resume": False, "db_latency_ms": latency,
                       "thresholds": {"min_free_ram_mb": settings.COPY_RESUME_MIN_FREE_RAM_MB,
                                      "max_cpu_load_per_cpu": settings.COPY_MAX_CPU_LOAD,
                                      "max_swap_usage_mb": settings.COPY_MAX_SWAP_USAGE_MB,
                                      "max_db_latency_ms": settings.COPY_MAX_DB_LATENCY_MS}},
            "while_suspended": {"stopped": ["target watching (Solana, BSC, Robinhood)", "copy entries and mirrored sells",
                                            "copy outcome evaluation", "wallet profiles and analytics",
                                            "wallet enrichment", "wallet ML and missed winners"],
                                "kept": ["stop loss / trailing / exits of copy positions already open",
                                         "all copy history, targets and settings (nothing deleted)"]}}


@router.get("/system/resources")
async def system_resources(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                           settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    sample = resources.sample()
    lvl, why = resources.level(sample, settings)
    st = await operating_mode.load(db, settings)
    paused = []
    if st["copy_trading_effective"] == "SUSPENDED":
        paused.append("copy trading")
    if st["resource_mode"] == "EMERGENCY" or (st["resource_mode"] == "LOW_RESOURCE" and lvl == resources.CRITICAL):
        paused += ["ML training", "automatic ML Review refreshes"]
    elif st["resource_mode"] == "LOW_RESOURCE":
        paused.append(f"ML training reduced to once every {settings.ML_TRAINING_INTERVAL_LOW_RESOURCE_H} h")
    return jsonable({
        "mode": {"resource_mode": st["resource_mode"], "source": st["resource_mode_source"],
                 "label": st["resource_mode"].replace("_", " ") + " MODE", "changed_at": st.get("changed_at"),
                 "changed_by": st.get("changed_by"), "note": st.get("note")},
        "level": lvl, "level_reasons": why, "host": sample,
        "thresholds": {k: getattr(settings, k) for k in (
            "RESOURCE_WARN_AVAILABLE_MB", "RESOURCE_CRITICAL_AVAILABLE_MB", "RESOURCE_WARN_LOAD_PER_CPU",
            "RESOURCE_CRITICAL_LOAD_PER_CPU", "RESOURCE_WARN_MEMORY_PRESSURE_PCT", "RESOURCE_CRITICAL_MEMORY_PRESSURE_PCT")},
        "postgres": await _postgres(db), "redis": await _redis(redis), "workers": await _workers(redis),
        "copy_trading": await _copy_view(db, settings, st, sample),
        "paused_now": paused, "priorities": operating_mode.PRIORITIES,
        "note": "host figures are the droplet's (read from /proc); per-service memory and CPU come from each "
                "service's own heartbeat. CRITICAL pauses priority-3 work only; execution, open positions, exits, "
                "risk and Solana discovery are never paused."})


@router.put("/system/resource-mode")
async def set_resource_mode(body: ModeIn, request: Request, db: AsyncSession = Depends(get_db),
                            settings: Settings = Depends(get_settings),
                            username: str = Depends(get_current_username)) -> dict:
    before = await operating_mode.load(db, settings)
    await operating_mode.save(db, username=username, user_id=await user_id(db, username), resource_mode=body.mode,
                              note=body.note)
    await audit(db, username, request, "system.resource_mode",
                {"from": before["resource_mode"], "to": body.mode, "note": body.note})
    await db.commit()
    return await operating_mode.load(db, settings)


@router.get("/copy/trading-status")
async def copy_trading_status(db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                              _: str = Depends(get_current_username)) -> dict:
    sample = resources.sample()
    st = await operating_mode.load(db, settings)
    view = await _copy_view(db, settings, st, sample)
    lvl, why = resources.level(sample, settings)
    m = sample.get("memory") or {}
    return jsonable({**view, "resource_mode": st["resource_mode"], "resource_level": lvl, "level_reasons": why,
                     "current": {"ram_available_mb": m.get("available_mb"), "ram_total_mb": m.get("total_mb"),
                                 "swap_used_mb": m.get("swap_used_mb"), "load": sample.get("load"),
                                 "cpus": sample.get("cpus"), "cpu_busy_pct": sample.get("cpu_busy_pct"),
                                 "db_latency_ms": view["resume"]["db_latency_ms"]}})


@router.post("/copy/trading-status")
async def set_copy_trading_status(body: CopyStatusIn, request: Request, db: AsyncSession = Depends(get_db),
                                  settings: Settings = Depends(get_settings),
                                  username: str = Depends(get_current_username)) -> dict:
    st = await operating_mode.load(db, settings)
    if body.status != "SUSPENDED":
        view = await _copy_view(db, settings, st, resources.sample())
        if not view["resume"]["safe"]:
            raise HTTPException(409, {"code": "RESUME_UNSAFE", "recommendation": "WAIT",
                                      "message": "copy trading was not resumed: server resources are below the "
                                                 "configured resume thresholds",
                                      "blocked_by": view["resume"]["blocked_by"],
                                      "thresholds": view["resume"]["thresholds"]})
    await operating_mode.save(db, username=username, user_id=await user_id(db, username), copy_trading=body.status,
                              note=body.note)
    await audit(db, username, request, "copy.trading_status",
                {"from": st["copy_trading"], "to": body.status, "note": body.note})
    await db.commit()
    return await copy_trading_status(db, settings, username)
