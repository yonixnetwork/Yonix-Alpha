"""Stream-vs-logs cross-check of EVM launch detection (master §68).

Two detection methods see the same launchpad transactions:
  logs     the launchpad's event logs (data-evm discovery, the primary
           source: what entries and copy trades are decided on);
  streams  the Robinhood sequencer feed and the BSC pending-transaction
           stream (chains.evm.streams), which record every transaction to a
           launchpad contract for 15 minutes.

Both directions are counted per hour, in Redis:
  logs -> stream   each launch and trade the log scan stores: did a stream
                   see its transaction first, and how much earlier?
                   (coverage and lead of the stream)
  stream -> logs   each launchpad transaction a stream saw, once GRACE has
                   passed: is it in the logs? A transaction not in the logs
                   reverted, emitted no decoded event (an approval, a
                   failed call), or was missed by the log scan. The first
                   two are normal; a rising share is the signal to look at
                   the log scan (RPC / Data Providers shows its gaps).

Measurement only: nothing here changes what is traded.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from typing import Any, Iterable

GRACE = timedelta(minutes=5)  # a stream-seen transaction gets this long to appear in the logs
LOGGED_TTL = 3600
CHECKED_TTL = 1800
BUCKET_TTL = 8 * 86400
LEAD_SAMPLES = 500
SCAN_BATCH = 500
KINDS = ("launch", "trade")
PLURAL = {"launch": "launches", "trade": "trades"}


def _bucket(chain: str, at: datetime) -> str:
    return f"yx:evm:xcheck:{chain}:{at:%Y%m%d%H}"


async def note_logged(redis, chain: str, items: Iterable[tuple[str, str | None]], now: datetime) -> dict[str, int]:
    """Called after the live log scan stored launches and trades: items are
    (kind, tx hash). Each transaction is counted once per kind (a re-scan
    of the same range adds nothing)."""
    if redis is None:
        return {}
    seen_items: list[tuple[str, str]] = []
    dedup: set[tuple[str, str]] = set()
    for kind, h in items:
        if h and kind in KINDS and (kind, h.lower()) not in dedup:
            dedup.add((kind, h.lower()))
            seen_items.append((kind, h.lower()))
    if not seen_items:
        return {}
    pipe = redis.pipeline()
    for kind, h in seen_items:
        pipe.set(f"yx:evm:logged:{chain}:{kind}:{h}", "1", ex=LOGGED_TTL, nx=True)
        pipe.set(f"yx:evm:logged:{chain}:{h}", "1", ex=LOGGED_TTL)
        pipe.get(f"yx:evm:seen:{chain}:{h}")
    res = await pipe.execute()
    counts = {f"{k}_{x}": 0 for k in KINDS for x in ("logged", "seen_first")}
    leads: dict[str, list[float]] = {k: [] for k in KINDS}
    for i, (kind, _h) in enumerate(seen_items):
        new, raw = res[3 * i], res[3 * i + 2]
        if not new:
            continue
        counts[f"{kind}_logged"] += 1
        rec = _json(raw)
        if rec and rec.get("seen_at"):
            counts[f"{kind}_seen_first"] += 1
            try:
                leads[kind].append(round((now - datetime.fromisoformat(rec["seen_at"])).total_seconds(), 3))
            except ValueError:
                pass
    key = _bucket(chain, now)
    pipe = redis.pipeline()
    for field, n in counts.items():
        if n:
            pipe.hincrby(key, field, n)
    for kind, xs in leads.items():
        if xs:
            pipe.rpush(f"{key}:lead:{kind}", *xs)
            pipe.ltrim(f"{key}:lead:{kind}", -LEAD_SAMPLES, -1)
            pipe.expire(f"{key}:lead:{kind}", BUCKET_TTL)
    pipe.expire(key, BUCKET_TTL)
    await pipe.execute()
    return counts


def _json(raw) -> dict[str, Any] | None:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


async def sweep(redis, chain: str, now: datetime) -> dict[str, int]:
    """Each launchpad transaction a stream saw, GRACE after it was seen and
    once only: in the logs or not (per stream source)."""
    out = {"checked": 0, "in_logs": 0, "not_in_logs": 0}
    if redis is None:
        return out
    keys: list[str] = []
    async for k in redis.scan_iter(match=f"yx:evm:seen:{chain}:*", count=SCAN_BATCH):
        keys.append(k.decode() if isinstance(k, bytes) else k)
    by_source: dict[str, dict[str, int]] = {}
    for i in range(0, len(keys), SCAN_BATCH):
        part = keys[i:i + SCAN_BATCH]
        recs = await redis.mget(part)
        due: list[tuple[str, dict[str, Any]]] = []
        for k, raw in zip(part, recs):
            rec = _json(raw)
            if not rec or "launchpad" not in (rec.get("match") or []) or not rec.get("seen_at"):
                continue
            try:
                if now - datetime.fromisoformat(rec["seen_at"]) < GRACE:
                    continue
            except ValueError:
                continue
            due.append((k.rsplit(":", 1)[-1], rec))
        if not due:
            continue
        pipe = redis.pipeline()
        for h, _ in due:
            pipe.set(f"yx:evm:xchecked:{chain}:{h}", "1", ex=CHECKED_TTL, nx=True)
            pipe.exists(f"yx:evm:logged:{chain}:{h}")
        res = await pipe.execute()
        for j, (_h, rec) in enumerate(due):
            if not res[2 * j]:
                continue  # counted by an earlier sweep
            src = by_source.setdefault(rec.get("source") or "stream", {"checked": 0, "in_logs": 0, "not_in_logs": 0})
            hit = "in_logs" if res[2 * j + 1] else "not_in_logs"
            for d in (out, src):
                d["checked"] += 1
                d[hit] += 1
    if out["checked"]:
        key = _bucket(chain, now)
        pipe = redis.pipeline()
        for src, d in by_source.items():
            for f, n in d.items():
                if n:
                    pipe.hincrby(key, f"stream_{f}", n)
                    pipe.hincrby(key, f"stream_{f}:{src}", n)
        pipe.expire(key, BUCKET_TTL)
        await pipe.execute()
    return out


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


async def report(redis, chain: str, now: datetime, hours: int = 24) -> dict[str, Any]:
    """Per hour (newest first) and totals over `hours`. Rates are None (shown
    NOT AVAILABLE) when nothing was counted, never 0 %."""
    rows: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    leads: dict[str, list[float]] = {k: [] for k in KINDS}
    for h in range(hours):
        at = now - timedelta(hours=h)
        key = _bucket(chain, at)
        raw = await redis.hgetall(key) if redis is not None else {}
        d = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in (raw or {}).items()}
        if d:
            rows.append({"hour": f"{at:%Y-%m-%d %H}:00", **d})
            for k, v in d.items():
                totals[k] = totals.get(k, 0) + v
        for kind in KINDS:
            xs = await redis.lrange(f"{key}:lead:{kind}", 0, -1) if redis is not None else []
            leads[kind].extend(float(x) for x in xs or [])
    t = totals.get
    out: dict[str, Any] = {"window_hours": hours, "hours": rows, "totals": totals}
    for kind in KINDS:
        xs = sorted(leads[kind])
        out[f"{PLURAL[kind]}_seen_first_by_stream"] = {
            "logged": t(f"{kind}_logged", 0), "seen_first": t(f"{kind}_seen_first", 0),
            "rate": _rate(t(f"{kind}_seen_first", 0), t(f"{kind}_logged", 0)),
            "lead_s_median": round(statistics.median(xs), 3) if xs else None,
            "lead_s_p95": xs[int(len(xs) * 0.95) - 1] if len(xs) >= 20 else None}
    out["stream_txs_in_logs"] = {
        "checked": t("stream_checked", 0), "in_logs": t("stream_in_logs", 0), "not_in_logs": t("stream_not_in_logs", 0),
        "rate": _rate(t("stream_in_logs", 0), t("stream_checked", 0)),
        "by_source": {src: {f: t(f"stream_{f}:{src}", 0) for f in ("checked", "in_logs", "not_in_logs")}
                      for src in sorted({k.split(":", 1)[1] for k in totals if k.startswith("stream_checked:")})}}
    out["note"] = ("logs -> stream: share of launches / trades the log scan stored whose transaction a stream had "
                   "already seen, and how much earlier (lead). stream -> logs: launchpad transactions a stream saw, "
                   "checked 5 minutes later; one not in the logs reverted, emitted no event, or was missed by the "
                   "log scan. Measurement only: nothing here changes what is traded.")
    return out
