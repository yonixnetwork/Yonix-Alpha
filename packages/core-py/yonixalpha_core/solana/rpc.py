import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

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
        allowed = [e for e in self.endpoints if (method is None or e.forbidden.get(method, 0) <= now)
                   and (e.auth_failed_until is None or e.auth_failed_until <= now)]
        healthy = [e for e in allowed if (e.disabled_until is None or e.disabled_until <= now)
                   and (e.rate_limited_until is None or e.rate_limited_until <= now)]
        if not healthy:
            return allowed or list(self.endpoints)
        # An endpoint at its own request budget is tried last, not skipped.
        return [e for e in healthy if not e.over_budget(now)] + [e for e in healthy if e.over_budget(now)]

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
            new.append(e)
        self.endpoints = new

    async def call(self, method: str, params: list | None = None) -> dict:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or []}

        last_error: Exception | None = None
        reasons: list[str] = []
        urls = [e.url for e in self.endpoints]
        for endpoint in self._candidates(method):
            started = time.monotonic()
            endpoint._sent.append(started)
            try:
                response = await self.client.post(endpoint.url, json=payload, timeout=endpoint.timeout or self.timeout_seconds)
                if response.status_code == 429:
                    endpoint.rate_limited_count += 1
                    endpoint.rate_limit_streak += 1
                    # A provider still limiting after its cooldown gets a longer
                    # rest instead of being hit again every 30 s.
                    backoff = min(self.cooldown_seconds * 2 ** (endpoint.rate_limit_streak - 1), MAX_RATE_LIMIT_BACKOFF)
                    endpoint.rate_limited_until = time.monotonic() + backoff
                elif response.status_code in (401, 403):
                    t = time.monotonic()
                    endpoint.forbidden[method] = t + FORBIDDEN_METHOD_SECONDS
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
                    cls = RpcRequestError if code in REQUEST_ERROR_CODES else RuntimeError
                    raise cls(f"RPC error from {endpoint.label}: {err}")
                endpoint.consecutive_failures = 0
                endpoint.disabled_until = None
                endpoint.rate_limited_until = None
                endpoint.rate_limit_streak = 0
                endpoint.auth_failed_until = None
                endpoint.forbidden.pop(method, None)
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
                return body["result"]
            except RpcRequestError as exc:
                # The endpoint is healthy; another endpoint may still support the request.
                last_error = exc
                reasons.append(f"{endpoint.label}: {_reason(exc, urls)}")
                log.info("rpc.call.rejected", endpoint=endpoint.label, method=method,
                         error=redact_text(str(exc), [e.url for e in self.endpoints]))
                continue
            except Exception as exc:  # noqa: BLE001 - any transport/parse/RPC failure triggers failover
                last_error = exc
                reasons.append(f"{endpoint.label}: {_reason(exc, urls)}")
                endpoint.failures += 1
                endpoint.last_failure_at = _now_iso()
                endpoint.last_error = reasons[-1].split(": ", 1)[-1]
                if endpoint.auth_failed_until is not None:
                    endpoint.last_error = f"AUTHENTICATION_FAILED ({endpoint.last_error}): check the provider key/URL and plan"
                self._note(method, endpoint.label, ok=False, error=reasons[-1])
                endpoint.consecutive_failures += 1
                log.warning(
                    "rpc.call.failed",
                    endpoint=endpoint.label,
                    method=method,
                    consecutive_failures=endpoint.consecutive_failures,
                    error=redact_text(str(exc), [e.url for e in self.endpoints]),
                )
                if endpoint.consecutive_failures >= self.failure_threshold:
                    endpoint.disabled_until = time.monotonic() + self.cooldown_seconds
                    log.warning("rpc.endpoint.disabled", endpoint=endpoint.label, cooldown_seconds=self.cooldown_seconds)
                continue

        detail = "; ".join(reasons)
        await alert_error("solana-rpc", "rpc.all_endpoints_failed", f"method={method} ({detail})")
        raise RpcAllEndpointsFailedError(f"All RPC endpoints failed for method={method} ({detail})") from last_error

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
                "auth_failed": e.auth_failed_until is not None and e.auth_failed_until > now,
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
    return await rpc.call(method, params)
