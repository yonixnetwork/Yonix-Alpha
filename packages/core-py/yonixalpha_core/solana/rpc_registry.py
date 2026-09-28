"""The effective Solana RPC / WebSocket endpoint list: providers added in the
dashboard (rpc_providers table, URLs encrypted) and the endpoints in .env,
ordered by priority (lowest first). Running services reload it on every
configuration revision (runtime_config.RuntimeConfigWatcher), so adding,
editing, reordering or disabling a provider needs no restart.

.env endpoints keep working as before (they are how the stack starts); the
dashboard can disable them or change their priority (platform_settings
"rpc_env_overrides"), but their URLs stay in .env (server-side secrets).
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, secretbox
from yonixalpha_core.db.models import PlatformSetting, RpcProvider
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.redact import redact_url

log = get_logger("core.rpc_registry")

ENV_OVERRIDES_KEY = "rpc_env_overrides"
FAILOVER_LOG = "yx:rpc:failovers"
HEALTH_PREFIX = "yx:rpc:health:"
# label, variable, default priority. Dashboard providers default to 150:
# after the .env primary, before the .env backups.
ENV_RPC = [("env:primary", "SOLANA_RPC_URL", 100), ("env:backup", "SOLANA_RPC_BACKUP_URL", 200),
           ("env:backup2", "SOLANA_RPC_BACKUP_URL_2", 300), ("env:backup3", "SOLANA_RPC_BACKUP_URL_3", 400)]
ENV_WS = [("env:ws", "SOLANA_WS_URL", 100), ("env:ws_backup", "SOLANA_WS_BACKUP_URL", 200)]
DEFAULT_PRIORITY = 150


async def env_overrides(session: AsyncSession) -> dict[str, dict[str, Any]]:
    row = await session.get(PlatformSetting, ENV_OVERRIDES_KEY)
    return dict(row.value) if row else {}


def _env_rows(settings: Any, slots: list[tuple[str, str, int]], overrides: dict) -> list[dict[str, Any]]:
    out = []
    for label, var, prio in slots:
        url = getattr(settings, var, None)
        if not url:
            continue
        o = overrides.get(label) or {}
        out.append({"label": label, "name": var, "url": url, "source": "env", "variable": var,
                    "priority": int(o.get("priority", prio)), "enabled": bool(o.get("enabled", True)),
                    "timeout": None, "rps": None})
    return out


async def providers(session: AsyncSession, settings: Any) -> list[dict[str, Any]]:
    """Every configured endpoint, enabled or not, decrypted (server-side
    only; never returned to the browser as-is)."""
    overrides = await env_overrides(session)
    rows = _env_rows(settings, ENV_RPC, overrides)
    for p in (await session.execute(select(RpcProvider).where(RpcProvider.chain == "solana"))).scalars():
        url = secretbox.decrypt(settings, p.rpc_url_enc)
        rows.append({"label": f"db:{p.name}", "name": p.name, "url": url, "source": "dashboard", "id": str(p.id),
                     "priority": p.priority, "enabled": p.enabled and url is not None,
                     "decrypt_failed": url is None, "timeout": _num(p.timeout_seconds), "rps": _num(p.rate_limit_rps),
                     "ws_url": secretbox.decrypt(settings, p.ws_url_enc) if p.ws_url_enc else None})
    return sorted(rows, key=lambda r: (r["priority"], r["source"] != "env", r["label"]))


def _num(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


async def effective_rpc(session: AsyncSession, settings: Any) -> list[dict[str, Any]]:
    return [r for r in await providers(session, settings) if r["enabled"] and r.get("url")]


async def effective_ws(session: AsyncSession, settings: Any) -> list[str]:
    overrides = await env_overrides(session)
    rows = [r for r in _env_rows(settings, ENV_WS, overrides) if r["enabled"]]
    for p in await providers(session, settings):
        if p["source"] == "dashboard" and p["enabled"] and p.get("ws_url"):
            rows.append({"url": p["ws_url"], "priority": p["priority"]})
    return [r["url"] for r in sorted(rows, key=lambda r: r["priority"])]


class WsUrls:
    """A url_provider for SolanaWsClient that follows the dashboard: each
    reconnect takes the next URL of the current list."""

    def __init__(self, urls: list[str]):
        self.urls = [u for u in urls if u]
        self._i = 0

    def __call__(self) -> str:
        url = self.urls[self._i % len(self.urls)]
        self._i += 1
        return url

    def replace(self, urls: list[str]) -> None:
        if urls:
            self.urls = list(urls)


def make_reloaders(service: str, rpc, settings: Any, session_factory, redis: Redis | None,
                   ws_urls: WsUrls | None = None) -> dict:
    """Reloaders for runtime_config.RuntimeConfigWatcher: re-reads the
    endpoint lists and swaps them into the running RpcManager / WS client."""

    async def on_failover(previous: str | None, current: str, reasons: str) -> None:
        entry = {"service": service, "from": previous, "to": current, "reasons": reasons,
                 "at": datetime.now(timezone.utc).isoformat()}
        if redis is not None:
            await redis.lpush(FAILOVER_LOG, json.dumps(entry))
            await redis.ltrim(FAILOVER_LOG, 0, 99)
            await events.publish(redis, "rpc.failover", entry, service)
        await alert_error("solana-rpc", "rpc.failover", f"{service}: {previous} → {current} ({reasons or 'recovered'})")

    async def reload_rpc() -> dict:
        async with session_factory() as session:
            specs = await effective_rpc(session, settings)
        rpc.replace_endpoints(specs)
        rpc.on_failover = on_failover
        return {"endpoints": [{"label": s["label"], "url": redact_url(s["url"]), "priority": s["priority"]} for s in specs]}

    out = {"rpc": reload_rpc}
    if ws_urls is not None:
        async def reload_ws() -> dict:
            async with session_factory() as session:
                urls = await effective_ws(session, settings)
            ws_urls.replace(urls)
            return {"ws_endpoints": [redact_url(u) for u in urls], "applies": "on the next reconnect"}
        out["ws"] = reload_ws
    return out


def rpc_status(rpc) -> dict:
    """For the watcher's acknowledgement: live endpoint health, no URLs."""
    snap = rpc.health_snapshot()
    methods = rpc.method_snapshot() if hasattr(rpc, "method_snapshot") else []
    return {"active": rpc.active_label, "endpoints": snap, "methods": methods}


