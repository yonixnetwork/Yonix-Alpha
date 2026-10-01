import hashlib
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from yonixalpha_core import provider_roles
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.redact import redact_text, redact_url

log = get_logger("data-solana.rpc")


class RpcAllEndpointsFailedError(Exception):
    pass


class RpcRateLimitedError(RpcAllEndpointsFailedError):
    """An optional lookup was not sent: every endpoint is cooling down after
    HTTP 429 (see call_optional)."""


# A provider that answers 401/403 for a method (wrong key, or a plan that
# doesn't include the method) is not asked for that method again for this
# long; its other methods keep working.
FORBIDDEN_METHOD_SECONDS = 600.0
MAX_RATE_LIMIT_BACKOFF = 300.0
# A 401/403 on one of these (or on several methods) means the key/URL itself
# is refused: the endpoint is AUTHENTICATION_FAILED and skipped for every
# method until FORBIDDEN_METHOD_SECONDS pass or a request succeeds.
CORE_METHODS = {"getSlot", "getBalance", "getAccountInfo", "getMultipleAccounts", "getLatestBlockhash",
                "sendTransaction", "simulateTransaction", "getSignatureStatuses"}
AUTH_FAILED_AFTER_METHODS = 3


# JSON-RPC codes meaning "this request is invalid / unsupported here"
# (invalid request, method not found, invalid params). The endpoint answered,
# so they don't count against its health — e.g. Helius refusing an
# unpaginated getProgramAccounts on a huge program must not put the primary
# RPC, which every other read depends on, into cooldown.
REQUEST_ERROR_CODES = {-32600, -32601, -32602, -32003}  # -32003: transaction signature verification failure


class RpcRequestError(RuntimeError):
    pass


class RpcUnsupportedTransactionVersionError(RpcRequestError):
    """The node has the transaction but its version is above the
    maxSupportedTransactionVersion we sent (JSON-RPC -32015). Every node
    answers the same way, so no other endpoint is tried and the endpoint's
    health is not touched."""


class RpcMethodUnsupportedError(RpcAllEndpointsFailedError):
    """No configured endpoint offers this method (each answered "method not
    found / not available"). A capability gap, not an outage."""


# --- transaction versions ------------------------------------------------------
# Every getTransaction declares the highest version we accept. Version 1
# transactions exist on mainnet (nodes answer -32015 "Transaction version (1)
# is not supported by the requesting client" when only 0 is declared).
# Nothing here decodes fetched transactions in binary: the node renders them
# as JSON ("json"/"jsonParsed") and our parsers read meta balances, log
# messages and parsed instructions, which every version renders the same
# way. Parsers that read the message layout check the version first
# (solana.txversion) and fail closed on one they don't know.
MAX_SUPPORTED_TRANSACTION_VERSION = 1
UNSUPPORTED_TX_VERSION_CODE = -32015


def get_transaction_params(signature: str, encoding: str = "jsonParsed", commitment: str = "confirmed") -> list:
    return [signature, {"encoding": encoding, "commitment": commitment,
                        "maxSupportedTransactionVersion": MAX_SUPPORTED_TRANSACTION_VERSION}]


# A provider that answers "method not found / not available" for a method
# doesn't offer it (plan or product limit): it is not asked again for this
# long, and the capability matrix says so.
UNSUPPORTED_METHOD_SECONDS = 6 * 3600.0
# Only messages about the METHOD count (e.g. Ankr: "the method getTransaction
# does not exist/is not available"); "account does not exist" and the like
# are about the request, not the provider's capabilities.
_METHOD_UNAVAILABLE = re.compile(r"\bmethod\b.*\b(not found|does not exist|is not available|not supported|is disabled)", re.I)
_VERSION_UNSUPPORTED = re.compile(r"transaction version \(\d+\) is not supported", re.I)

PRIORITIES = ("critical", "normal", "background")
# Background work (history scans, analytics, follow-ups) may use at most this
# many concurrent requests per endpoint and this share of its request
# budget; it is shed (never queued) when an endpoint is limited, so critical
# execution traffic is never starved by it.
BACKGROUND_MAX_INFLIGHT = 2
BACKGROUND_BUDGET_SHARE = 0.5
DEFAULT_BACKGROUND_RPS = 4.0  # across all services (shared via Redis) when no rps is configured
MAX_RETRY_AFTER_SECONDS = 600.0


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        return None


