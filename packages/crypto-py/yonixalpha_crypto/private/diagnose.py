"""What an exchange failure means, in plain words, without guessing.

A connection test runs separate checks (connections.test):
- public_market_data: an UNSIGNED request to the environment's own host. It
  proves only that this server can reach that host;
- account_auth: a signed account read. It proves the key and signature work;
- order_validation: practice environments only, Binance's test-order
  endpoint (validated by the exchange, never placed). LIVE: never sent;
- live_orders: always "locked" (livelock).

classify() maps one failure to a state from the HTTP status and the
exchange's own error code, never from the wording alone:
- HTTP 451 -> geo_restricted: the exchange refuses this server's location.
  It is not a key problem, and nothing tries to get around it (no VPN,
  proxy, other host or demo -> live fallback);
- HTTP 403 -> forbidden: refused by the exchange or its firewall (Bybit's
  CloudFront location block answers 403). Not proven to be a key problem;
- HTTP 429 / 418 -> rate_limited;
- HTTP 401, or an authentication code (Binance -2014 / -2015 / -1022,
  Bybit 10003 / 10004 / 10005 / 33004) -> auth_failed;
- no answer -> timeout or unreachable;
- an answer that is not JSON on a non-error status -> malformed;
- HTTP 5xx -> exchange_error; anything else -> error.
Messages carry the host, status and the exchange's code, never a key.
"""

from __future__ import annotations

from typing import Any

from yonixalpha_crypto.private.signedhttp import BlockedRequest, ExchangeRequestError

OK = "ok"
GEO = "geo_restricted"
FORBIDDEN = "forbidden"
RATE = "rate_limited"
AUTH = "auth_failed"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
MALFORMED = "malformed"
EXCHANGE_ERROR = "exchange_error"
REJECTED = "rejected"
ERROR = "error"
BLOCKED_LOCALLY = "blocked_locally"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"
NOT_CHECKED = "not_checked"
LOCKED = "locked"

AUTH_CODES = {"binance": {-2014, -2015, -1022}, "bybit": {10003, 10004, 10005, 33004}}
# The host could not be used at all: later checks on it are skipped.
HOST_UNUSABLE = {GEO, FORBIDDEN, TIMEOUT, UNREACHABLE}


def result(state: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "ok": state == OK, "message": message, **extra}


def classify(exc: Exception, exchange: str, host: str) -> dict[str, Any]:
    if isinstance(exc, BlockedRequest):
        return result(BLOCKED_LOCALLY, f"refused by YonixAlpha before sending: {exc}")
    if not isinstance(exc, ExchangeRequestError):
        return result(ERROR, f"{type(exc).__name__} while contacting {host}")
    status, code = exc.status, exc.code
    detail = {"http_status": status, "code": code, "host": host, "exchange_message": exc.message[:200]}
    if status == 451:
        return result(GEO, f"{exchange} refused requests from this server's location (HTTP 451 from {host}). "
                           "This is the exchange's location restriction, not a key or password problem. "
                           "YonixAlpha does not use VPNs, proxies or other hosts to get around it.", **detail)
    if status == 403:
        return result(FORBIDDEN, f"HTTP 403 from {host}: the exchange or its firewall refused this server "
                                 "(often a location block). Not proven to be a key problem.", **detail)
    if status in (429, 418):
        return result(RATE, f"rate limited by {host} (HTTP {status}); wait before testing again.", **detail)
    if status == 401 or code in AUTH_CODES.get(exchange, set()):
        return result(AUTH, f"the exchange rejected the key or its signature (HTTP {status}, code {code}): "
                            f"{exc.message[:160]}. Check the key, its permissions and its IP restriction.", **detail)
    if status is None:
        if "Timeout" in exc.message:
            return result(TIMEOUT, f"no answer from {host} in time ({exc.message}).", **detail)
        return result(UNREACHABLE, f"could not connect to {host} ({exc.message}).", **detail)
    if status < 400 and code is None:
        return result(MALFORMED, f"{host} answered HTTP {status} with a body that is not valid JSON.", **detail)
    if status < 400:  # a valid answer carrying the exchange's own error code (Bybit retCode)
        return result(ERROR, f"{host} refused the request: code {code}: {exc.message[:160]}", **detail)
    if status >= 500:
        return result(EXCHANGE_ERROR, f"{host} had a server error (HTTP {status}); try again later.", **detail)
    return result(ERROR, f"{host} answered HTTP {status}, code {code}: {exc.message[:160]}", **detail)
