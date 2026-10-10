"""Event-driven re-evaluation for safety-gate candidates.

Before this, a gate candidate was evaluated at most every 30 s from a loop
that slept 15 s between passes, whatever happened to the token in between
(docs/EARLY_ENTRY_ROOT_CAUSE_AUDIT.md). Two changes, neither touching any
rule the gate applies:

  wake      the funnel (and a PAPER-mode entry strategy) pushes a new
            candidate's id on GATE_WAKE; the decision engine's loop waits on
            that list instead of a fixed sleep, so a promoted token is
            evaluated within about a second, newest candidates first.
  events    a candidate whose token had a meaningful event since its last
            evaluation (significant buy, meaningful SOL inflow, phase /
            acceleration change, seller surge, creator sell, liquidity
            change, migration) may be re-evaluated after
            event_min_interval_seconds instead of the 30 s fallback. The
            fallback stays: nothing is evaluated less often than before.

The event source is the per-mint state the early-entry shadow pass already
computes from the stream (entry_store.STATE_KEY): one Redis read, no RPC.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

GATE_WAKE = "yx:gate:wake"
FINGERPRINT_KEY = "yx:gate:fp:"
FINGERPRINT_TTL = 3600
DEFAULT_MIN_INTERVAL_SECONDS = 10

SIGNIFICANT_BUY_SOL = 0.5
MEANINGFUL_INFLOW_SOL = 1.0
SELLER_SURGE = 5
LIQUIDITY_CHANGE = 0.2


def fingerprint(state: dict[str, Any] | None) -> dict[str, Any]:
    """The fields of an entry-intelligence state an event is judged on."""
    s = state or {}
    f = s.get("features") or {}
    return {"trades_total": f.get("trades_total") or 0, "net_inflow_sol_total": f.get("net_inflow_sol_total") or 0.0,
            "unique_sellers": f.get("unique_sellers") or 0, "creator_sold": bool(f.get("creator_sold")),
            "sol_accumulated": f.get("sol_accumulated"), "phase": s.get("phase"),
            "max_buy_sol_10s": ((f.get("w10") or {}).get("max_buy_sol")) or 0.0,
            "migrated": bool(s.get("migrated")), "at": s.get("evaluated_at")}


def meaningful(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Reasons the token changed meaningfully between two fingerprints."""
    if not prev:
        return []
    why: list[str] = []
    if cur["trades_total"] > prev["trades_total"] and cur["max_buy_sol_10s"] >= SIGNIFICANT_BUY_SOL:
        why.append(f"significant buy ({cur['max_buy_sol_10s']:.2f} SOL)")
    if cur["net_inflow_sol_total"] - prev["net_inflow_sol_total"] >= MEANINGFUL_INFLOW_SOL:
        why.append(f"net inflow +{cur['net_inflow_sol_total'] - prev['net_inflow_sol_total']:.2f} SOL")
    if cur["phase"] and prev.get("phase") and cur["phase"] != prev["phase"]:
        why.append(f"phase {prev['phase']} -> {cur['phase']}")
    if cur["unique_sellers"] - prev["unique_sellers"] >= SELLER_SURGE:
        why.append(f"seller surge (+{cur['unique_sellers'] - prev['unique_sellers']} sellers)")
    if cur["creator_sold"] and not prev.get("creator_sold"):
        why.append("creator sold")
    a, b = prev.get("sol_accumulated"), cur.get("sol_accumulated")
    if a and b is not None and abs(b - a) / a >= LIQUIDITY_CHANGE:
        why.append(f"curve liquidity {a:.2f} -> {b:.2f} SOL")
    if cur["migrated"] and not prev.get("migrated"):
        why.append("migration / route change")
    return why


async def wake(redis, candidate_id: Any) -> None:
    if redis is None:
        return
    pipe = redis.pipeline()
    pipe.rpush(GATE_WAKE, str(candidate_id))
    pipe.ltrim(GATE_WAKE, -500, -1)
    pipe.expire(GATE_WAKE, 600)
    await pipe.execute()


async def wait_for_wake(redis, stop_event: asyncio.Event, timeout: float) -> list[str]:
    """Waits until a candidate is pushed, the stop event is set, or
    `timeout` passes; returns (and drains) the pushed ids."""
    deadline = time.monotonic() + timeout
    while not stop_event.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            return []
        try:
            got = await redis.blpop([GATE_WAKE], timeout=max(1, min(int(left), 2)))
        except Exception:  # noqa: BLE001 - Redis hiccup: fall back to the timer
            await asyncio.sleep(min(left, 1.0))
            continue
        if got:
            ids = [got[1].decode() if isinstance(got[1], bytes) else got[1]]
            while True:
                more = await redis.lpop(GATE_WAKE)
                if not more:
                    break
                ids.append(more.decode() if isinstance(more, bytes) else more)
            return ids
    return []


async def due_by_event(redis, candidate_id: Any, mint: str, min_interval: float) -> list[str]:
    """Meaningful events since this candidate's last evaluation, when that
    evaluation is at least `min_interval` old; [] otherwise."""
    raw = await redis.get(FINGERPRINT_KEY + str(candidate_id))
    if not raw:
        return []
    prev = json.loads(raw)
    if time.time() - float(prev.get("_evaluated_ts") or 0) < min_interval:
        return []
    from yonixalpha_core.entry_store import read_state

    cur = fingerprint(await read_state(redis, mint))
    return meaningful(prev, cur)


async def remember(redis, candidate_id: Any, mint: str) -> None:
    """The fingerprint this evaluation saw (next events are judged against it)."""
    from yonixalpha_core.entry_store import read_state

    fp = fingerprint(await read_state(redis, mint))
    fp["_evaluated_ts"] = time.time()
    await redis.set(FINGERPRINT_KEY + str(candidate_id), json.dumps(fp, default=str), ex=FINGERPRINT_TTL)
