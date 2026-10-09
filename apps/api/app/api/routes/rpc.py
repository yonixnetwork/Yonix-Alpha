"""RPC & data providers: add, edit, test, reorder and disable RPC endpoints
for Solana (RPC + WebSocket), BSC and Robinhood Chain from the dashboard.
Services reload the list on the configuration revision this write bumps
(runtime_config); no restart.

URLs (which embed API keys) are encrypted at rest and only ever returned as
scheme://host. Adding a provider or changing a URL needs the admin password:
an RPC feeds every safety check, so pointing it somewhere else is as
sensitive as changing a key."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable, require_password
from yonixalpha_core import provider_roles, runtime_config, secretbox, system_profile
from yonixalpha_core.chains.evm import rpc_registry as evm_registry
from yonixalpha_core.chains.evm import streams as evm_streams
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import PlatformSetting, RpcProvider
from yonixalpha_core.redact import redact_url
from yonixalpha_core.solana import rpc_registry

router = APIRouter(prefix="/rpc", tags=["rpc"])

PROVIDER_TYPES = ["helius", "alchemy", "chainstack", "quicknode", "triton", "ankr", "blockdaemon", "drpc",
                  "validation_cloud", "robinhood", "public", "custom"]
RECENT = timedelta(minutes=5)


CHAIN_PATTERN = "^(solana|bsc|robinhood)$"


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9 _.\-]+$")
    chain: str = Field("solana", pattern=CHAIN_PATTERN)
    provider_type: str = "custom"
    rpc_url: str = Field(max_length=1024)
    ws_url: str | None = Field(None, max_length=1024)
    enabled: bool = True
    priority: int = Field(rpc_registry.DEFAULT_PRIORITY, ge=1, le=9999)
    timeout_seconds: Decimal = Field(Decimal("10"), ge=Decimal("1"), le=Decimal("60"))
    rate_limit_rps: Decimal | None = Field(None, gt=0, le=Decimal("10000"))
    notes: str | None = Field(None, max_length=500)
    roles: list[str] = Field(default_factory=list)
    plan: str | None = Field(None, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ProviderPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9 _.\-]+$")
    provider_type: str | None = None
    rpc_url: str | None = Field(None, max_length=1024)
    ws_url: str | None = Field(None, max_length=1024)  # "" clears it
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=9999)
    timeout_seconds: Decimal | None = Field(None, ge=Decimal("1"), le=Decimal("60"))
    rate_limit_rps: Decimal | None = Field(None, ge=0, le=Decimal("10000"))  # 0 clears it
    notes: str | None = Field(None, max_length=500)
    roles: list[str] | None = None
    plan: str | None = Field(None, max_length=64)  # "" clears it
    password: str | None = Field(None, max_length=256)


class EnvPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=9999)
    roles: list[str] | None = None
    plan: str | None = Field(None, max_length=64)


def _roles(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    roles, errors = provider_roles.parse_roles(v)
    if errors:
        raise HTTPException(422, {"errors": errors})
    return list(roles)


def _check_type(t: str | None) -> None:
    if t is not None and t not in PROVIDER_TYPES:
        raise HTTPException(422, f"provider_type must be one of {PROVIDER_TYPES}")


def _check_urls(rpc_url: str | None, ws_url: str | None) -> None:
    errors = []
    if rpc_url is not None and (e := rpc_registry.validate_url(rpc_url)):
        errors.append(f"RPC URL: {e}")
    if ws_url and (e := rpc_registry.validate_url(ws_url, ("wss",))):
        errors.append(f"WebSocket URL: {e}")
    if errors:
        raise HTTPException(422, {"errors": errors})


def _parse_ts(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def _health(label: str, acks: dict, last_test: dict | None, now: datetime) -> dict:
    """CONFIGURED / CONNECTED / HEALTHY / ACTIVE from what services actually
    saw on real requests, never from the URL merely existing."""
    per_service, connected, healthy_votes, active_in = {}, False, [], []
    for service, ack in acks.items():
        for e in ((ack.get("status") or {}).get("rpc") or {}).get("endpoints") or []:
            if e.get("label") != label:
                continue
            per_service[service] = e
            ok_at = _parse_ts(e.get("last_success_at"))
            recent_ok = ok_at is not None and now - ok_at <= RECENT
            connected = connected or recent_ok
            if e.get("disabled") or e.get("rate_limited") or e.get("auth_failed"):
                healthy_votes.append(False)
            elif recent_ok:
                healthy_votes.append(True)
            if e.get("active"):
                active_in.append(service)
    tested_ok = bool(last_test and last_test.get("status") == rpc_registry.CONNECTED
                     and (_parse_ts(last_test.get("tested_at")) or now - RECENT * 3) >= now - RECENT * 2)
    connected = connected or tested_ok
    if healthy_votes:
        healthy = "YES" if all(healthy_votes) else "DEGRADED" if any(healthy_votes) else "NO"
    else:
        healthy = "YES" if tested_ok else "UNKNOWN"
    totals = {k: sum((e.get(k) or 0) for e in per_service.values())
              for k in ("successes", "failures", "rate_limited_count", "forbidden_count")}
    done = totals["successes"] + totals["failures"]
    latencies = [e["latency_ms"] for e in per_service.values() if e.get("latency_ms") is not None]
    return {"connected": connected, "healthy": healthy, "active": bool(active_in), "active_in": active_in,
            "rate_limited_now": [s for s, e in per_service.items() if e.get("rate_limited")],
            "auth_failed_now": [s for s, e in per_service.items() if e.get("auth_failed")],
            "refused_methods": sorted({m for e in per_service.values() for m in (e.get("forbidden_methods") or [])}),
            "success_rate": round(totals["successes"] / done, 4) if done else None,
            "error_rate": round(totals["failures"] / done, 4) if done else None, **totals,
            "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "last_success_at": max((e.get("last_success_at") or "" for e in per_service.values()), default="") or None,
            "last_failure_at": max((e.get("last_failure_at") or "" for e in per_service.values()), default="") or None,
            "last_error": next((e.get("last_error") for e in per_service.values() if e.get("last_error")), None),
            "services": sorted(per_service), **_capabilities(per_service, last_test)}


def _capabilities(per_service: dict, last_test: dict | None) -> dict:
    """Method capability matrix for one endpoint: the latest observation per
    method from real traffic (any service) or from the TEST probe."""
    caps: dict[str, dict] = {}
    probe = (last_test or {}).get("capabilities") or {}
    for method, entry in (probe.get("methods") or {}).items():
        caps[method] = {"status": entry.get("status"), "at": probe.get("probed_at"), "source": "probe",
                        "detail": entry.get("detail"), "latency_ms": entry.get("latency_ms")}
    for service, e in per_service.items():
        for method, entry in (e.get("capabilities") or {}).items():
            cur = caps.get(method)
            if cur is None or (entry.get("at") or "") > (cur.get("at") or ""):
                caps[method] = {"status": entry.get("status"), "at": entry.get("at"), "source": f"traffic:{service}",
                                "detail": entry.get("error"), "latency_ms": None}
    versions: dict[str, int] = dict(probe.get("tx_versions_seen") or {})
    rl_by_method: dict[str, int] = {}
    for e in per_service.values():
        for v, n in (e.get("tx_versions") or {}).items():
            versions[v] = versions.get(v, 0) + n
        for m, n in (e.get("rate_limited_by_method") or {}).items():
            rl_by_method[m] = rl_by_method.get(m, 0) + n
    return {"capabilities": caps, "tx_versions_seen": versions, "rate_limited_by_method": rl_by_method,
            "unsupported_methods": sorted({m for e in per_service.values() for m in (e.get("unsupported_methods") or [])})}


async def _listing(db: AsyncSession, redis: Redis, settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    acks = await runtime_config.read_acks(redis)
    overrides = await rpc_registry.env_overrides(db)
    rows = {str(p.id): p for p in (await db.execute(select(RpcProvider))).scalars()}
    out = []
    for r in await rpc_registry.providers(db, settings):
        p = rows.get(r.get("id", ""))
        last_test = (p.last_test if p else (overrides.get(r["label"]) or {}).get("last_test"))
        out.append({
            "label": r["label"], "id": r.get("id"), "name": r["name"], "source": r["source"],
            "provider_type": p.provider_type if p else "env", "chain": "solana",
            "rpc_url": redact_url(r.get("url")), "ws_url": (p.ws_display if p else None),
            "enabled": r["enabled"], "priority": r["priority"], "timeout_seconds": r.get("timeout"), "rate_limit_rps": r.get("rps"),
            "notes": p.notes if p else f"from .env ({r.get('variable')}); URL changes on the server or via Settings → keys",
            "roles": r.get("roles") or [], "plan": r.get("plan"),
            "configured": not r.get("decrypt_failed", False),
            "decrypt_failed": r.get("decrypt_failed", False), "last_test": last_test,
            **_health(r["label"], acks, last_test, now),
        })
    active = next((e["label"] for e in out if "decision-engine" in e["active_in"]), None) \
        or next((e["label"] for e in out if e["active"]), None)
    failovers = [json.loads(x) for x in await redis.lrange(rpc_registry.FAILOVER_LOG, 0, 19)]
    # Request routing: which endpoint each service used last for each RPC method.
    routing = {service: ((ack.get("status") or {}).get("rpc") or {}).get("methods") or []
               for service, ack in acks.items() if ((ack.get("status") or {}).get("rpc") or {}).get("methods")}
    role_fallbacks = {service: ((ack.get("status") or {}).get("rpc") or {}).get("role_fallbacks") or {}
                      for service, ack in acks.items()}
    return jsonable({"providers": out, "active": active, "failovers": failovers, "provider_types": PROVIDER_TYPES,
                     "routing": routing, "roles": list(provider_roles.ROLES),
                     "role_fallbacks": {k: v for k, v in role_fallbacks.items() if v},
                     "note": "Order = priority (lowest first). Each request goes to the first usable endpoint; on "
                             "failure or HTTP 429 the same request moves to the next one."})


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                         settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    return await _listing(db, redis, settings)


async def _enabled_count(db: AsyncSession, settings: Settings) -> int:
    return len(await rpc_registry.effective_rpc(db, settings))


@router.post("/providers")
async def add_provider(body: ProviderIn, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    """Validate → password → test the connection → save (encrypted). The
    provider is saved even when the test fails (e.g. rate limited right
    now), and the result says so; it is never reported healthy until real
    requests succeed."""
    _check_type(body.provider_type)
    _check_urls(body.rpc_url, body.ws_url)
    roles = _roles(body.roles)
    await require_password(db, redis, username, body.password, request, "rpc",
                           {"action": "add", "name": body.name, "chain": body.chain})
    if (await db.execute(select(RpcProvider).where(RpcProvider.name == body.name))).scalar_one_or_none():
        raise HTTPException(409, f"a provider named {body.name!r} already exists")
    test = await _test_url(request, body.chain, body.rpc_url, float(body.timeout_seconds))
    p = RpcProvider(name=body.name, chain=body.chain, provider_type=body.provider_type,
                    rpc_url_enc=secretbox.encrypt(settings, body.rpc_url), rpc_display=redact_url(body.rpc_url),
                    ws_url_enc=secretbox.encrypt(settings, body.ws_url) if body.ws_url else None,
                    ws_display=redact_url(body.ws_url) if body.ws_url else None, enabled=body.enabled, priority=body.priority,
                    timeout_seconds=body.timeout_seconds, rate_limit_rps=body.rate_limit_rps, notes=body.notes,
                    last_test=test, created_by=username, roles=roles, plan=body.plan or None)
    db.add(p)
    await audit(db, username, request, "rpc.provider_added", {"name": body.name, "type": body.provider_type, "chain": body.chain,
                                                              "host": redact_url(body.rpc_url), "test": test["status"]})
    await db.commit()
    return {"provider": {"id": str(p.id), "name": p.name, "chain": p.chain, "rpc_url": p.rpc_display, "enabled": p.enabled,
                         "priority": p.priority}, "test": test}


@router.patch("/providers/{provider_id}")
async def edit_provider(provider_id: UUID, body: ProviderPatch, request: Request, db: AsyncSession = Depends(get_db),
                        redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                        username: str = Depends(get_current_username)) -> dict:
    p = await db.get(RpcProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    _check_type(body.provider_type)
    _check_urls(body.rpc_url, body.ws_url)
    if body.rpc_url is not None or body.ws_url:
        if not body.password:
            raise HTTPException(422, "changing a URL needs your password")
        await require_password(db, redis, username, body.password, request, "rpc", {"action": "edit_url", "name": p.name})
    changed = []
    if body.name is not None and body.name != p.name:
        if (await db.execute(select(RpcProvider).where(RpcProvider.name == body.name))).scalar_one_or_none():
            raise HTTPException(409, f"a provider named {body.name!r} already exists")
        p.name = body.name
        changed.append("name")
    if body.rpc_url is not None:
        p.rpc_url_enc, p.rpc_display = secretbox.encrypt(settings, body.rpc_url), redact_url(body.rpc_url)
        p.last_test = await _test_url(request, p.chain, body.rpc_url, float(body.timeout_seconds or p.timeout_seconds))
        changed.append("rpc_url")
    if body.ws_url is not None:
        p.ws_url_enc = secretbox.encrypt(settings, body.ws_url) if body.ws_url else None
        p.ws_display = redact_url(body.ws_url) if body.ws_url else None
        changed.append("ws_url")
    for field in ("provider_type", "enabled", "priority", "timeout_seconds", "notes"):
        v = getattr(body, field)
        if v is not None:
            setattr(p, field, v)
            changed.append(field)
    if body.rate_limit_rps is not None:
        p.rate_limit_rps = body.rate_limit_rps or None
        changed.append("rate_limit_rps")
    if body.roles is not None:
        p.roles = _roles(body.roles)
        changed.append("roles")
    if body.plan is not None:
        p.plan = body.plan or None
        changed.append("plan")
    p.updated_at = datetime.now(timezone.utc)
    await db.flush()
    if p.chain == "solana" and await _enabled_count(db, settings) == 0:
        await db.rollback()
        raise HTTPException(409, "this would leave no enabled RPC endpoint; enable another one first")
    await audit(db, username, request, "rpc.provider_updated", {"name": p.name, "changed": changed})
    await db.commit()
    return {"id": str(p.id), "changed": changed, "test": p.last_test if "rpc_url" in changed else None}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    p = await db.get(RpcProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    name, chain = p.name, p.chain
    await db.delete(p)
    await db.flush()
    if chain == "solana" and await _enabled_count(db, settings) == 0:
        await db.rollback()
        raise HTTPException(409, "this would leave no enabled RPC endpoint; add or enable another one first")
    await audit(db, username, request, "rpc.provider_deleted", {"name": name})
    await db.commit()
    return {"deleted": name}


@router.put("/providers/env/{label}")
async def edit_env_endpoint(label: str, body: EnvPatch, request: Request, db: AsyncSession = Depends(get_db),
                            settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    """Enable/disable or reorder a .env endpoint (its URL stays in .env)."""
    if label not in {slot[0] for slot in rpc_registry.ENV_RPC}:
        raise HTTPException(404, f"unknown .env endpoint; one of {[s[0] for s in rpc_registry.ENV_RPC]}")
    overrides = await rpc_registry.env_overrides(db)
    entry = dict(overrides.get(label) or {})
    if body.enabled is not None:
        entry["enabled"] = body.enabled
    if body.priority is not None:
        entry["priority"] = body.priority
    if body.roles is not None:
        entry["roles"] = _roles(body.roles)
    if body.plan is not None:
        entry["plan"] = body.plan or None
    overrides[label] = entry
    await db.execute(insert(PlatformSetting).values(key=rpc_registry.ENV_OVERRIDES_KEY, value=overrides)
                     .on_conflict_do_update(index_elements=[PlatformSetting.key], set_={"value": overrides}))
    await db.flush()
    if await _enabled_count(db, settings) == 0:
        await db.rollback()
        raise HTTPException(409, "this would leave no enabled RPC endpoint; enable another one first")
    await audit(db, username, request, "rpc.env_endpoint_updated", {"label": label, **body.model_dump(exclude_none=True)})
    await db.commit()
    return {"label": label, **entry}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """TEST CONNECTION for a dashboard provider (id) or a .env endpoint
    (its label, e.g. env:primary). Result only; never the URL."""
    if provider_id.split(":")[0] in ("env", "public") and provider_id.count(":") == 2:  # env:bsc:0, public:robinhood:0
        return await _test_evm_builtin(provider_id, request, db, settings)
    if provider_id.startswith("env:"):
        row = next((r for r in await rpc_registry.providers(db, settings) if r["label"] == provider_id), None)
        if row is None:
            raise HTTPException(404, "that .env endpoint is not configured")
        result = await rpc_registry.test_rpc(request.app.state.http, row["url"])
        if result.get("status") == rpc_registry.CONNECTED:
            result["capabilities"] = await rpc_registry.probe_capabilities(request.app.state.http, row["url"],
                                                                           settings.WALLET_PUBLIC_KEY)
        overrides = await rpc_registry.env_overrides(db)
        overrides[provider_id] = {**(overrides.get(provider_id) or {}), "last_test": result}
        await db.execute(insert(PlatformSetting).values(key=rpc_registry.ENV_OVERRIDES_KEY, value=overrides)
                         .on_conflict_do_update(index_elements=[PlatformSetting.key], set_={"value": overrides}))
    else:
        try:
            p = await db.get(RpcProvider, UUID(provider_id))
        except ValueError:
            p = None
        if p is None:
            raise HTTPException(404, "provider not found")
        url = secretbox.decrypt(settings, p.rpc_url_enc)
        if url and p.chain != "solana":
            p.last_test = await _test_url(request, p.chain, url, float(p.timeout_seconds))
            await db.commit()
            return p.last_test
        result = (await rpc_registry.test_rpc(request.app.state.http, url, float(p.timeout_seconds)) if url else
                  {"status": rpc_registry.INVALID, "detail": "stored URL cannot be decrypted (encryption key changed) — re-enter it",
                   "latency_ms": None, "tested_at": datetime.now(timezone.utc).isoformat()})
        if url and result.get("status") == rpc_registry.CONNECTED:
            result["capabilities"] = await rpc_registry.probe_capabilities(request.app.state.http, url,
                                                                           settings.WALLET_PUBLIC_KEY,
                                                                           float(p.timeout_seconds))
        p.last_test = result
    await db.commit()
    return result


# --- BSC / Robinhood Chain ------------------------------------------------------------------------

async def _test_url(request: Request, chain: str, url: str, timeout: float) -> dict:
    if chain == "solana":
        return await rpc_registry.test_rpc(request.app.state.http, url, timeout)
    return await evm_registry.test_evm_rpc(request.app.state.http, url, chain, timeout)


async def _save_evm_override(db: AsyncSession, label: str, entry: dict) -> None:
    ov = await evm_registry.overrides(db)
    ov[label] = {**(ov.get(label) or {}), **entry}
    await db.execute(insert(PlatformSetting).values(key=evm_registry.OVERRIDES_KEY, value=ov)
                     .on_conflict_do_update(index_elements=[PlatformSetting.key], set_={"value": ov}))


async def _test_evm_builtin(label: str, request: Request, db: AsyncSession, settings: Settings) -> dict:
    chain = label.split(":")[1]
    if chain not in evm_registry.EVM_CHAINS:
        raise HTTPException(404, "unknown chain")
    row = next((r for r in await evm_registry.endpoints(db, settings, chain) if r["label"] == label), None)
    if row is None:
        raise HTTPException(404, "that endpoint is not configured")
    result = await evm_registry.test_evm_rpc(request.app.state.http, row["url"], chain)
    await _save_evm_override(db, label, {"last_test": result})
    await db.commit()
    return result


GUIDE = {
    "summary": "Discovery on BSC and Robinhood Chain reads launchpad events with eth_getLogs every few seconds. "
               "Free tiers limit eth_getLogs to a few blocks per request (Alchemy free: 10 blocks on BNB and "
               "Robinhood; QuickNode free trial: 5), which works but needs many requests; a paid plan lifts it "
               "(Alchemy Pay As You Go: no range limit on BNB and Robinhood; QuickNode paid: 10,000 blocks). "
               "TEST CONNECTION shows the span each endpoint actually serves.",
    "providers": [
        {"name": "Alchemy", "chains": ["solana", "bsc", "robinhood"], "where": "https://dashboard.alchemy.com",
         "steps": "Create an app, enable Solana, BNB Smart Chain and Robinhood Chain mainnet, then copy each "
                  "network's HTTPS URL (one API key, one URL per chain) into ADD RPC with the matching chain."},
        {"name": "QuickNode", "chains": ["solana", "bsc", "robinhood"], "where": "https://dashboard.quicknode.com",
         "steps": "Create an endpoint per chain (Solana mainnet, BNB Smart Chain mainnet, Robinhood Chain mainnet) "
                  "and copy each HTTPS URL into ADD RPC with the matching chain."},
    ],
    "note": "Provider features and limits change; the TEST result on this page is what counts, not this text.",
}


@router.get("/evm")
async def list_evm(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                   settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """BSC / Robinhood endpoints in the order data-evm and copy-engine use
    them, with the live state data-evm reported (keyed by scheme://host)."""
    out = {}
    for chain in evm_registry.EVM_CHAINS:
        raw = await redis.get(f"yx:evm:rpc:{chain}")
        live = {}
        if raw:
            try:
                h = json.loads(raw)
                live = {e["url"]: {**e, "reported_at": h.get("at")} for e in h.get("endpoints", [])}
            except (ValueError, KeyError, TypeError):
                live = {}
        rows = []
        for r in await evm_registry.endpoints(db, settings, chain):
            host = redact_url(r.get("url"))
            rows.append({"label": r["label"], "id": r.get("id"), "name": r["name"], "source": r["source"],
                         "provider_type": r.get("provider_type"), "rpc_url": host, "enabled": r["enabled"],
                         "priority": r["priority"], "decrypt_failed": r.get("decrypt_failed", False),
                         "notes": r.get("notes"), "last_test": r.get("last_test"), "live": live.get(host),
                         "roles": r.get("roles") or [], "plan": r.get("plan"), "ws_url": r.get("ws_display"),
                         "rate_limit_rps": r.get("rate_limit_rps")})
        out[chain] = {"chain_id": evm_registry.chain_id(chain), "endpoints": rows,
                      "in_use": [r["rpc_url"] for r in rows if r["enabled"]]}
    return jsonable({"chains": out, "guide": GUIDE,
                     "note": "Order = priority (lowest first): dashboard entries default to 150, .env "
                             "(BSC_RPC_URLS / ROBINHOOD_RPC_URLS) 500+, built-in public 900+. Live state comes from "
                             "data-evm's last report; a chain with every endpoint disabled falls back to the public ones."})


class EvmBuiltinPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=9999)
    roles: list[str] | None = None
    plan: str | None = Field(None, max_length=64)


