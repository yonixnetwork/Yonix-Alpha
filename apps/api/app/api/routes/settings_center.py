"""Settings center: one place that shows how every provider is configured
and tests each connection for real.

Secrets are never returned: each one is reported only as configured / not
configured. Non-secret values (testnet flags, public addresses, the host
part of an RPC URL) are shown as they are. Secrets live only in the
server's .env (root-readable, never in the database, never sent to the
browser). Provider API keys and URLs can be replaced from the dashboard
(write-only, admin password required): the new values are queued for the
host helper that writes .env (see yonixalpha_core.env_updates). Wallet
keys, passwords and the trading locks are changed on the server only, with
scripts/set-keys.sh.

Operational settings (risk thresholds, fresh-token observation, exits,
modes, live-execution parameters, strategy parameters) are edited through
their existing pages and endpoints; `sections` lists where each lives.
"""

import json
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import PASSWORD_MAX_FAILURES, audit, require_password  # noqa: F401 (limit shared with the live smoke test)
from yonixalpha_core import env_updates, provider_tests
from yonixalpha_core.config import Settings
from yonixalpha_core.redact import redact_url

router = APIRouter(prefix="/settings", tags=["settings"])

TEST_COOLDOWN_SECONDS = 5
LAST_RESULT_TTL = 7 * 86400

# (group, provider test name or None, secrets, non-secret values shown as-is, URL values shown as scheme://host)
PROVIDERS = {
    "bsc": ("BSC (BNB Smart Chain)", ["bsc_rpc"], [], [], ["BSC_RPC_URLS"]),
    "robinhood": ("Robinhood Chain", ["robinhood_rpc"], [], [], ["ROBINHOOD_RPC_URLS"]),
    "honeypot": ("Honeypot.is (BSC safety enrichment)", ["honeypot_is"], [], [], []),
    "explorers": ("Block explorers (launch-coordination funding checks)", ["etherscan", "robinhood_explorer"],
                  ["ETHERSCAN_API_KEY"], [], []),
    "solana": ("Solana", ["solana_rpc", "solana_ws", "solana_rpc_backup", "solana_rpc_backup_2", "solana_rpc_backup_3"],
               ["HELIUS_API_KEY"], [],
               ["SOLANA_RPC_URL", "SOLANA_WS_URL", "SOLANA_RPC_BACKUP_URL", "SOLANA_RPC_BACKUP_URL_2", "SOLANA_RPC_BACKUP_URL_3",
                "SOLANA_WS_BACKUP_URL"]),
    "helius": ("Helius", ["helius"], ["HELIUS_API_KEY"], [], []),
    "pumpportal": ("Pump.fun / PumpPortal", ["pumpportal"], ["PUMPPORTAL_API_KEY"], [], []),
    "jupiter": ("Jupiter", ["jupiter"], ["JUPITER_API_KEY"], [], []),
    "wallet": ("Wallet (Solana)", [], ["WALLET_PRIVATE_KEY"], ["WALLET_PUBLIC_KEY"], []),
    "evm_wallet": ("Wallet (EVM: BSC + Robinhood Chain)", [], ["EVM_WALLET_PRIVATE_KEY"], ["EVM_WALLET_ADDRESS"], []),
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
     "covers": "per-strategy mode and parameters (Pump.fun fresh, migrated, momentum), "
               "manual TP1-3 / stop / trailing overrides"},
    {"section": "Live execution", "where": "/dashboard/live",
     "covers": "Solana slippage, priority fees, SOL reserve"},
    {"section": "Word filters & rules", "where": "/dashboard/rules", "covers": "blacklist, custom rules"},
    {"section": "ML", "where": "/dashboard/ml", "covers": "models, champion / challenger, drift"},
    {"section": "Provider API keys", "where": "/dashboard/settings",
     "covers": "Helius, Solana RPC/WS URLs, BSC / Robinhood Chain RPC URLs, Jupiter, PumpPortal, Telegram — "
               "write-only, never displayed; applied to .env by the server helper"},
    {"section": "Server-only secrets", "where": "server: scripts/set-keys.sh",
     "covers": "wallet private keys, admin password, JWT/DB/Redis secrets, testnet flags, trading locks"},
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
            "endpoints": {n: ", ".join(redact_url(u.strip()) for u in str(_value(settings, n)).split(",") if u.strip())
                          if _value(settings, n) else None for n in urls},
            "last_test": last,
        })
    return {"groups": groups, "sections": SECTIONS, "results": provider_tests.RESULTS,
            "secret_update": "provider keys: Settings -> Change provider API keys; server-only secrets: scripts/set-keys.sh"}


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