# --- connection test ------------------------------------------------------------

CONNECTED, AUTH_FAILED, TIMEOUT, RATE_LIMITED, INVALID, UNAVAILABLE = (
    "CONNECTED", "AUTHENTICATION_FAILED", "TIMEOUT", "RATE_LIMITED", "INVALID_CONFIGURATION", "UNAVAILABLE")
_AUTH_WORDS = ("unauthorized", "api key", "api-key", "apikey", "forbidden", "invalid key", "not allowed", "auth")


def validate_url(url: str | None, schemes: tuple[str, ...] = ("https",)) -> str | None:
    """An error message, or None when `url` is usable."""
    from urllib.parse import urlsplit

    if not url or not isinstance(url, str):
        return "URL is required"
    if any(ch.isspace() for ch in url) or url[:1] in ("'", '"'):
        return "URL contains spaces or quotes — copy it again"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL cannot be parsed"
    if parts.scheme not in schemes:
        return f"URL must start with {' or '.join(s + '://' for s in schemes)}"
    if not parts.hostname or "." not in parts.hostname:
        return "URL has no valid host"
    return None


async def test_rpc(client, url: str | None, timeout: float = 10.0) -> dict[str, Any]:
    """One real JSON-RPC round trip (getSlot). Never returns the URL."""
    import time

    import httpx

    err = validate_url(url)
    now = datetime.now(timezone.utc).isoformat()
    if err:
        return {"status": INVALID, "detail": err, "latency_ms": None, "tested_at": now}
    started = time.monotonic()
    try:
        r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}, timeout=timeout)
    except httpx.TimeoutException:
        return {"status": TIMEOUT, "detail": f"no answer within {timeout:g}s", "latency_ms": None, "tested_at": now}
    except httpx.TransportError as exc:
        return {"status": UNAVAILABLE, "detail": f"connection error ({type(exc).__name__})", "latency_ms": None, "tested_at": now}
    ms = round((time.monotonic() - started) * 1000, 1)
    if r.status_code in (401, 403):
        return {"status": AUTH_FAILED, "detail": f"HTTP {r.status_code}", "latency_ms": ms, "tested_at": now}
    if r.status_code == 429:
        return {"status": RATE_LIMITED, "detail": "HTTP 429 Too Many Requests", "latency_ms": ms, "tested_at": now}
    if r.status_code >= 400:
        return {"status": UNAVAILABLE, "detail": f"HTTP {r.status_code}", "latency_ms": ms, "tested_at": now}
    try:
        body = r.json()
    except ValueError:
        return {"status": UNAVAILABLE, "detail": "response is not JSON-RPC", "latency_ms": ms, "tested_at": now}
    if isinstance(body, dict) and "error" in body:
        text = str(body["error"]).lower()
        status = AUTH_FAILED if any(w in text for w in _AUTH_WORDS) else RATE_LIMITED if "limit" in text else UNAVAILABLE
        return {"status": status, "detail": redact_url(str(body["error"]))[:200] if "://" in str(body["error"]) else str(body["error"])[:200],
                "latency_ms": ms, "tested_at": now}
    if not isinstance(body, dict) or not isinstance(body.get("result"), int):
        return {"status": UNAVAILABLE, "detail": "unexpected getSlot answer", "latency_ms": ms, "tested_at": now}
    return {"status": CONNECTED, "detail": f"slot {body['result']}", "latency_ms": ms, "tested_at": now}


