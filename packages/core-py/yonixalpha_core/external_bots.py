"""Adapter for the user's standalone bots' control APIs (CONTROL_API_CONTRACT
v1 of trading-command-center): GET /status, POST /close, GET/PUT /config,
each behind `Authorization: Bearer <that bot's CONTROL_API_TOKEN>`.

YonixAlpha runs these strategies natively; the adapter exists so that
standalone bots which are still deployed can be watched and stopped from
the same dashboard, and so both never trade the same strategy with real
money at once: while a configured bot's strategy is live elsewhere (it
reports a position, or its state cannot be read), YonixAlpha refuses LIVE
for that strategy (`conflict`).

Security: bot URLs and tokens come only from the server environment, are
never returned to the frontend, and every dashboard call to these bots
goes through authenticated YonixAlpha API routes. The bots' own control
APIs should listen on 127.0.0.1 or a private network only.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from redis.asyncio import Redis

CACHE_KEY = "yx:extbot:{}"
CACHE_TTL_SECONDS = 120
TIMEOUT = httpx.Timeout(4.0)


@dataclass(frozen=True)
class BotSpec:
    name: str
    label: str
    url_setting: str
    token_setting: str
    strategies: tuple[str, ...]  # YonixAlpha strategies/engines this bot also trades


BOTS = {b.name: b for b in [
    BotSpec("meta_muse", "Meta Muse — BTC/ETH Crossover", "META_MUSE_CONTROL_URL", "META_MUSE_TOKEN", ("meta_muse",)),
    BotSpec("goldvsbtc", "Gold vs BTC Dual Trend", "GOLDVSBTC_CONTROL_URL", "GOLDVSBTC_TOKEN", ("gold_btc_trend",)),
    BotSpec("meme_bot", "Solana Pump.fun Sniper (Meme-bot)", "MEME_BOT_CONTROL_URL", "MEME_BOT_TOKEN",
            ("solana_fresh", "solana_migration", "solana_momentum")),
    BotSpec("hyperliquid_grid", "Hyperliquid Grid Bot", "HYPERLIQUID_GRID_CONTROL_URL", "HYPERLIQUID_GRID_TOKEN",
            ("hyperliquid_grid",)),
]}


def _secret(v: Any) -> str:
    if v is None:
        return ""
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)


def configured(settings: Any, bot: BotSpec) -> bool:
    return bool(getattr(settings, bot.url_setting, None) and _secret(getattr(settings, bot.token_setting, None)))


class BotError(Exception):
    pass


class ExternalBotClient:
    def __init__(self, client: httpx.AsyncClient, settings: Any, bot: BotSpec):
        self.client, self.bot = client, bot
        self.base = (getattr(settings, bot.url_setting, None) or "").rstrip("/")
        self._token = _secret(getattr(settings, bot.token_setting, None))

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        if not (self.base and self._token):
            raise BotError(f"{self.bot.url_setting} / {self.bot.token_setting} not set")
        try:
            resp = await self.client.request(method, f"{self.base}{path}", json=body, timeout=TIMEOUT,
                                             headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise BotError(f"control API unreachable: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            raise BotError("control API rejected the token (401)")
        try:
            data = resp.json()
        except ValueError as exc:
            raise BotError(f"control API returned HTTP {resp.status_code} non-JSON") from exc
        if resp.status_code >= 400:
            raise BotError(f"control API HTTP {resp.status_code}: {str(data.get('detail') if isinstance(data, dict) else data)[:200]}")
        return data

    async def status(self) -> dict[str, Any]:
        return await self._call("GET", "/status")

    async def close(self) -> dict[str, Any]:
        return await self._call("POST", "/close")

    async def get_config(self) -> dict[str, Any]:
        return await self._call("GET", "/config")

    async def put_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PUT", "/config", patch)


async def poll_all(client: httpx.AsyncClient, settings: Any, redis: Redis | None,
                   now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Reads every configured bot's /status and caches it for `conflict`."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    for bot in BOTS.values():
        if not configured(settings, bot):
            out[bot.name] = {"configured": False, "state": "NOT CONFIGURED"}
            continue
        try:
            st = await ExternalBotClient(client, settings, bot).status()
            entry = {"configured": True, "reachable": True, "state": "CONNECTED", "running": bool(st.get("running")),
                     "paused": bool(st.get("paused")), "position": st.get("position"), "last_signal": st.get("last_signal"),
                     "last_error": st.get("last_error"), "uptime_seconds": st.get("uptime_seconds")}
        except BotError as exc:
            entry = {"configured": True, "reachable": False, "state": "UNAVAILABLE", "error": str(exc)[:200]}
        entry["checked_at"] = now.isoformat()
        out[bot.name] = entry
        if redis is not None:
            await redis.set(CACHE_KEY.format(bot.name), json.dumps(entry, default=str), ex=CACHE_TTL_SECONDS)
    return out


async def cached(redis: Redis | None, name: str) -> dict[str, Any] | None:
    if redis is None:
        return None
    raw = await redis.get(CACHE_KEY.format(name))
    return json.loads(raw) if raw else None


async def conflict(redis: Redis | None, settings: Any, strategy: str) -> str | None:
    """Why LIVE must not start for `strategy` because a standalone bot may
    be trading it, or None."""
    for bot in BOTS.values():
        if strategy not in bot.strategies or not configured(settings, bot):
            continue
        st = await cached(redis, bot.name)
        if st is None or not st.get("reachable"):
            return f"standalone bot {bot.name} is configured but its state is unknown (control API not reachable)"
        if st.get("position"):
            return f"standalone bot {bot.name} holds a position in this strategy"
        if st.get("running") and not st.get("paused"):
            return f"standalone bot {bot.name} is running this strategy"
    return None
