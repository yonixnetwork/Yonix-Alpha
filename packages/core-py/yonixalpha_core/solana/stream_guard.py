"""Keeps fresh-token discovery complete when the RPC WebSocket loses events.

The on-chain program-log stream (pump_stream) is the only source of token
data. PumpPortal's free new-token feed (pumpportal_ws) announces every
pump.fun launch independently; `pumpportal_ws.coverage()` measures how many
of those the stream recorded. Two repairs use that cross-check:

- gap fill: a launch PumpPortal announced that the stream has not recorded
  after GAP_MIN_AGE seconds is read from its own transaction on chain
  (getTransaction of the create signature) and its CreateEvent is ingested
  exactly as if the stream had delivered it. PumpPortal only says which
  signature to look at; every field comes from the chain, and a transaction
  without a CreateEvent for that mint adds nothing.
- watchdog: when the stream is silent for STALL_SECONDS, or recorded less
  than MIN_COVERAGE of at least MIN_ANNOUNCED announced launches over
  WATCH_WINDOW, the WebSocket is reconnected (which moves it to the next
  provider) and an alert is sent, at most once per RECONNECT_COOLDOWN.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from redis.asyncio import Redis

from yonixalpha_core.logging import get_logger
from yonixalpha_core.solana import pump_stream, pumpportal_ws
from yonixalpha_core.solana.codec import b58decode
from yonixalpha_core.solana.pumpfun import PROGRAM_DATA_PREFIX, PUMP_PROGRAM_ID, decode_event, decode_log_events
from yonixalpha_core.solana.rpc import get_transaction_params
from yonixalpha_core.solana.txversion import instructions

log = get_logger("core.stream_guard")

GAP_MIN_AGE = 15  # seconds the stream gets to deliver a launch itself
GAP_MAX_AGE = 180  # older launches are past the fresh-token window
GAP_MAX_PER_PASS = 20
GAP_DONE_TTL = 3600
GAP_DONE = "yx:pump:gapfill"
GAP_STATS = "yx:pump:gapfill:stats"

WATCH_WINDOW = 600
MIN_ANNOUNCED = 10
MIN_COVERAGE = 0.5
STALL_SECONDS = 60
RECONNECT_COOLDOWN = 600


def create_lines(tx: dict[str, Any] | None, mint: str) -> list[str]:
    """The "Program data:" line(s) of `mint`'s CreateEvent in one
    getTransaction result: from the log messages, else from pump.fun's
    self-invoked event instruction (emit_cpi), re-encoded as a log line."""
    if not tx:
        return []
    out = []
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        if line.startswith(PROGRAM_DATA_PREFIX):
            ev = decode_log_events([line])
            if ev and ev[0][0] == "create" and ev[0][1].get("mint") == mint:
                out.append(line)
    if out:
        return out
    try:
        inner = instructions(tx, outer=False)
    except Exception:  # noqa: BLE001 - an unreadable layout adds nothing
        inner = []
    for program, _accounts, data in inner:
        if program != PUMP_PROGRAM_ID:
            continue
        try:
            raw = b58decode(data)
        except Exception:  # noqa: BLE001
            continue
        ev = decode_event(raw)
        if ev and ev[0] == "create" and ev[1].get("mint") == mint:
            out.append(PROGRAM_DATA_PREFIX + base64.b64encode(raw).decode())
    return out


async def missing_launches(redis: Redis, now: float) -> list[tuple[str, str]]:
    """(mint, create signature) announced by PumpPortal between GAP_MAX_AGE
    and GAP_MIN_AGE seconds ago that the stream has not recorded and that
    were not tried before."""
    out: list[tuple[str, str]] = []
    for mint in await redis.zrangebyscore(pumpportal_ws.NEW, now - GAP_MAX_AGE, now - GAP_MIN_AGE):
        if await redis.zscore(pump_stream.RECENT, mint) is not None or await redis.exists(pump_stream.meta_key(mint)):
            continue
        raw = await redis.get(pumpportal_ws.meta_key(mint))
        sig = (json.loads(raw) or {}).get("signature") if raw else None
        if not isinstance(sig, str) or not sig:
            continue
        if not await redis.set(f"{GAP_DONE}:{mint}", "1", nx=True, ex=GAP_DONE_TTL):
            continue  # tried already
        out.append((mint, sig))
        if len(out) >= GAP_MAX_PER_PASS:
            break
    return out


async def gap_fill(redis: Redis, rpc: Any, now: float | None = None) -> dict[str, int]:
    """Ingests the CreateEvent of every launch the stream missed. Returns
    counts (also accumulated in GAP_STATS)."""
    now = now or time.time()
    counts = {"checked": 0, "filled": 0, "no_create_event": 0, "errors": 0}
    from datetime import datetime, timezone

    for mint, sig in await missing_launches(redis, now):
        counts["checked"] += 1
        try:
            tx = await rpc.call("getTransaction", get_transaction_params(sig, "jsonParsed"), priority="normal")
        except TypeError:  # an RPC object without priorities
            tx = await rpc.call("getTransaction", get_transaction_params(sig, "jsonParsed"))
        except Exception as exc:  # noqa: BLE001 - one failed lookup never stops the pass
            counts["errors"] += 1
            log.warning("gapfill.lookup_failed", mint=mint, error=type(exc).__name__)
            continue
        lines = create_lines(tx, mint)
        if not lines:
            counts["no_create_event"] += 1
            continue
        await pump_stream.ingest_logs(redis, lines, sig, datetime.now(timezone.utc), from_stream=False)
        counts["filled"] += 1
    if counts["checked"]:
        pipe = redis.pipeline(transaction=False)
        for k, v in counts.items():
            if v:
                pipe.hincrby(GAP_STATS, k, v)
        await pipe.execute()
        log.info("gapfill.pass", **counts)
    return counts


async def stream_coverage(redis: Redis, now: float | None = None, window: int = WATCH_WINDOW) -> dict[str, Any]:
    """Share of the launches PumpPortal announced (older than its grace
    period, within `window`) that the stream itself delivered: a launch the
    gap fill had to read from chain counts as missed."""
    now = now or time.time()
    announced = await redis.zrangebyscore(pumpportal_ws.NEW, now - window, now - pumpportal_ws.COVERAGE_GRACE_SECONDS)
    missed = 0
    for mint in announced:
        if await redis.exists(f"{GAP_DONE}:{mint}") or (
                await redis.zscore(pump_stream.RECENT, mint) is None and not await redis.exists(pump_stream.meta_key(mint))):
            missed += 1
    n = len(announced)
    return {"announced": n, "delivered_by_stream": n - missed, "coverage": round((n - missed) / n, 4) if n else None}


async def stream_problem(redis: Redis, now: float | None = None) -> str | None:
    """Why the program-log stream looks broken right now, or None."""
    now = now or time.time()
    hb = await pump_stream.heartbeat(redis)
    if hb is not None and now - hb.timestamp() > STALL_SECONDS:
        return f"no pump.fun event received for {now - hb.timestamp():.0f}s"
    cov = await stream_coverage(redis, now)
    if cov["announced"] >= MIN_ANNOUNCED and cov["coverage"] is not None and cov["coverage"] < MIN_COVERAGE:
        return (f"the stream delivered {cov['delivered_by_stream']} of {cov['announced']} launches PumpPortal announced "
                f"in the last {WATCH_WINDOW // 60} min ({cov['coverage']:.0%})")
    return None
