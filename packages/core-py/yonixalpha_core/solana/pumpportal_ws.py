"""PumpPortal data WebSocket (wss://pumpportal.fun/api/data) — a second,
independent view of pump.fun next to the on-chain program-log stream.

The on-chain stream (pump_stream) stays the ONLY source of trading data
(curves, trades, fees): it is decoded from the program's own events. The
PumpPortal feed is used for what an independent source is good for:

- coverage: every token PumpPortal announces should also appear in the
  on-chain stream within seconds; `coverage()` measures the share that did,
  so a silently lagging RPC WebSocket shows up in provider health instead
  of as missing trades;
- migrations: an independent migration signal, cross-checked with ours;
- held tokens (only with PUMPPORTAL_API_KEY): trade events for the mints
  of open Pump.fun positions (subscribeTokenTrade is metered by PumpPortal
  per message and charged to the key's linked wallet), giving the exit
  side a freshness cross-check for exactly the tokens held.

Free subscriptions (subscribeNewToken, subscribeMigration) need no key.
The key is only appended to the connection URL, which is never logged.
Message formats follow PumpPortal's documentation (txType create / buy /
sell / migrate with mint, signature, traderPublicKey, solAmount, ...).
"""

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from yonixalpha_core.logging import get_logger
from yonixalpha_core.solana import pump_stream

log = get_logger("core.pumpportal_ws")

URL = "wss://pumpportal.fun/api/data"
PREFIX = "yx:pp"
HEARTBEAT = f"{PREFIX}:hb"
STATS = f"{PREFIX}:stats"
NEW = f"{PREFIX}:new"  # zset mint -> seen (unix s)
MIGRATED = f"{PREFIX}:migrated"
RETENTION = 2 * 3600
MAX_TRADES = 200
TRADES_TTL = 3 * 3600
WATCH_REFRESH_SECONDS = 30
COVERAGE_GRACE_SECONDS = 60


def meta_key(mint: str) -> str:
    return f"{PREFIX}:meta:{mint}"


def trades_key(mint: str) -> str:
    return f"{PREFIX}:trades:{mint}"


def connection_url(api_key: Any = None) -> str:
    key = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
    return f"{URL}?api-key={key}" if key else URL


async def ingest(redis: Redis, msg: dict[str, Any], now: float | None = None) -> str | None:
    """Stores one PumpPortal message. Returns its kind or None."""
    now = now or time.time()
    kind = msg.get("txType")
    mint = msg.get("mint")
    if not kind or not isinstance(mint, str) or not 32 <= len(mint) <= 44:
        return None
    pipe = redis.pipeline()
    pipe.set(HEARTBEAT, str(now), ex=3600)
    pipe.hincrby(STATS, kind, 1)
    if kind == "create":
        pipe.zadd(NEW, {mint: now})
        pipe.zremrangebyscore(NEW, 0, now - RETENTION)
        meta = {k: msg.get(k) for k in ("name", "symbol", "uri", "traderPublicKey", "initialBuy", "solAmount",
                                         "marketCapSol", "bondingCurveKey", "signature", "pool")}
        pipe.set(meta_key(mint), json.dumps({**meta, "seen": now}), ex=RETENTION)
    elif kind == "migrate":
        pipe.zadd(MIGRATED, {mint: now})
        pipe.zremrangebyscore(MIGRATED, 0, now - RETENTION)
    elif kind in ("buy", "sell"):
        row = {k: msg.get(k) for k in ("signature", "traderPublicKey", "solAmount", "tokenAmount", "marketCapSol", "pool")}
        pipe.lpush(trades_key(mint), json.dumps({**row, "side": kind, "seen": now}))
        pipe.ltrim(trades_key(mint), 0, MAX_TRADES - 1)
        pipe.expire(trades_key(mint), TRADES_TTL)
    else:
        return None
    await pipe.execute()
    return kind


async def heartbeat(redis: Redis) -> float | None:
    raw = await redis.get(HEARTBEAT)
    return float(raw) if raw else None


async def coverage(redis: Redis, now: float | None = None, window: int = 3600) -> dict[str, Any]:
    """Share of PumpPortal-announced tokens (older than the grace period,
    within `window`) that the on-chain stream also recorded."""
    now = now or time.time()
    announced = await redis.zrangebyscore(NEW, now - window, now - COVERAGE_GRACE_SECONDS)
    if not announced:
        return {"announced": 0, "seen_on_chain": 0, "coverage": None}
    seen = 0
    for mint in announced:
        if await redis.zscore(pump_stream.RECENT, mint) is not None or await redis.exists(pump_stream.meta_key(mint)):
            seen += 1
    return {"announced": len(announced), "seen_on_chain": seen, "coverage": round(seen / len(announced), 4)}


class PumpPortalFeed:
    """One connection; reconnects with backoff. `held_mints` returns the
    mints of open Pump.fun positions (used only when an API key is set)."""

    def __init__(self, redis: Redis, api_key: Any = None, held_mints: Callable[[], Awaitable[set[str]]] | None = None,
                 connect=None):
        self.redis, self.api_key, self.held_mints = redis, api_key, held_mints
        self._connect = connect
        self.watching: set[str] = set()

    def _has_key(self) -> bool:
        k = self.api_key
        return bool(k.get_secret_value() if hasattr(k, "get_secret_value") else k)

    async def _open(self):
        if self._connect is not None:
            return self._connect(connection_url(self.api_key))
        import websockets

        return websockets.connect(connection_url(self.api_key), ping_interval=20, ping_timeout=20, max_size=2**20)

    async def _sync_watch(self, ws) -> None:
        if not (self._has_key() and self.held_mints):
            return
        want = await self.held_mints()
        add, drop = sorted(want - self.watching), sorted(self.watching - want)
        if add:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
        if drop:
            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
        self.watching = set(want)

    async def session(self, stop_event: asyncio.Event) -> None:
        """One connected session until it drops or stop is requested."""
        async with await self._open() as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            self.watching = set()
            await self._sync_watch(ws)
            last_sync = time.monotonic()
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=WATCH_REFRESH_SECONDS)
                except asyncio.TimeoutError:
                    raw = None
                if raw is not None:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        if "message" in msg or "errors" in msg:
                            log.info("pumpportal.notice", notice=str(msg)[:200])
                        else:
                            await ingest(self.redis, msg)
                if time.monotonic() - last_sync >= WATCH_REFRESH_SECONDS:
                    await self._sync_watch(ws)
                    last_sync = time.monotonic()

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = 1.0
        while not stop_event.is_set():
            try:
                await self.session(stop_event)
                delay = 1.0
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport failure
                # str(exc) can contain the URL; only the type is logged so the key never is.
                log.warning("pumpportal.disconnected", error=type(exc).__name__, retry_in=delay)
                await self.redis.hincrby(STATS, "disconnects", 1)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)