# --- provider key updates -----------------------------------------------------

# The spool shared with the host helper (docker-compose.prod.yml mounts it;
# scripts/install-env-updater.sh makes it writable for this container only).
ENV_SPOOL = os.environ.get("ENV_SPOOL_DIR", "/app/runtime/env-requests")
KEY_UPDATE_COOLDOWN_SECONDS = 10


def updater_installed() -> bool:
    return os.path.isdir(ENV_SPOOL) and os.access(ENV_SPOOL, os.W_OK)


class KeyUpdate(BaseModel):
    updates: dict[str, str] = Field(description="key -> new value; an empty value clears the key")
    password: str = Field(min_length=1, max_length=256)


@router.get("/keys")
async def key_status(settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """Which provider keys are set. Secrets as configured / not configured
    only; URLs as scheme://host; plain values (chat id, address) as is."""
    keys = []
    for key, kind in env_updates.EDITABLE_KEYS.items():
        v = _value(settings, key)
        shown = None if kind == "secret" else (redact_url(v) if kind == "url" else v) or None
        keys.append({"key": key, "kind": kind, "configured": bool(v), "value": shown})
    return {"installed": updater_installed(), "keys": keys, "server_only": list(env_updates.SERVER_ONLY),
            "install": "on the server, once: sudo scripts/install-env-updater.sh"}


@router.post("/keys")
async def queue_key_update(body: KeyUpdate, request: Request, db: AsyncSession = Depends(get_db),
                           redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Queues new provider key values for the server helper, which writes
    them into .env and restarts the affected services. Needs the admin
    password again; values are never logged, stored elsewhere or returned."""
    await require_password(db, redis, username, body.password, request, "provider_keys", {"keys": sorted(body.updates)})
    errors = env_updates.validate_all(body.updates)
    if errors:
        raise HTTPException(422, {"errors": errors})
    if not updater_installed():
        raise HTTPException(503, "the key updater is not installed on this server — run once: "
                                 "sudo scripts/install-env-updater.sh")
    if not await redis.set(f"yx:provider_keys:cooldown:{username}", "1", nx=True, ex=KEY_UPDATE_COOLDOWN_SECONDS):
        raise HTTPException(429, f"another change was queued less than {KEY_UPDATE_COOLDOWN_SECONDS}s ago")
    rid, req = env_updates.new_request(body.updates, username)
    env_updates.write_private(env_updates.request_path(ENV_SPOOL, rid), req)
    await audit(db, username, request, "provider_keys.update_queued", {"request_id": rid, "keys": sorted(body.updates)})
    await db.commit()
    return {"request_id": rid, "keys": sorted(body.updates), "status": "QUEUED"}


@router.get("/keys/requests/{rid}")
async def key_update_result(rid: str, _: str = Depends(get_current_username)) -> dict:
    try:
        result_file = env_updates.result_path(ENV_SPOOL, rid)
        request_file = env_updates.request_path(ENV_SPOOL, rid)
    except ValueError as exc:
        raise HTTPException(404, "unknown request") from exc
    if os.path.exists(result_file):
        with open(result_file) as f:
            return json.load(f)
    if os.path.exists(request_file):
        return {"id": rid, "status": "QUEUED"}
    return {"id": rid, "status": "UNKNOWN"}