def _reason(exc: Exception, urls: list[str]) -> str:
    """Short, credential-free cause of one endpoint's failure (e.g. "HTTP 429",
    "timeout"), so "all endpoints failed" says why."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return f"connection error ({type(exc).__name__})"
    return redact_text(str(exc), urls)[:160] or type(exc).__name__


@dataclass
class _Endpoint:
    url: str
    label: str
    consecutive_failures: int = 0
    disabled_until: float | None = None  # time.monotonic() timestamp
    # Dashboard-managed providers (rpc_registry): display name, where it came
    # from, its own timeout and request budget. None = manager defaults.
    name: str | None = None
    source: str = "env"
    priority: int | None = None
    timeout: float | None = None
    rps: float | None = None
    # Health statistics (real requests only).
    successes: int = 0
    failures: int = 0
    rate_limited_count: int = 0
    rate_limited_until: float | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None  # moving average of successful calls
    _sent: list = field(default_factory=list)  # monotonic send times in the last second
    forbidden: dict = field(default_factory=dict)  # method -> monotonic time until which 401/403 is remembered
    rate_limit_streak: int = 0  # consecutive 429s: each doubles the cooldown (reset by a success)
    auth_failed_until: float | None = None  # key/URL refused (401/403): skipped for every method
    unsupported: dict = field(default_factory=dict)  # method -> monotonic time until which "method not available" is remembered
    # Capability matrix (learned from real traffic and probes):
    # method -> {"status": SUPPORTED|UNSUPPORTED|FORBIDDEN|RATE_LIMITED|ERROR, "at", "error"}
    capabilities: dict = field(default_factory=dict)
    tx_versions: dict = field(default_factory=dict)  # transaction version -> times served by getTransaction
    inflight: int = 0
    inflight_background: int = 0
    rate_limited_by_method: dict = field(default_factory=dict)  # method -> count of 429s
    forbidden_count: int = 0
    roles: tuple = ()  # provider roles (provider_roles); empty = every role

    @property
    def key(self) -> str:
        """Stable, credential-free id for shared (Redis) state."""
        return hashlib.sha256(self.url.encode()).hexdigest()[:16]

    def capable(self, method: str, now: float) -> bool:
        return (self.forbidden.get(method, 0) <= now and self.unsupported.get(method, 0) <= now
                and (self.auth_failed_until is None or self.auth_failed_until <= now))

    def cap(self, method: str, status: str, error: str | None = None) -> None:
        self.capabilities[method] = {"status": status, "at": _now_iso(), "error": error}

    def over_budget(self, now: float) -> bool:
        if not self.rps:
            return False
        self._sent = [t for t in self._sent if now - t < 1.0]
        return len(self._sent) >= self.rps


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


FailoverHook = Callable[[str | None, str, str], Awaitable[None]]


@dataclass
class RpcManager:
    """Primary -> backup -> emergency-fallback JSON-RPC routing with health
    scoring, per spec section 32: never rely on a single endpoint, and fail
    over on controlled health signals rather than random rotation.

    Health scoring: an endpoint that fails `failure_threshold` calls in a
    row is disabled for `cooldown_seconds` (skipped, not removed — it's
    re-tried after cooldown, since a transient outage shouldn't permanently
    demote a good endpoint). If every endpoint is currently in cooldown,
    that cooldown is treated as informational rather than fatal ("emergency
    fallback"): the call still goes out, in primary-first order, rather than
    failing outright — a live attempt against a recently-bad endpoint beats
    no attempt at all.
    """

    endpoints: list[_Endpoint]
    client: httpx.AsyncClient
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    timeout_seconds: float = 10.0
    _request_id: int = field(default=0, init=False)
    # The endpoint that served the last successful call, and a hook called
    # when that changes (failover or recovery).
    active_label: str | None = field(default=None, init=False)
    on_failover: FailoverHook | None = field(default=None, init=False)
    # method -> which endpoint served it last and how it went (request routing view)
    method_stats: dict = field(default_factory=dict, init=False)
    # Optional Redis (set by the runtime watcher): 429 cooldowns and the
    # background request budget are shared by every service using the same
    # provider, since the provider's limit is per key, not per process.
    shared: Any = field(default=None, init=False)
    role_fallbacks: dict = field(default_factory=dict, init=False)  # role -> requests no role holder could take

    @classmethod
    def create(
        cls,
        client: httpx.AsyncClient,
        primary_url: str,
        backup_url: str | None = None,
        emergency_url: str | None = None,
        extra_backup_urls: list[str | None] | None = None,
        **kwargs,
    ) -> "RpcManager":
        """Endpoints in failover order: primary, backup, then each extra
        backup (labelled backup2, backup3, ...), then emergency. Empty URLs
        are skipped."""
        endpoints = [_Endpoint(url=primary_url, label="primary")]
        if backup_url:
            endpoints.append(_Endpoint(url=backup_url, label="backup"))
        for i, url in enumerate((u for u in (extra_backup_urls or []) if u), start=2):
            endpoints.append(_Endpoint(url=url, label=f"backup{i}"))
        if emergency_url:
            endpoints.append(_Endpoint(url=emergency_url, label="emergency"))
        return cls(endpoints=endpoints, client=client, **kwargs)

    def _candidates(self, method: str | None = None) -> list[_Endpoint]:
        now = time.monotonic()
        allowed = [e for e in self.endpoints if method is None or e.capable(method, now)]
        if method is not None and not allowed:
            # Nothing offers the method (or every key is refused): try the
            # endpoints that are only refused, never the ones that don't have it.
            allowed = [e for e in self.endpoints if e.unsupported.get(method, 0) <= now]
        healthy = [e for e in allowed if (e.disabled_until is None or e.disabled_until <= now)
                   and (e.rate_limited_until is None or e.rate_limited_until <= now)]
        if method is not None:  # role holders first; every endpoint when none holds the role (counted)
            healthy = provider_roles.prefer(healthy, provider_roles.solana_role(method), self.role_fallbacks)
        if not healthy:
            return allowed
        # An endpoint at its own request budget is tried last, not skipped.
        return [e for e in healthy if not e.over_budget(now)] + [e for e in healthy if e.over_budget(now)]

    async def _background_candidates(self, method: str) -> list[_Endpoint]:
        """Endpoints background work may use now: capable, not cooling down
        (in this process or, via Redis, in any service), under the
        background concurrency limit and budget share. Empty = shed."""
        now = time.monotonic()
        out = []
        for e in self.endpoints:
            if not e.capable(method, now):
                continue
            if (e.disabled_until and e.disabled_until > now) or (e.rate_limited_until and e.rate_limited_until > now):
                continue
            if e.inflight_background >= BACKGROUND_MAX_INFLIGHT:
                continue
            if e.rps:
                recent = [t for t in e._sent if now - t < 1.0]
                if len(recent) >= max(1.0, e.rps * BACKGROUND_BUDGET_SHARE):
                    continue
            out.append(e)
        out = provider_roles.prefer(out, provider_roles.solana_role(method), self.role_fallbacks)
        if out and self.shared is not None:
            try:
                keys = [f"yx:rpc:rl:{e.key}" for e in out]
                limited = await self.shared.mget(keys)
                out = [e for e, flag in zip(out, limited) if not flag]
                kept = []
                second = int(time.time())
                for e in out:
                    budget = max(1.0, (e.rps or DEFAULT_BACKGROUND_RPS * 2) * BACKGROUND_BUDGET_SHARE)
                    k = f"yx:rpc:bg:{e.key}:{second}"
                    n = await self.shared.incr(k)
                    if n == 1:
                        await self.shared.expire(k, 3)
                    if n <= budget:
                        kept.append(e)
                out = kept
            except Exception as exc:  # noqa: BLE001 - shared state is advisory
                log.debug("rpc.shared_state_unavailable", error=type(exc).__name__)
        return out

    def cooling_down(self, method: str | None = None) -> float | None:
        """Seconds until the first endpoint that may serve `method` leaves its
        429 cooldown, when every such endpoint is rate-limited; else None."""
        now = time.monotonic()
        usable = [e for e in self.endpoints if method is None or e.forbidden.get(method, 0) <= now]
        if not usable or any(e.rate_limited_until is None or e.rate_limited_until <= now for e in usable):
            return None
        return min(e.rate_limited_until for e in usable) - now

    def replace_endpoints(self, specs: list[dict[str, Any]]) -> None:
        """Swaps the endpoint list at runtime (dashboard change), keeping the
        health statistics of endpoints whose URL is unchanged. `specs` are
        dicts with url, label and optionally name/source/priority/timeout/rps,
        in failover order. An empty list is ignored (never leave no RPC)."""
        if not specs:
            return
        by_url = {e.url: e for e in self.endpoints}
        new = []
        for spec in specs:
            e = by_url.get(spec["url"]) or _Endpoint(url=spec["url"], label=spec["label"])
            e.label = spec["label"]
            e.name = spec.get("name")
            e.source = spec.get("source", "env")
            e.priority = spec.get("priority")
            e.timeout = float(spec["timeout"]) if spec.get("timeout") else None
            e.rps = float(spec["rps"]) if spec.get("rps") else None
            e.roles = tuple(spec.get("roles") or ())
            new.append(e)
        self.endpoints = new

    async def call(self, method: str, params: list | None = None, priority: str = "normal") -> dict:
        """`priority`: "critical" (trade execution: tried on every usable
        endpoint, never shed), "normal" (decisions), "background" (history,
        analytics, follow-ups: shed with RpcRateLimitedError instead of
        queueing whenever the providers are limited, see
        _background_candidates)."""
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or []}

        last_error: Exception | None = None
        reasons: list[str] = []
        unsupported_only = True
        urls = [e.url for e in self.endpoints]
        if priority == "background":
            candidates = await self._background_candidates(method)
            if not candidates:
                raise RpcRateLimitedError(f"background {method} shed: every endpoint is limited, cooling down or busy")
        else:
            candidates = self._candidates(method)
            if not candidates:
                raise RpcMethodUnsupportedError(f"no configured RPC endpoint offers {method}")
        for endpoint in candidates:
            started = time.monotonic()
            endpoint._sent.append(started)
            endpoint.inflight += 1
            if priority == "background":
                endpoint.inflight_background += 1
            try:
                response = await self.client.post(endpoint.url, json=payload, timeout=endpoint.timeout or self.timeout_seconds)
                if response.status_code == 429:
                    endpoint.rate_limited_count += 1
                    endpoint.rate_limit_streak += 1
                    endpoint.rate_limited_by_method[method] = endpoint.rate_limited_by_method.get(method, 0) + 1
                    # A provider still limiting after its cooldown gets a longer
                    # rest instead of being hit again every 30 s; its own
                    # Retry-After wins when it sends one.
                    backoff = min(self.cooldown_seconds * 2 ** (endpoint.rate_limit_streak - 1), MAX_RATE_LIMIT_BACKOFF)
                    retry_after = _retry_after(response)
                    if retry_after is not None:
                        backoff = max(retry_after, 1.0)
                    endpoint.rate_limited_until = time.monotonic() + backoff
                    endpoint.cap(method, "RATE_LIMITED", "HTTP 429")
                    await self._share_cooldown(endpoint, backoff)
                elif response.status_code in (401, 403):
                    t = time.monotonic()
                    endpoint.forbidden_count += 1
                    endpoint.forbidden[method] = t + FORBIDDEN_METHOD_SECONDS
                    endpoint.cap(method, "FORBIDDEN", f"HTTP {response.status_code}")
                    refused = [m for m, until in endpoint.forbidden.items() if until > t]
                    if method in CORE_METHODS or len(refused) >= AUTH_FAILED_AFTER_METHODS:
                        endpoint.auth_failed_until = t + FORBIDDEN_METHOD_SECONDS
                    log.warning("rpc.method_forbidden", endpoint=endpoint.label, method=method, status=response.status_code,
                                auth_failed=endpoint.auth_failed_until is not None, skip_seconds=FORBIDDEN_METHOD_SECONDS)
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    err = body["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    message = str(err.get("message", "")) if isinstance(err, dict) else str(err)
                    if code == UNSUPPORTED_TX_VERSION_CODE or _VERSION_UNSUPPORTED.search(message):
                        raise RpcUnsupportedTransactionVersionError(f"RPC error from {endpoint.label}: {err}")
                    if code == -32601 or _METHOD_UNAVAILABLE.search(message):
                        endpoint.unsupported[method] = time.monotonic() + UNSUPPORTED_METHOD_SECONDS
                        endpoint.cap(method, "UNSUPPORTED", message[:160])
                        raise RpcRequestError(f"RPC error from {endpoint.label}: {err}")
                    cls = RpcRequestError if code in REQUEST_ERROR_CODES else RuntimeError
                    raise cls(f"RPC error from {endpoint.label}: {err}")
                endpoint.consecutive_failures = 0
                endpoint.disabled_until = None
                endpoint.rate_limited_until = None
                endpoint.rate_limit_streak = 0
                endpoint.auth_failed_until = None
                endpoint.forbidden.pop(method, None)
                endpoint.unsupported.pop(method, None)
                endpoint.cap(method, "SUPPORTED")
                result = body["result"]
                if method == "getTransaction" and isinstance(result, dict):
                    v = str(result.get("version", "legacy"))
                    endpoint.tx_versions[v] = endpoint.tx_versions.get(v, 0) + 1
                endpoint.successes += 1
                self._note(method, endpoint.label, ok=True)
                endpoint.last_success_at = _now_iso()
                ms = (time.monotonic() - started) * 1000
                endpoint.latency_ms = round(ms if endpoint.latency_ms is None else endpoint.latency_ms * 0.8 + ms * 0.2, 1)
                if self.active_label != endpoint.label:
                    # Before any success, a call that had to skip failing
                    # higher-priority endpoints is a failover too.
                    previous = self.active_label or (reasons[0].split(": ", 1)[0] if reasons else None)
                    self.active_label = endpoint.label
                    await self._failover(previous, endpoint.label, "; ".join(reasons))
                return result
            except RpcUnsupportedTransactionVersionError:
                # Deterministic: the transaction exists, our declared max version is lower.
                endpoint.cap(method, "SUPPORTED")
                self._note(method, endpoint.label, ok=False, error=f"{endpoint.label}: transaction version above "
                                                                   f"{MAX_SUPPORTED_TRANSACTION_VERSION}")
                raise
            except RpcRequestError as exc:
                # The endpoint is healthy; another endpoint may still support the request.
                last_error = exc
                reasons.append(f"{endpoint.label}: {_reason(exc, urls)}")
                if endpoint.unsupported.get(method, 0) <= time.monotonic():
                    unsupported_only = False
                log.info("rpc.call.rejected", endpoint=endpoint.label, method=method,
                         error=redact_text(str(exc), [e.url for e in self.endpoints]))
                continue
            except Exception as exc:  # noqa: BLE001 - any transport/parse/RPC failure triggers failover
                unsupported_only = False
                last_error = exc
                reasons.append(f"{endpoint.label}: {_reason(exc, urls)}")
                endpoint.failures += 1
                endpoint.last_failure_at = _now_iso()
                endpoint.last_error = reasons[-1].split(": ", 1)[-1]
                if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in (401, 403, 429):
                    endpoint.cap(method, "ERROR", endpoint.last_error[:160])
                if endpoint.auth_failed_until is not None:
                    endpoint.last_error = f"AUTHENTICATION_FAILED ({endpoint.last_error}): check the provider key/URL and plan"
                self._note(method, endpoint.label, ok=False, error=reasons[-1])
                endpoint.consecutive_failures += 1
                log.warning(
                    "rpc.call.failed",
                    endpoint=endpoint.label,
                    method=method,
                    priority=priority,
                    consecutive_failures=endpoint.consecutive_failures,
                    error=redact_text(str(exc), [e.url for e in self.endpoints]),
                )
                if endpoint.consecutive_failures >= self.failure_threshold:
                    endpoint.disabled_until = time.monotonic() + self.cooldown_seconds
                    log.warning("rpc.endpoint.disabled", endpoint=endpoint.label, cooldown_seconds=self.cooldown_seconds)
                continue
            finally:
                endpoint.inflight -= 1
                if priority == "background":
                    endpoint.inflight_background -= 1

        detail = "; ".join(reasons)
        if unsupported_only and reasons:
            # A capability gap, not an outage: no alert, the matrix shows it.
            raise RpcMethodUnsupportedError(f"no configured RPC endpoint offers {method} ({detail})") from last_error
        if priority == "background":
            # Optional work: the caller records "unavailable"; no alert storm.
            raise RpcRateLimitedError(f"background {method} failed on every usable endpoint ({detail})") from last_error
        await alert_error("solana-rpc", "rpc.all_endpoints_failed", f"method={method} ({detail})")
        raise RpcAllEndpointsFailedError(f"All RPC endpoints failed for method={method} ({detail})") from last_error

    async def _share_cooldown(self, endpoint: _Endpoint, seconds: float) -> None:
        if self.shared is None:
            return
        try:
            await self.shared.set(f"yx:rpc:rl:{endpoint.key}", "1", ex=max(1, int(seconds)))
        except Exception as exc:  # noqa: BLE001 - shared state is advisory
            log.debug("rpc.shared_state_unavailable", error=type(exc).__name__)

    async def _failover(self, previous: str | None, current: str, reasons: str) -> None:
        if previous is None:
            return  # first successful call, not a failover
        log.warning("rpc.failover", previous=previous, active=current, reasons=reasons)
        if self.on_failover is not None:
            try:
                await self.on_failover(previous, current, reasons)
            except Exception as exc:  # noqa: BLE001 - reporting must never break a call
                log.warning("rpc.failover_hook_failed", error=type(exc).__name__)

    def _note(self, method: str, label: str, ok: bool, error: str | None = None) -> None:
        st = self.method_stats.setdefault(method, {"provider": None, "ok": 0, "fail": 0, "last_error": None, "at": None})
        st["ok" if ok else "fail"] += 1
        st["at"] = _now_iso()
        if ok:
            st["provider"] = label
        else:
            st["last_error"] = error

    def method_snapshot(self) -> list[dict]:
        """Which endpoint serves each request type (request routing view)."""
        return [{"method": m, **v} for m, v in sorted(self.method_stats.items())]

    def health_snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "label": e.label,
                "url": redact_url(e.url),
                "consecutive_failures": e.consecutive_failures,
                "disabled": e.disabled_until is not None and e.disabled_until > now,
                "name": e.name, "source": e.source, "priority": e.priority,
                "rate_limited": e.rate_limited_until is not None and e.rate_limited_until > now,
                "rate_limited_count": e.rate_limited_count, "successes": e.successes, "failures": e.failures,
                "success_rate": round(e.successes / (e.successes + e.failures), 4) if (e.successes + e.failures) else None,
                "latency_ms": e.latency_ms, "last_success_at": e.last_success_at, "last_failure_at": e.last_failure_at,
                "last_error": e.last_error, "active": e.label == self.active_label,
                "forbidden_methods": sorted(m for m, until in e.forbidden.items() if until > now),
                "unsupported_methods": sorted(m for m, until in e.unsupported.items() if until > now),
                "auth_failed": e.auth_failed_until is not None and e.auth_failed_until > now,
                "capabilities": dict(e.capabilities), "tx_versions": dict(e.tx_versions),
                "rate_limited_by_method": dict(e.rate_limited_by_method), "forbidden_count": e.forbidden_count,
                "inflight": e.inflight, "roles": list(e.roles),
            }
            for e in self.endpoints
        ]


async def call_optional(rpc: Any, method: str, params: list | None = None) -> Any:
    """For lookups that only enrich an analysis and whose absence is already
    reported as "unavailable" (early-buyer funding). While every endpoint
    is cooling down after HTTP 429 the request is not sent at all, so
    optional analysis never extends a provider's rate limit or floods the
    alerts. Calls a trade depends on use rpc.call, which still tries every
    endpoint."""
    wait = rpc.cooling_down(method) if isinstance(rpc, RpcManager) else None
    if wait is not None:
        raise RpcRateLimitedError(f"all RPC endpoints are rate-limited (HTTP 429) for {wait:.0f}s more; {method} skipped")
    if isinstance(rpc, RpcManager):
        return await rpc.call(method, params, priority="background")
    return await rpc.call(method, params)


class _PriorityRpc:
    """`rpc` with every call made at a fixed priority (the executor uses
    "critical"); other attributes pass through. Test doubles without a
    priority parameter are called unchanged."""

    def __init__(self, inner: Any, priority: str):
        self._inner, self._priority = inner, priority

    async def call(self, method: str, params: list | None = None, priority: str | None = None) -> Any:
        if isinstance(self._inner, (RpcManager, _PriorityRpc)):
            return await self._inner.call(method, params, priority=priority or self._priority)
        return await self._inner.call(method, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def with_priority(rpc: Any, priority: str) -> Any:
    if rpc is None:
        return None
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}")
    return _PriorityRpc(rpc, priority)
