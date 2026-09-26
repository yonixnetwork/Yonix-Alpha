"""Settings center: one place that shows how every provider is configured
and tests each connection for real.

Secrets are never returned: each one is reported only as configured / not
configured. Non-secret values (testnet flags, public addresses, the host
part of an RPC URL) are shown as they are. Secrets live only in the
server's .env (root-readable, never in the database, never sent to the
browser); they are changed on the server with scripts/set-keys.sh.

Operational settings (risk thresholds, fresh-token observation, exits,
modes, live-execution parameters, strategy parameters) are edited through
their existing pages and endpoints; `sections` lists where each lives.
"""

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit
from yonixalpha_core import provider_tests
from yonixalpha_core.config import Settings
from yonixalpha_core.redact import redact_url

router = APIRouter(prefix="/settings", tags=["settings"])

TEST_COOLDOWN_SECONDS = 5
LAST_RESULT_TTL = 7 * 86400

# (group, provider test name or None, secrets, non-secret values shown as-is, URL values shown as scheme://host)
PROVIDERS = {
    "solana": ("Solana", ["solana_rpc", "solana_ws", "solana_rpc_backup"], ["HELIUS_API_KEY"], [],
               ["SOLANA_RPC_URL", "SOLANA_WS_URL", "SOLANA_RPC_BACKUP_URL", "SOLANA_WS_BACKUP_URL"]),
    "helius": ("Helius", ["helius"], ["HELIUS_API_KEY"], [], []),
    "pumpportal": ("Pump.fun / PumpPortal", ["pumpportal"], ["PUMPPORTAL_API_KEY"], [], []),
    "jupiter": ("Jupiter", ["jupiter"], ["JUPITER_API_KEY"], [], []),
    "wallet": ("Wallet", [], ["WALLET_PRIVATE_KEY"], ["WALLET_PUBLIC_KEY"], []),
    "binance": ("Binance", ["binance"], ["BINANCE_API_KEY", "BINANCE_API_SECRET"], ["BINANCE_TESTNET"], []),
    "bybit": ("Bybit", ["bybit"], ["BYBIT_API_KEY", "BYBIT_API_SECRET"], ["BYBIT_TESTNET"], []),
    "hyperliquid": ("Hyperliquid", ["hyperliquid"], ["HYPERLIQUID_API_WALLET_PRIVATE_KEY"],
                    ["HYPERLIQUID_ACCOUNT_ADDRESS", "HYPERLIQUID_TESTNET"], []),
    "mt5": ("MT5 / Forex bridge", ["mt5"], ["MT5_BRIDGE_TOKEN"], [], ["MT5_BRIDGE_URL"]),
    "telegram": ("Notifications (Telegram)", ["telegram"], ["TELEGRAM_BOT_TOKEN"], ["TELEGRAM_CHAT_ID"], []),
    "application": ("General", [], ["JWT_SECRET", "ADMIN_PASSWORD_HASH"],
                    ["APP_ENV", "LOG_LEVEL", "PUBLIC_DOMAIN", "TRADING_ENABLED", "LIVE_TRADING_ENABLED", "PAPER_TRADING"], []),
}

SECTIONS = [
    {"section": "Risk & thresholds", "where": "/dashboard/risk-settings",
     "covers": "per-engine risk limits, holder/creator concentration, flow, tax, slippage, price impact, ML minimum, "
               "fresh-token observation window and monitoring limits, curve liquidity minimum, exit intelligence and "
               "emergency-exit thresholds, TP R-multiples / fractions, trailing"},
    {"section": "Modes", "where": "/dashboard/settings", "covers": "global mode PAPER / MANUAL / LIVE"},
    {"section": "Strategies", "where": "/dashboard/strategies",
     "covers": "per-strategy mode and parameters (Meta Muse, Confluence Matrix, Hyperliquid Grid, Gold vs BTC, Solana), "
               "manual TP1-3 / stop / trailing overrides"},
    {"section": "Live execution", "where": "/dashboard/live",
     "covers": "Solana slippage, priority fees, SOL reserve; futures leverage cap, free-balance floor, fill deviation"},
    {"section": "Word filters & rules", "where": "/dashboard/rules", "covers": "blacklist, custom rules"},
    {"section": "ML", "where": "/dashboard/ml", "covers": "models, champion / challenger, drift"},
    {"section": "Secrets", "where": "server: scripts/set-keys.sh",
     "covers": "API keys, private keys, tokens, passwords — write-only, never displayed"},
]


def _value(settings: Settings, name: str):
    v = getattr(settings, name, None)
    return v.get_secret_value() if hasattr(v, "get_secret_value") else v


@router.get("/overview")
async def overview(settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
                   _: str = Depends(get_current_username)) -> dict:
    groups = []
    for key, (title, tests, secrets, plain, urls) in PROVIDERS.items():
        last = {}
        for t in tests:
            raw = await redis.hgetall(f"yx:provider_test:{t}")
            if raw:
                last[t] = raw
        groups.append({
            "key": key, "title": title, "tests": tests,
            "secrets": {n: ("configured" if _value(settings, n) else "not configured") for n in secrets},
            "values": {n: _value(settings, n) for n in plain},
            "endpoints": {n: redact_url(_value(settings, n)) or None for n in urls},
            "last_test": last,
        })
    return {"groups": groups, "sections": SECTIONS, "results": provider_tests.RESULTS,
            "secret_update": "on the server: scripts/set-keys.sh (hidden input, backup kept); secrets are never shown here"}


@router.post("/providers/{name}/test")
async def test_provider(name: str, request: Request, settings: Settings = Depends(get_settings),
                        redis: Redis = Depends(get_redis), db: AsyncSession = Depends(get_db),
                        username: str = Depends(get_current_username)) -> dict:
    """One real, read-only request to the provider with the server's current
    configuration; the classified result is returned and remembered."""
    if name not in provider_tests.PROVIDERS:
        raise HTTPException(404, f"unknown provider; one of {provider_tests.PROVIDERS}")
    if not await redis.set(f"yx:provider_test:cooldown:{name}", "1", nx=True, ex=TEST_COOLDOWN_SECONDS):
        raise HTTPException(429, f"tested less than {TEST_COOLDOWN_SECONDS}s ago")
    async with httpx.AsyncClient() as client:
        result = await provider_tests.test_provider(name, settings, client)
    await redis.hset(f"yx:provider_test:{name}", mapping={"status": result["status"], "detail": result["detail"],
                                                        "latency_ms": result["latency_ms"], "tested_at": int(time.time())})
    await redis.expire(f"yx:provider_test:{name}", LAST_RESULT_TTL)
    await audit(db, username, request, "provider.tested", {"provider": name, "status": result["status"]})
    await db.commit()
    return result