@router.put("/evm/{label}")
async def edit_evm_builtin(label: str, body: EvmBuiltinPatch, request: Request, db: AsyncSession = Depends(get_db),
                           settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    """Enable/disable or reorder a .env or built-in public BSC / Robinhood
    endpoint (its URL stays where it is). The last enabled endpoint of a
    chain cannot be disabled."""
    parts = label.split(":")
    if len(parts) != 3 or parts[0] not in ("env", "public") or parts[1] not in evm_registry.EVM_CHAINS:
        raise HTTPException(404, "unknown endpoint label")
    chain = parts[1]
    rows = await evm_registry.endpoints(db, settings, chain)
    if not any(r["label"] == label for r in rows):
        raise HTTPException(404, "that endpoint is not configured")
    if body.enabled is False and not any(r["enabled"] and r["label"] != label for r in rows):
        raise HTTPException(409, f"this is the last enabled {chain} endpoint; add or enable another one first")
    if body.roles is not None:
        body.roles = _roles(body.roles)
    await _save_evm_override(db, label, body.model_dump(exclude_none=True))
    await audit(db, username, request, "rpc.evm_endpoint_updated", {"label": label, **body.model_dump(exclude_none=True)})
    await db.commit()
    return {"label": label, **body.model_dump(exclude_none=True)}


# --- plan health (master upgrade §53) -----------------------------------------------------------------

async def evm_live(redis: Redis, chain: str) -> dict | None:
    raw = await redis.get(f"yx:evm:rpc:{chain}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


@router.get("/plan-health")
async def plan_health(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """UPGRADE REQUIRED and configuration findings per chain, from what the
    services observed on real requests, plus which roles have a dedicated
    provider and how often a role had to fall back to any endpoint."""
    solana = (await _listing(db, redis, settings))
    findings = provider_roles.solana_findings(solana["providers"])
    coverage: dict[str, dict] = {"solana": {r: sorted(p["name"] for p in solana["providers"]
                                                      if p["enabled"] and r in (p.get("roles") or []))
                                            for r in provider_roles.ROLES}}
    fallbacks: dict[str, dict] = {"solana": {}}
    for per_service in (solana.get("role_fallbacks") or {}).values():  # summed over the services that reported
        for role, n in per_service.items():
            fallbacks["solana"][role] = fallbacks["solana"].get(role, 0) + n
    for chain in evm_registry.EVM_CHAINS:
        if not system_profile.chain_enabled(settings, chain):  # a chain switched off by the profile has nothing to upgrade
            continue
        rows = await evm_registry.endpoints(db, settings, chain)
        live = await evm_live(redis, chain)
        findings += provider_roles.evm_findings(chain, rows, live)
        findings += provider_roles.stream_findings(chain, await evm_streams.reports(redis, chain))
        coverage[chain] = {r: sorted(e["name"] for e in rows if e["enabled"] and r in (e.get("roles") or []))
                           for r in provider_roles.ROLES}
        fallbacks[chain] = (live or {}).get("role_fallbacks") or {}
    order = {provider_roles.UPGRADE: 0, provider_roles.CONFIGURATION: 1, provider_roles.INFO: 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["chain"]))
    return jsonable({"findings": findings, "upgrade_required": sum(1 for f in findings if f["severity"] == provider_roles.UPGRADE),
                     "role_coverage": coverage, "role_fallbacks": fallbacks, "roles": list(provider_roles.ROLES),
                     "note": "from real requests the services reported and the configuration; the plan shown is what "
                             "you recorded for the provider (not verified with the provider). A role with no dedicated "
                             "provider is served by every endpoint."})