# --- capability probe -------------------------------------------------------------

PROBE_METHODS = ("getLatestBlockhash", "getBalance", "getTokenAccountsByOwner", "getSignaturesForAddress",
                 "getTransaction", "getSignatureStatuses", "simulateTransaction", "sendTransaction")
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _probe_tx_b64() -> str:
    """An unsigned transaction with one ComputeBudget instruction and a
    random fee payer: simulating it (sigVerify off) shows whether the
    provider serves simulateTransaction; it can never be sent."""
    import base64

    from solders.compute_budget import set_compute_unit_limit
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

    msg = MessageV0.try_compile(Keypair().pubkey(), [set_compute_unit_limit(10_000)], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


async def probe_capabilities(client, url: str, owner: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """Read-only capability probe of one endpoint: which methods it serves
    and which transaction versions its getTransaction returns. Nothing is
    ever sent: sendTransaction is reported from real traffic only."""
    import re
    import time

    import httpx

    from yonixalpha_core.solana.rpc import MAX_SUPPORTED_TRANSACTION_VERSION, get_transaction_params

    owner = owner or "11111111111111111111111111111111"
    out: dict[str, Any] = {}
    versions: dict[str, int] = {}

    async def one(method: str, params: list) -> Any:
        started = time.monotonic()
        entry: dict[str, Any] = {"status": "ERROR", "latency_ms": None, "detail": None}
        out[method] = entry
        try:
            r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
        except httpx.TimeoutException:
            entry["detail"] = "timeout"
            return None
        except httpx.TransportError as exc:
            entry["detail"] = f"connection error ({type(exc).__name__})"
            return None
        entry["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        if r.status_code in (401, 403):
            entry.update(status="FORBIDDEN", detail=f"HTTP {r.status_code}")
            return None
        if r.status_code == 429:
            entry.update(status="RATE_LIMITED", detail="HTTP 429")
            return None
        try:
            body = r.json()
        except ValueError:
            entry["detail"] = f"HTTP {r.status_code}, not JSON-RPC"
            return None
        err = body.get("error") if isinstance(body, dict) else None
        if err:
            msg = str(err.get("message", err) if isinstance(err, dict) else err)
            code = err.get("code") if isinstance(err, dict) else None
            if code == -32601 or re.search(r"\bmethod\b.*\b(not found|does not exist|is not available|not supported)", msg, re.I):
                entry.update(status="UNSUPPORTED", detail=msg[:160])
            elif code == -32015:
                entry.update(status="SUPPORTED", detail=f"version above {MAX_SUPPORTED_TRANSACTION_VERSION}: {msg[:120]}")
            else:
                entry.update(status="ERROR", detail=msg[:160])
            return None
        entry["status"] = "SUPPORTED"
        return body.get("result")

    await one("getLatestBlockhash", [{"commitment": "confirmed"}])
    await one("getBalance", [owner, {"commitment": "confirmed"}])
    await one("getTokenAccountsByOwner", [owner, {"programId": _TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
    sigs = await one("getSignaturesForAddress", [_PUMP_PROGRAM, {"limit": 5, "commitment": "confirmed"}])
    sigs = sigs if isinstance(sigs, list) else []
    first = None
    for sig in [x.get("signature") for x in sigs if isinstance(x, dict)][:3]:
        tx = await one("getTransaction", get_transaction_params(sig))
        if isinstance(tx, dict):
            v = str(tx.get("version", "legacy"))
            versions[v] = versions.get(v, 0) + 1
        first = first or sig
    if first is None:
        out.setdefault("getTransaction", {"status": "NOT_PROBED", "latency_ms": None,
                                          "detail": "no recent signature to look up"})
    await one("getSignatureStatuses", [[first or "1" * 88], {"searchTransactionHistory": False}])
    try:
        await one("simulateTransaction", [_probe_tx_b64(), {"encoding": "base64", "sigVerify": False,
                                                             "replaceRecentBlockhash": True}])
    except Exception as exc:  # noqa: BLE001 - probe construction failure is reported, not raised
        out["simulateTransaction"] = {"status": "NOT_PROBED", "latency_ms": None, "detail": type(exc).__name__}
    out["sendTransaction"] = {"status": "NOT_PROBED", "latency_ms": None,
                              "detail": "never probed (it would broadcast); see live traffic"}
    return {"methods": out, "tx_versions_seen": versions, "max_version_declared": MAX_SUPPORTED_TRANSACTION_VERSION,
            "probed_at": datetime.now(timezone.utc).isoformat()}
