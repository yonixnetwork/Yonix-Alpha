"""Scanner pipeline health (memecoin refocus Phase 3, 2026-10-20).

One view of whether discovery is actually WORKING, from evidence only: the
time each stage last did something, not whether a process is running.

  stream_event        last pump.fun program-log message (pump_stream heartbeat)
  pumpportal_event    last PumpPortal data message (independent launch feed)
  token_discovered    newest token seen created on chain (pump_stream RECENT)
  candidate_promoted  newest token handed to the safety gate (pump_stream PROMOTED)
  candidate_evaluated newest Solana gate decision (risk_assessments.evaluated_at, indexed)
  paper_trade         newest Solana PAPER position opened (paper_positions.created_at, indexed)

States: CONNECTED (recent), STALE (older than the stale limit), DISCONNECTED
(older than the offline limit) and DATA GAP (no evidence at all: never
recorded, or the key expired). A paper trade is shown for information only:
having no trade for hours can be correct, so it never sets the overall state.

Also: the gate wake-queue depth, PumpPortal message / disconnect counters,
and the last hour's Solana RPC outcomes (ok, HTTP 429, failed, failover
retries, shed background calls) from rpc_usage. Read-only; bounded queries.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import gate_events, rpc_usage
from yonixalpha_core.db.models import PaperPosition, RiskAssessment
from yonixalpha_core.solana import pump_stream, pumpportal_ws
from yonixalpha_core.solana.rpc_registry import FAILOVER_LOG

CONNECTED, STALE, DISCONNECTED, DATA_GAP, INFO = "CONNECTED", "STALE", "DISCONNECTED", "DATA GAP", "INFO"
_RANK = {CONNECTED: 0, STALE: 1, DATA_GAP: 2, DISCONNECTED: 3}

# (stage, label, stale after s, disconnected after s); None = information only
STAGES: tuple[tuple[str, str, int | None, int | None], ...] = (
    ("stream_event", "pump.fun on-chain stream: last message", 60, 300),
    ("pumpportal_event", "PumpPortal feed: last message", 120, 600),
    ("token_discovered", "last token discovered (created on chain)", 300, 1800),
    ("candidate_promoted", "last token promoted to the safety gate", 1800, 7200),
    ("candidate_evaluated", "last Solana gate decision", 1800, 7200),
    ("paper_trade", "last Solana paper trade opened", None, None),
)
RPC_OUTCOMES = ("ok", "http_429", "failed", "rejected", "failover_retry", "shed")


def stage_state(age_s: float | None, stale_s: int | None, offline_s: int | None) -> str:
    if age_s is None:
        return DATA_GAP
    if stale_s is None:
        return INFO
    if age_s <= stale_s:
        return CONNECTED
    return STALE if offline_s is None or age_s <= offline_s else DISCONNECTED


async def _zset_newest(redis: Redis, key: str) -> float | None:
    got = await redis.zrevrange(key, 0, 0, withscores=True)
    return float(got[0][1]) if got else None


async def report(db: AsyncSession, redis: Redis, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    last: dict[str, float | None] = {}
    hb = await pump_stream.heartbeat(redis)
    last["stream_event"] = hb.timestamp() if hb else None
    last["pumpportal_event"] = await pumpportal_ws.heartbeat(redis)
    last["token_discovered"] = await _zset_newest(redis, pump_stream.RECENT)
    last["candidate_promoted"] = await _zset_newest(redis, pump_stream.PROMOTED)
    ev = await db.scalar(select(func.max(RiskAssessment.evaluated_at)).where(RiskAssessment.engine.like("solana%")))
    last["candidate_evaluated"] = ev.timestamp() if ev else None
    pt = await db.scalar(select(func.max(PaperPosition.created_at)).where(
        PaperPosition.engine.like("solana%"), func.coalesce(PaperPosition.execution_mode, "PAPER") == "PAPER"))
    last["paper_trade"] = pt.timestamp() if pt else None

    stages = []
    for key, label, stale_s, offline_s in STAGES:
        at = last[key]
        age = max(0.0, ts - at) if at is not None else None
        state = stage_state(age, stale_s, offline_s)
        detail = "no evidence recorded (never seen, or the key expired)" if age is None else f"{int(age)} s ago"
        stages.append({"stage": key, "label": label, "state": state, "last_at":
                       datetime.fromtimestamp(at, timezone.utc).isoformat() if at is not None else None,
                       "age_seconds": round(age, 1) if age is not None else None, "detail": detail,
                       "stale_after_seconds": stale_s, "disconnected_after_seconds": offline_s})
    counted = [s["state"] for s in stages if s["state"] != INFO]
    overall = max(counted, key=lambda s: _RANK[s]) if counted else DATA_GAP

    pp_stats = {k if isinstance(k, str) else k.decode(): int(v)
                for k, v in (await redis.hgetall(pumpportal_ws.STATS)).items()}
    usage = await rpc_usage.read_usage(redis, hours=1, now=now)
    rpc: dict[str, float] = {o: 0.0 for o in RPC_OUTCOMES}
    for field_, n in usage.items():
        parts = field_.split("|")
        if len(parts) == 5 and parts[4] in rpc:
            rpc[parts[4]] += n
    calls = rpc["ok"] + rpc["http_429"] + rpc["failed"] + rpc["rejected"]
    return {
        "at": now.isoformat(), "overall": overall, "stages": stages,
        "queues": {"gate_wake_depth": await redis.llen(gate_events.GATE_WAKE),
                   "rpc_failovers_logged": await redis.llen(FAILOVER_LOG)},
        "pumpportal_counters": pp_stats,
        "rpc_last_hour": {**{k: int(v) for k, v in rpc.items()},
                          "rate_limited_share": round(rpc["http_429"] / calls, 4) if calls else None},
        "pumpportal_coverage": await pumpportal_ws.coverage(redis, now=time.time()),
        "notes": ["Every state is derived from the last recorded activity, never from a process merely running.",
                  "DATA GAP: no evidence at all. While Memecoin is paused every stage goes STALE, then DISCONNECTED.",
                  "A paper trade is information only: hours without a trade can be correct."],
    }
