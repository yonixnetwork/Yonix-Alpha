"""The effective BSC / Robinhood Chain RPC list: endpoints added in the
dashboard (rpc_providers rows with chain "bsc" / "robinhood", URLs encrypted),
then BSC_RPC_URLS / ROBINHOOD_RPC_URLS from .env, then the chain's built-in
public endpoints, ordered by priority (lowest first).

Running services (data-evm, copy-engine) reload it on every configuration
revision (runtime_config), so adding, reordering or disabling an endpoint
needs no restart. .env and public endpoints can be disabled or reordered
from the dashboard ("evm_rpc_overrides"); their URLs stay where they are.
A chain is never left with no endpoint: if every one is disabled the public
ones are used and the reload says so.

`test_evm_rpc` is the TEST CONNECTION behind the dashboard and the
evm_rpc_probe tool: the chain id must match, and eth_getLogs is asked over
10 / 100 / 1000 / 2000 blocks of the chain's busiest launchpad contract,
ending a few blocks under the head (a load-balanced node can report a head
another of its backends does not have yet). Discovery needs eth_getLogs, so
an endpoint that refuses it is reported NO_LOGS, never CONNECTED.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import secretbox
from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.registry import CHAINS, LAUNCHPADS
from yonixalpha_core.db.models import PlatformSetting, RpcProvider
from yonixalpha_core.logging import get_logger
from yonixalpha_core.redact import redact_text, redact_url

log = get_logger("core.evm_rpc_registry")

EVM_CHAINS = ("bsc", "robinhood")
OVERRIDES_KEY = "evm_rpc_overrides"
DASHBOARD_PRIORITY = 150
ENV_PRIORITY = 500
PUBLIC_PRIORITY = 900
SPANS = (10, 100, 1000, 2000)  # 2000: the span discovery asks for
HEAD_MARGIN = 5
# The busiest emitter per chain (production, 2026-09-30): Flap's Portal on BSC
# (~13k logs per 1500 blocks), the Pons V2 factory on Robinhood Chain.
LOGS_PROBE_CONTRACT = {"bsc": LAUNCHPADS["flap"].contracts["portal"],
                       "robinhood": LAUNCHPADS["pons_v2"].contracts["factory"]}

CONNECTED, NO_LOGS, AUTH_FAILED, TIMEOUT, RATE_LIMITED, INVALID, UNAVAILABLE = (
    "CONNECTED", "NO_LOGS", "AUTHENTICATION_FAILED", "TIMEOUT", "RATE_LIMITED", "INVALID_CONFIGURATION", "UNAVAILABLE")


def chain_id(chain: str) -> int:
    return int(CHAINS[Chain(chain)].evm_chain_id)


async def overrides(session: AsyncSession) -> dict[str, dict[str, Any]]:
    row = await session.get(PlatformSetting, OVERRIDES_KEY)
    return dict(row.value) if row else {}


async def endpoints(session: AsyncSession, settings: Any, chain: str) -> list[dict[str, Any]]:
    """Every endpoint of `chain`, enabled or not, decrypted (server-side only)."""
    ov = await overrides(session)
    rows: list[dict[str, Any]] = []
    for p in (await session.execute(select(RpcProvider).where(RpcProvider.chain == chain))).scalars():
        url = secretbox.decrypt(settings, p.rpc_url_enc)
        rows.append({"label": f"db:{p.name}", "name": p.name, "url": url, "source": "dashboard", "id": str(p.id),
                     "provider_type": p.provider_type, "priority": p.priority, "enabled": p.enabled and url is not None,
                     "decrypt_failed": url is None, "last_test": p.last_test, "notes": p.notes})
    var = f"{chain.upper()}_RPC_URLS"
    env = [u.strip() for u in (getattr(settings, var, None) or "").split(",") if u.strip()]
    for i, url in enumerate(env):
        rows.append({"label": f"env:{chain}:{i}", "name": f"{var} #{i + 1}", "url": url, "source": "env",
                     "provider_type": "env", "priority": ENV_PRIORITY + i, "enabled": True})
    for i, url in enumerate(CHAINS[Chain(chain)].public_rpc):
        rows.append({"label": f"public:{chain}:{i}", "name": f"public #{i + 1}", "url": url, "source": "public",
                     "provider_type": "public", "priority": PUBLIC_PRIORITY + i, "enabled": True})
    for r in rows:
        if r["source"] != "dashboard":
            o = ov.get(r["label"]) or {}
            r["priority"] = int(o.get("priority", r["priority"]))
            r["enabled"] = bool(o.get("enabled", True))
            r["last_test"] = o.get("last_test")
    return sorted(rows, key=lambda r: (r["priority"], r["label"]))


async def effective_urls(session: AsyncSession, settings: Any, chain: str) -> list[str]:
    urls = [r["url"] for r in await endpoints(session, settings, chain) if r["enabled"] and r.get("url")]
    if not urls:  # never leave a chain with nothing: the public endpoints keep discovery alive
        log.warning("evm_rpc.all_disabled_using_public", chain=chain)
        urls = list(CHAINS[Chain(chain)].public_rpc)
    return list(dict.fromkeys(urls))


async def rpc_for(session: AsyncSession, settings: Any, chain: str):
    """A one-off EvmRpc over the effective list (API requests, tools)."""
    from yonixalpha_core.chains.evm.rpc import make_rpc

    rpc = make_rpc(chain, settings)
    rpc.replace_urls(await effective_urls(session, settings, chain))
    return rpc


def make_reloader(rpcs: dict[str, Any], settings: Any, session_factory):
    """A runtime_config reloader: re-reads each chain's list and swaps it into
    the running EvmRpc (endpoint state of unchanged URLs is kept)."""

    async def reload_evm_rpc() -> dict:
        out = {}
        async with session_factory() as session:
            for chain, rpc in rpcs.items():
                urls = await effective_urls(session, settings, chain)
                rpc.replace_urls(urls)
                out[chain] = [redact_url(u) for u in urls]
        return {"evm_endpoints": out}

    return reload_evm_rpc


# --- connection test -----------------------------------------------------------------------------

async def _post(client, url: str, method: str, params: list | None, timeout: float) -> Any:
    r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=timeout)
    if r.status_code != 200:
        raise _Http(r.status_code)
    body = r.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RuntimeError(str(err.get("message", err) if isinstance(err, dict) else err)[:160])
    if not isinstance(body, dict) or "result" not in body:
        raise RuntimeError("response is not JSON-RPC")
    return body["result"]


class _Http(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


async def test_evm_rpc(client, url: str | None, chain: str, timeout: float = 10.0) -> dict[str, Any]:
    """A real read-only test of one BSC / Robinhood endpoint. Never returns the URL."""
    import httpx

    from yonixalpha_core.solana.rpc_registry import validate_url

    now = datetime.now(timezone.utc).isoformat()
    base = {"chain": chain, "tested_at": now, "latency_ms": None, "chain_id": None, "head": None,
            "logs": {}, "logs_max_span": None}
    err = validate_url(url)
    if err:
        return {**base, "status": INVALID, "detail": err}
    started = time.monotonic()
    try:
        cid = int(await _post(client, url, "eth_chainId", [], timeout), 16)
        base["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        base["chain_id"] = cid
        if cid != chain_id(chain):
            return {**base, "status": INVALID, "detail": f"answers for chain id {cid}, expected {chain_id(chain)} ({chain})"}
        head = int(await _post(client, url, "eth_blockNumber", [], timeout), 16)
        base["head"] = head
    except httpx.TimeoutException:
        return {**base, "status": TIMEOUT, "detail": f"no answer within {timeout:g}s"}
    except httpx.TransportError as exc:
        return {**base, "status": UNAVAILABLE, "detail": f"connection error ({type(exc).__name__})"}
    except _Http as exc:
        status = AUTH_FAILED if exc.code in (401, 403) else RATE_LIMITED if exc.code == 429 else UNAVAILABLE
        return {**base, "status": status, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a malformed answer is reported, never raised
        return {**base, "status": UNAVAILABLE, "detail": redact_text(f"{type(exc).__name__}: {exc}", [url])[:200]}
    contract = LOGS_PROBE_CONTRACT[chain]
    to_block = max(0, head - HEAD_MARGIN)
    for span in SPANS:
        try:
            logs = await _post(client, url, "eth_getLogs", [{"address": contract, "fromBlock": hex(max(0, to_block - span + 1)),
                                                             "toBlock": hex(to_block)}], timeout)
            base["logs"][str(span)] = f"OK ({len(logs or [])} logs)"
            base["logs_max_span"] = span
        except Exception as exc:  # noqa: BLE001 - each span is reported
            base["logs"][str(span)] = "REFUSED: " + redact_text(str(exc), [url])[:100]
            break  # a larger span will not be served either
    if base["logs_max_span"] is None:
        return {**base, "status": NO_LOGS,
                "detail": f"chain OK (head {head}) but eth_getLogs is refused even for {SPANS[0]} blocks: "
                          f"discovery cannot use this endpoint ({base['logs'][str(SPANS[0])]})"}
    small = base["logs_max_span"] < 100
    return {**base, "status": CONNECTED,
            "detail": f"chain {chain} OK, head {head}; eth_getLogs served up to {base['logs_max_span']} blocks"
                      + ("; a free-tier limit this small makes discovery slow (many requests per pass)" if small else "")}
