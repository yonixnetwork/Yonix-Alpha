import time
from dataclasses import dataclass, field

import httpx

from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.redact import redact_text, redact_url

log = get_logger("data-solana.rpc")


class RpcAllEndpointsFailedError(Exception):
    pass


# JSON-RPC codes meaning "this request is invalid / unsupported here"
# (invalid request, method not found, invalid params). The endpoint answered,
# so they don't count against its health — e.g. Helius refusing an
# unpaginated getProgramAccounts on a huge program must not put the primary
# RPC, which every other read depends on, into cooldown.
REQUEST_ERROR_CODES = {-32600, -32601, -32602}


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

    def _candidates(self) -> list[_Endpoint]:
        now = time.monotonic()
        healthy = [e for e in self.endpoints if e.disabled_until is None or e.disabled_until <= now]
        return healthy if healthy else list(self.endpoints)

    async def call(self, method: str, params: list | None = None) -> dict:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or []}

        last_error: Exception | None = None
        reasons: list[str] = []
        urls = [e.url for e in self.endpoints]
        for endpoint in self._candidates():
            try:
                response = await self.client.post(endpoint.url, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    err = body["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    cls = RpcRequestError if code in REQUEST_ERROR_CODES else RuntimeError
                    raise cls(f"RPC error from {endpoint.label}: {err}")
                endpoint.consecutive_failures = 0
                endpoint.disabled_until = None
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

    def health_snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "label": e.label,
                "url": redact_url(e.url),
                "consecutive_failures": e.consecutive_failures,
                "disabled": e.disabled_until is not None and e.disabled_until > now,
            }
            for e in self.endpoints
        ]
