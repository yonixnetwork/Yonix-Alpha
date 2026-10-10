"""Redis-backed state for the pump.fun event stream, shared by the writer
(engine-solana-discovery) and the readers (decision-engine, paper-trading).

Only decoded pump.fun events land here; the stream is the single ingestion
path, so a candidate's trade flow, curve reserves and fee rates all come
from the same on-chain events. Everything has a TTL and every list is
capped, so Redis memory stays bounded however busy pump.fun gets.

Times: trade/curve times are the on-chain event `timestamp` (unix seconds).
Freshness of the *stream* is the wall-clock time the last notification
arrived (`heartbeat`), which is what the gate's data checks use.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis

from yonixalpha_core.solana.codec import clean_text
from yonixalpha_core.solana.flow import Trade
from yonixalpha_core.solana.pumpfun import BondingCurveState, decode_log_events, is_sol_quoted, total_fee_bps

PREFIX = "yx:pump"
HEARTBEAT = f"{PREFIX}:hb"
STATS = f"{PREFIX}:stats"
RECENT = f"{PREFIX}:recent"  # zset mint -> create timestamp
PROMOTED = f"{PREFIX}:promoted"  # zset mint -> promotion timestamp
MIGRATED = f"{PREFIX}:migrated"  # zset mint -> migration timestamp
ACTIVE = f"{PREFIX}:active"  # zset mint -> last trade timestamp (momentum scan)
STREAM_STARTED = f"{PREFIX}:stream_started"  # first ingest: start of the creator-launch coverage
NAMES = f"{PREFIX}:name"  # + ":" + normalized name -> first mint that used it
NAME_TTL = 24 * 3600
CREATOR_TTL = 7 * 86400

META_TTL = 6 * 3600
CURVE_TTL = 6 * 3600
TRADES_TTL = 3 * 3600
MAX_TRADES_PER_MINT = 400
RECENT_RETENTION = 2 * 3600
PROMOTED_RETENTION = 24 * 3600


def meta_key(mint: str) -> str:
    return f"{PREFIX}:meta:{mint}"


def curve_key(mint: str) -> str:
    return f"{PREFIX}:curve:{mint}"


def trades_key(mint: str) -> str:
    return f"{PREFIX}:trades:{mint}"


def creator_key(creator: str) -> str:
    return f"{PREFIX}:creator:{creator}"


def normalize_name(value: str | None) -> str:
    """Case-, space- and punctuation-insensitive form used for duplicates."""
    return "".join(ch for ch in (value or "").casefold() if ch.isalnum())


def name_key(name: str | None) -> str | None:
    n = normalize_name(name)
    return f"{NAMES}:{n}" if n else None


async def duplicate_of(redis: Redis, mint: str, name: str | None) -> str | None:
    """The earlier mint (last 24 h) that launched with the same name, or
    None. Symbols are not compared: popular tickers are reused all the time."""
    key = name_key(name)
    if key is None:
        return None
    first = await redis.get(key)
    return first if first and first != mint else None


def _ts(fields: dict[str, Any]) -> int | None:
    ts = fields.get("timestamp")
    return int(ts) if isinstance(ts, int) and ts > 0 else None


async def ingest_logs(redis: Redis, logs: list[str], signature: str | None, received_at: datetime,
                      from_stream: bool = True) -> dict[str, int]:
    """Decodes every pump.fun event in one transaction's logs and writes it.
    Returns counts per event kind (plus "skipped_non_sol"), which the
    caller aggregates into STATS so an operator can see whether decoding
    works on the live stream at all."""
    counts: dict[str, int] = {}
    pipe = redis.pipeline(transaction=False)
    if from_stream:  # a gap-filled transaction (stream_guard) is not a sign of a live stream
        pipe.set(HEARTBEAT, received_at.isoformat())
        pipe.set(STREAM_STARTED, int(received_at.timestamp()), nx=True)
        pipe.hincrby(STATS, "notifications", 1)
    for kind, f in decode_log_events(logs):
        mint = f.get("mint")
        if not mint:
            continue
        if not is_sol_quoted(f):
            counts["skipped_non_sol"] = counts.get("skipped_non_sol", 0) + 1
            continue
        counts[kind] = counts.get(kind, 0) + 1
        ts = _ts(f)
        if kind == "create":
            meta = {
                "name": clean_text(f.get("name"), 128),
                "symbol": clean_text(f.get("symbol"), 32),
                "uri": clean_text(f.get("uri"), 512),
                "creator": f.get("creator") or f.get("user") or "",
                "bonding_curve": f.get("bonding_curve", ""),
                "token_program": f.get("token_program", ""),
                "created_at": ts or int(received_at.timestamp()),
                "signature": signature or "",
                "is_mayhem_mode": 1 if f.get("is_mayhem_mode") else 0,
                "initial_real_token_reserves": int(f.get("real_token_reserves") or 0),
                # Entry latency (entry_timing): when this process received the
                # create event, and whether the live stream or a gap fill did.
                "received_at": f"{received_at.timestamp():.3f}",
                "received_via": "stream" if from_stream else "gap_fill",
            }
            pipe.hset(meta_key(mint), mapping=meta)
            pipe.expire(meta_key(mint), META_TTL)
            pipe.zadd(RECENT, {mint: meta["created_at"]})
            if meta["creator"]:
                pipe.zadd(creator_key(meta["creator"]), {mint: meta["created_at"]})
                pipe.expire(creator_key(meta["creator"]), CREATOR_TTL)
            if name_key(meta["name"]):
                # The first launch to use a name keeps it for 24 h.
                pipe.set(name_key(meta["name"]), mint, nx=True, ex=NAME_TTL)
        elif kind == "trade":
            if ts is None or "virtual_sol_reserves" not in f:
                continue
            row = [ts, f["user"], 1 if f["is_buy"] else 0, f["sol_amount"], f["token_amount"],
                   f["virtual_sol_reserves"], f["virtual_token_reserves"]]
            pipe.rpush(trades_key(mint), json.dumps(row, separators=(",", ":")))
            pipe.ltrim(trades_key(mint), -MAX_TRADES_PER_MINT, -1)
            pipe.expire(trades_key(mint), TRADES_TTL)
            curve = {
                "vsol": f["virtual_sol_reserves"],
                "vtok": f["virtual_token_reserves"],
                "updated_at": ts,
            }
            if "real_sol_reserves" in f:
                curve["rsol"] = f["real_sol_reserves"]
                curve["rtok"] = f["real_token_reserves"]
            # A reported total of 0 is treated as unknown, never as free: no
            # SOL curve fee tier charges nothing, and a 0 would drop the fee
            # from every cost estimate. The last real rate is kept; with none,
            # the curve can't be simulated and the gate blocks.
            fee = total_fee_bps(f)
            if fee:
                curve["fee_bps"] = fee
            pipe.hset(curve_key(mint), mapping=curve)
            pipe.expire(curve_key(mint), CURVE_TTL)
            pipe.zadd(ACTIVE, {mint: ts})
        elif kind == "complete":
            pipe.hset(curve_key(mint), mapping={"complete": 1, "completed_at": ts or int(received_at.timestamp())})
            pipe.expire(curve_key(mint), CURVE_TTL)
        elif kind == "migration":
            pipe.hset(curve_key(mint), mapping={"complete": 1, "pool": f.get("pool", ""), "migrated_at": ts or 0})
            pipe.expire(curve_key(mint), CURVE_TTL)
            pipe.zadd(MIGRATED, {mint: ts or int(received_at.timestamp())})
    for kind, n in counts.items():
        pipe.hincrby(STATS, kind if from_stream else f"{kind}_gap_filled", n)
    await pipe.execute()
    return counts


async def heartbeat(redis: Redis) -> datetime | None:
    raw = await redis.get(HEARTBEAT)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def stats(redis: Redis) -> dict[str, int]:
    return {k: int(v) for k, v in (await redis.hgetall(STATS)).items()}


async def load_meta(redis: Redis, mint: str) -> dict[str, str] | None:
    meta = await redis.hgetall(meta_key(mint))
    if not meta:
        return None
    # Entries written before names were cleaned at ingestion may still
    # carry NUL / control characters.
    for k in ("name", "symbol", "uri"):
        if k in meta:
            meta[k] = clean_text(meta[k])
    return meta


@dataclass
class StreamCurve:
    vsol: int
    vtok: int
    rsol: int | None
    rtok: int | None
    fee_bps: int | None
    updated_at: datetime
    complete: bool
    pool: str | None
    migrated_at: datetime | None = None

    def as_state(self) -> BondingCurveState | None:
        if self.rsol is None or self.rtok is None:
            return None
        return BondingCurveState(self.vtok, self.vsol, self.rtok, self.rsol, 0, self.complete)


async def load_curve(redis: Redis, mint: str) -> StreamCurve | None:
    h = await redis.hgetall(curve_key(mint))
    if not h or "vsol" not in h:
        return None
    return StreamCurve(
        vsol=int(h["vsol"]),
        vtok=int(h["vtok"]),
        rsol=int(h["rsol"]) if "rsol" in h else None,
        rtok=int(h["rtok"]) if "rtok" in h else None,
        fee_bps=int(h["fee_bps"]) if "fee_bps" in h else None,
        updated_at=datetime.fromtimestamp(int(h["updated_at"]), tz=timezone.utc),
        complete=h.get("complete") == "1",
        pool=h.get("pool") or None,
        migrated_at=datetime.fromtimestamp(int(h["migrated_at"]), tz=timezone.utc) if h.get("migrated_at") not in (None, "", "0") else None,
    )


async def load_trades(redis: Redis, mint: str) -> list[Trade]:
    out = []
    for raw in await redis.lrange(trades_key(mint), 0, -1):
        try:
            ts, trader, buy, sol, tok, vsol, vtok = json.loads(raw)
        except (ValueError, TypeError):
            continue
        out.append(Trade(datetime.fromtimestamp(ts, tz=timezone.utc), trader, bool(buy), int(sol), int(tok), int(vsol), int(vtok)))
    return out


async def recent_unpromoted(redis: Redis, now: datetime, max_age_seconds: int, limit: int = 500) -> list[tuple[str, int]]:
    """(mint, created_ts) created within `max_age_seconds`, not yet promoted."""
    lo = int(now.timestamp()) - max_age_seconds
    rows = await redis.zrangebyscore(RECENT, lo, "+inf", start=0, num=limit, withscores=True)
    if not rows:
        return []
    pipe = redis.pipeline(transaction=False)
    for mint, _ in rows:
        pipe.zscore(PROMOTED, mint)
    promoted = await pipe.execute()
    return [(mint, int(score)) for (mint, score), p in zip(rows, promoted) if p is None]


OBS_FINAL = f"{PREFIX}:obs_final"  # zset mint -> time the observation outcome became final
OBS_SINCE = f"{PREFIX}:obs_since"  # hash mint -> end of the first window (continued monitoring)
OBS_LIVE = f"{PREFIX}:obs_live"  # zset mint -> created ts, tokens currently observed / monitored


def obs_report_key(mint: str) -> str:
    return f"{PREFIX}:obs:{mint}"


async def open_for_observation(redis: Redis, now: datetime, max_age_seconds: int, limit: int = 2000) -> list[tuple[str, int]]:
    """(mint, created_ts) launched within `max_age_seconds`, newest first,
    whose observation has no final outcome yet and that were not promoted.
    Newest first: when the stream is busier than one run can process, the
    launches still inside their observation window come first."""
    lo = int(now.timestamp()) - max_age_seconds
    rows = await redis.zrevrangebyscore(RECENT, "+inf", lo, start=0, num=limit, withscores=True)
    if not rows:
        return []
    pipe = redis.pipeline(transaction=False)
    for mint, _ in rows:
        pipe.zscore(PROMOTED, mint)
        pipe.zscore(OBS_FINAL, mint)
    flags = await pipe.execute()
    return [(mint, int(score)) for i, (mint, score) in enumerate(rows) if flags[2 * i] is None and flags[2 * i + 1] is None]


async def mark_promoted(redis: Redis, mint: str, now: datetime) -> bool:
    """True if this call promoted the mint (NX: concurrent callers agree)."""
    return bool(await redis.zadd(PROMOTED, {mint: int(now.timestamp())}, nx=True))


async def migrated_since(redis: Redis, since_ts: int) -> list[tuple[str, int]]:
    rows = await redis.zrangebyscore(MIGRATED, since_ts, "+inf", withscores=True)
    return [(m, int(s)) for m, s in rows]


async def creator_launches(redis: Redis, creator: str | None, now: datetime, window_seconds: int = 86400) -> int | None:
    """Launches by `creator` observed by this stream in the window. None if
    the creator is unknown; a stream that just started undercounts."""
    if not creator:
        return None
    t = int(now.timestamp())
    return int(await redis.zcount(creator_key(creator), t - window_seconds, t))


async def active_mints(redis: Redis, now: datetime, within_seconds: int, limit: int = 500) -> list[str]:
    lo = int(now.timestamp()) - within_seconds
    return list(await redis.zrevrangebyscore(ACTIVE, "+inf", lo, start=0, num=limit))


async def prune(redis: Redis, now: datetime) -> None:
    t = int(now.timestamp())
    pipe = redis.pipeline(transaction=False)
    pipe.zremrangebyscore(ACTIVE, "-inf", t - TRADES_TTL)
    pipe.zremrangebyscore(RECENT, "-inf", t - RECENT_RETENTION)
    pipe.zremrangebyscore(PROMOTED, "-inf", t - PROMOTED_RETENTION)
    pipe.zremrangebyscore(MIGRATED, "-inf", t - PROMOTED_RETENTION)
    pipe.zremrangebyscore(OBS_FINAL, "-inf", t - PROMOTED_RETENTION)
    pipe.zremrangebyscore(OBS_LIVE, "-inf", t - RECENT_RETENTION)
    await pipe.execute()
