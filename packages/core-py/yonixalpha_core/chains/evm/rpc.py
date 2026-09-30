"""EVM JSON-RPC client with ordered failover (BSC, Robinhood Chain).

- Every endpoint's eth_chainId is checked once before it is used; an
  endpoint that answers for another chain is disabled (WRONG_CHAIN) and never
  used, so a mistyped URL can never quote or trade on the wrong network.
- HTTP 429 cools the endpoint down for Retry-After seconds when sent, else
  for a doubling backoff (2 s .. 120 s); network errors and 5xx for 5 s.
  The next endpoint is tried at once. When the only endpoints left are
  cooling down after a 429 and the wait is short (<= cooldown_wait_s), the
  call waits it out instead of failing, so one 429 does not fail every
  launchpad scanned in the same pass.
- Requests to one endpoint are paced: every 429 doubles the gap between
  requests (up to 2 s) and every answer shrinks it again, so a public node
  that rate-limits is asked at the rate it accepts.
- A method an endpoint refuses (JSON-RPC "method not found", eth_getLogs
  refused even for one block, or HTTP 403 for eth_getLogs from a node that
  answers other methods) is skipped on that endpoint for 30 minutes, then
  asked again.
- A JSON-RPC error (e.g. "execution reverted") is an answer, not an
  endpoint failure: it is raised as EvmRpcError to the caller unchanged.
- URLs embed API keys, so they only ever appear as scheme://host
  (yonixalpha_core.redact) in health output, errors and logs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from yonixalpha_core.redact import redact_text, redact_url


class EvmRpcError(RuntimeError):
    """The node answered with a JSON-RPC error (revert, bad params, ...)."""

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class EvmRpcUnavailableError(RuntimeError):
    """No endpoint could answer (all failing, cooling down or wrong chain)."""


# A JSON-RPC error that means "this endpoint does not serve the method" (not
# "the request failed"): the next endpoint is tried and the method is
# skipped on this endpoint from then on.
CAPABILITY_ERRORS = ("method not found", "not supported", "is not available", "not available on", "disabled",
                     "not whitelisted", "unsupported method", "method not allowed")
RANGE_ERRORS = ("range", "too many", "limit exceeded", "exceed", "response size", "10000 results", "block range")
UNSUPPORTED_METHOD_SECONDS = 30 * 60.0
MAX_GAP_S = 2.0


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return max(0.0, float(v)) if v is not None else None
    except ValueError:
        return None


@dataclass
class _Endpoint:
    url: str
    state: str = "UNCHECKED"  # UNCHECKED | OK | COOLDOWN | WRONG_CHAIN | FAILING
    cooldown_until: float = 0.0
    backoff: float = 0.0
    last_error: str | None = None
    ok: int = 0
    errors: int = 0
    rate_limited: int = 0
    latency_ms: float | None = None
    chain_id: int | None = None
    extra: dict = field(default_factory=dict)
    unsupported: dict = field(default_factory=dict)  # method -> monotonic time until which it is not asked here
    min_gap: float = 0.0  # seconds between requests (grows on 429, shrinks on answers)
    next_at: float = 0.0

    def refuses(self, method: str, now: float) -> bool:
        return self.unsupported.get(method, 0.0) > now

    def mark_unsupported(self, method: str) -> None:
        self.unsupported[method] = time.monotonic() + UNSUPPORTED_METHOD_SECONDS

    def usable(self, now: float, method: str = "") -> bool:
        return self.state != "WRONG_CHAIN" and now >= self.cooldown_until and not self.refuses(method, now)


def _capability_error(exc: EvmRpcError, method: str) -> bool:
    """Only "method not found" counts for eth_call / eth_estimateGas: their
    revert reasons are contract text ("trading disabled") and must reach the
    caller as a revert, never be mistaken for an endpoint limitation."""
    if exc.code == -32601:
        return True
    msg = str(exc).lower()
    if method in ("eth_call", "eth_estimateGas") or exc.code == 3 or "revert" in msg:
        return False
    return any(k in msg for k in CAPABILITY_ERRORS)


class EvmRpc:
    def __init__(self, chain: str, chain_id: int, urls: list[str] | tuple[str, ...], *,
                 client: httpx.AsyncClient | None = None, timeout: float = 10.0,
                 cooldown_wait_s: float = 8.0) -> None:
        if not urls:
            raise ValueError(f"{chain}: no RPC URLs")
        self.chain = chain
        self.chain_id = chain_id
        self.endpoints = [_Endpoint(u) for u in dict.fromkeys(u.strip() for u in urls if u and u.strip())]
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._own_client = client is None
        self.cooldown_wait_s = cooldown_wait_s
        self._id = 0
        self._last_error_ep: _Endpoint | None = None  # the endpoint that answered the last EvmRpcError

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def _redact(self, text: str) -> str:
        return redact_text(text, [e.url for e in self.endpoints])

    async def _post(self, ep: _Endpoint, method: str, params: list) -> Any:
        now = time.monotonic()
        if ep.next_at > now:
            await asyncio.sleep(ep.next_at - now)
        ep.next_at = max(now, ep.next_at) + ep.min_gap
        self._id += 1
        t0 = time.monotonic()
        r = await self._client.post(ep.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        ep.latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 429:
            ep.min_gap = min(MAX_GAP_S, max(0.1, ep.min_gap * 2))
            raise _RateLimited(_retry_after(r))
        ep.min_gap = ep.min_gap * 0.97 if ep.min_gap > 0.01 else 0.0
        if r.status_code == 403 and method == "eth_getLogs" and ep.ok > 0:
            raise _MethodRefused("HTTP 403 for eth_getLogs (the node answers other methods)")
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            raise EvmRpcError(str(err.get("message", err))[:300], err.get("code"), err.get("data"))
        if not isinstance(body, dict) or "result" not in body:
            raise httpx.HTTPError(f"malformed JSON-RPC response for {method}")
        return body["result"]

    async def _ensure_chain(self, ep: _Endpoint) -> None:
        if ep.chain_id is not None:
            return
        got = int(await self._post(ep, "eth_chainId", []), 16)
        ep.chain_id = got
        if got != self.chain_id:
            ep.state = "WRONG_CHAIN"
            ep.last_error = f"eth_chainId {got}, expected {self.chain_id}"
            raise _WrongChain(ep.last_error)
        ep.state = "OK"

    def _cooldown_wait(self, method: str, now: float) -> float | None:
        """Seconds until an endpoint cooling down after a 429 can be asked
        `method` again (None: no endpoint is merely rate-limited)."""
        waits = [e.cooldown_until - now for e in self.endpoints
                 if e.state == "COOLDOWN" and e.cooldown_until > now and not e.refuses(method, now)]
        return min(waits) if waits else None

    async def call(self, method: str, params: list | None = None) -> Any:
        params = params or []
        deadline = time.monotonic() + self.cooldown_wait_s
        while True:
            now = time.monotonic()
            order = [e for e in self.endpoints if e.usable(now, method)]
            if not order:
                wait = self._cooldown_wait(method, now)
                if wait is not None and now + wait <= deadline:
                    await asyncio.sleep(wait)
                    continue
                alive = [e for e in self.endpoints if e.state != "WRONG_CHAIN"]
                if alive and all(e.refuses(method, now) for e in alive):
                    raise EvmRpcUnavailableError(f"{self.chain} RPC: no configured endpoint serves {method}")
                waits = [e.cooldown_until - now for e in alive if not e.refuses(method, now)]
                detail = (f"all cooling down for {min(waits):.0f}s more" if waits
                          else "every endpoint answers for the wrong chain")
                raise EvmRpcUnavailableError(f"{self.chain} RPC unavailable: {detail}")
            causes: list[str] = []
            for ep in order:
                try:
                    await self._ensure_chain(ep)
                    result = await self._post(ep, method, params)
                    ep.ok += 1
                    ep.backoff = 0.0
                    ep.state = "OK"
                    ep.last_error = None
                    return result
                except EvmRpcError as exc:
                    ep.ok += 1  # the endpoint works
                    if not _capability_error(exc, method):
                        self._last_error_ep = ep
                        raise  # the request itself failed (e.g. reverted): same answer anywhere
                    ep.mark_unsupported(method)
                    causes.append(f"{redact_url(ep.url)}: {method} not served ({self._redact(str(exc))[:80]})")
                except _MethodRefused as exc:
                    ep.mark_unsupported(method)
                    ep.last_error = str(exc)
                    causes.append(f"{redact_url(ep.url)}: {exc}")
                except _WrongChain as exc:
                    causes.append(f"{redact_url(ep.url)}: {exc}")
                except _RateLimited as exc:
                    ep.rate_limited += 1
                    ep.backoff = min(120.0, max(2.0, ep.backoff * 2))
                    wait = exc.retry_after if exc.retry_after is not None else ep.backoff
                    ep.cooldown_until = time.monotonic() + wait
                    ep.state, ep.last_error = "COOLDOWN", "HTTP 429"
                    causes.append(f"{redact_url(ep.url)}: HTTP 429")
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    ep.errors += 1
                    ep.cooldown_until = time.monotonic() + 5.0
                    ep.state = "FAILING"
                    ep.last_error = self._redact(f"{type(exc).__name__}: {exc}")[:200]
                    causes.append(f"{redact_url(ep.url)}: {ep.last_error}")
            now = time.monotonic()
            wait = self._cooldown_wait(method, now)
            if wait is not None and now + wait <= deadline:
                continue  # only rate-limited: wait it out at the top of the loop
            raise EvmRpcUnavailableError(f"{self.chain} RPC {method} failed on every endpoint: " + "; ".join(causes))

    # --- typed helpers ---------------------------------------------------------------------------

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def get_block(self, number: int | str = "latest") -> dict[str, Any]:
        tag = hex(number) if isinstance(number, int) else number
        return await self.call("eth_getBlockByNumber", [tag, False])

    async def get_code(self, address: str) -> str:
        return await self.call("eth_getCode", [address, "latest"])

    async def get_storage_at(self, address: str, slot: str) -> str:
        return await self.call("eth_getStorageAt", [address, slot, "latest"])

    async def get_balance(self, address: str) -> int:
        return int(await self.call("eth_getBalance", [address, "latest"]), 16)

    async def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return await self.call("eth_getTransactionReceipt", [tx_hash])

    async def eth_call(self, to: str, data: str, *, from_: str | None = None, value: int | None = None,
                       block: str = "latest", state_override: dict | None = None) -> str:
        tx: dict[str, Any] = {"to": to, "data": data}
        if from_:
            tx["from"] = from_
        if value:
            tx["value"] = hex(value)
        params: list[Any] = [tx, block]
        if state_override:
            params.append(state_override)
        return await self.call("eth_call", params)

    async def get_logs(self, addresses: list[str] | str | None, topics: list, from_block: int, to_block: int,
                       *, max_span: int = 2000) -> list[dict[str, Any]]:
        """eth_getLogs over [from_block, to_block] in chunks of at most
        `max_span` blocks; a chunk the node refuses as too large is halved
        until it is accepted. A node that refuses even a single block (some
        public nodes answer every eth_getLogs with "limit exceeded", e.g.
        bsc-dataseed.binance.org) does not serve logs: it is marked so and
        the next endpoint is asked, from the full span again."""
        out: list[dict[str, Any]] = []
        start = from_block
        span = max(1, max_span)
        while start <= to_block:
            end = min(to_block, start + span - 1)
            try:
                flt: dict[str, Any] = {"topics": topics, "fromBlock": hex(start), "toBlock": hex(end)}
                if addresses is not None:  # None: any emitter (the caller validates it)
                    flt["address"] = addresses
                logs = await self.call("eth_getLogs", [flt])
            except EvmRpcError as exc:
                if any(k in str(exc).lower() for k in RANGE_ERRORS):
                    if span > 1:
                        span = max(1, span // 2)
                        continue
                    ep = self._last_error_ep
                    if ep is not None and not ep.refuses("eth_getLogs", time.monotonic()):
                        ep.mark_unsupported("eth_getLogs")
                        ep.last_error = self._redact(f"eth_getLogs refused even for one block: {exc}")[:200]
                        span = max(1, max_span)
                        continue
                raise
            out.extend(logs or [])
            start = end + 1
        return out

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {"chain": self.chain, "chain_id": self.chain_id, "endpoints": [
            {"url": redact_url(e.url), "state": e.state if e.state != "COOLDOWN" or now < e.cooldown_until else "OK",
             "cooldown_s": round(max(0.0, e.cooldown_until - now), 1), "last_error": e.last_error,
             "ok": e.ok, "errors": e.errors, "rate_limited": e.rate_limited, "latency_ms": e.latency_ms,
             "chain_id_seen": e.chain_id, "min_gap_s": round(e.min_gap, 3),
             "unsupported_methods": sorted(m for m, until in e.unsupported.items() if until > now)} for e in self.endpoints]}

    async def publish_health(self, redis) -> None:
        if redis is not None:
            await redis.set(f"yx:evm:rpc:{self.chain}", json.dumps({**self.health(), "at": time.time()}), ex=600)


class _RateLimited(Exception):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("HTTP 429")
        self.retry_after = retry_after


class _WrongChain(Exception):
    pass


class _MethodRefused(Exception):
    """The endpoint refused this method over HTTP (not a JSON-RPC answer)."""


def make_rpc(chain: str, settings: Any = None, *, client: httpx.AsyncClient | None = None) -> EvmRpc:
    """RPC for `chain` ("bsc" | "robinhood"): the configured URLs
    (BSC_RPC_URLS / ROBINHOOD_RPC_URLS, comma-separated) first, then the
    chain's public endpoints as the last fallback."""
    from yonixalpha_core.chains.base import Chain
    from yonixalpha_core.chains.registry import CHAINS

    spec = CHAINS[Chain(chain)]
    if spec.evm_chain_id is None:
        raise ValueError(f"{chain} is not an EVM chain")
    configured = getattr(settings, f"{chain.upper()}_RPC_URLS", None) if settings is not None else None
    urls = [u for u in (configured or "").split(",") if u.strip()] + list(spec.public_rpc)
    return EvmRpc(chain, spec.evm_chain_id, urls, client=client)


async def gather_limited(coros: list, limit: int = 4) -> list:
    """Runs coroutines with at most `limit` in flight (public RPCs rate-limit
    bursts); exceptions are returned, not raised."""
    sem = asyncio.Semaphore(limit)

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros), return_exceptions=True)
