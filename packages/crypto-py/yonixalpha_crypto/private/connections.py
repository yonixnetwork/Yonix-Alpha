"""Exchange connections: save (encrypted), describe (never the key), test
(read-only requests only) and assess least privilege.

A test runs separate checks (private/diagnose.py) and records them on the
row (`checks`): public reachability of the environment's host (no key),
account authentication, practice order validation (never placed; never on
LIVE). A failure on one check is never shown as another (a 451 location
block is not reported as an authentication failure). When the key works, it
also records:
- the key authenticates (status OK / FAILED, the exchange's error scrubbed);
- what the key may do (permissions): trade spot / derivatives, withdraw,
  IP restriction, as reported by the exchange itself;
- a small balance summary, for the operator to recognise the account.
Nothing here places, changes or cancels an order.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto.db import ApiConnection, Event
from yonixalpha_crypto.private import diagnose, environments, livelock, secretstore
from yonixalpha_crypto.private.environments import LIVE, PRACTICE
from yonixalpha_crypto.private.signedhttp import BlockedRequest, ExchangeRequestError, SignedHttp

_CRED = re.compile(r"^[A-Za-z0-9_\-+/=]{8,256}$")
_LABEL = re.compile(r"^[A-Za-z0-9 _.\-]{1,48}$")


class ConnectionInputError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def view(c: ApiConnection) -> dict[str, Any]:
    """Everything the dashboard may see. Never the key, the secret or their
    encrypted values."""
    env = environments.get(c.exchange, c.environment)
    return {"id": str(c.id), "exchange": c.exchange, "environment": c.environment, "label": c.label,
            "host": env.base_url, "environment_label": env.label, "practice": c.environment in PRACTICE,
            "key_hint": c.key_hint, "enabled": c.enabled, "status": c.status, "permissions": c.permissions,
            "checked_at": c.checked_at.isoformat() if c.checked_at else None,
            "last_ok_at": c.last_ok_at.isoformat() if c.last_ok_at else None, "last_error": c.last_error,
            "checks": c.checks,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None, "updated_by": c.updated_by}


async def _event(session: AsyncSession, level: str, message: str, detail: dict[str, Any]) -> None:
    session.add(Event(at=_now(), level=level, source="connections", message=message, detail=detail))


async def create(session: AsyncSession, *, exchange: str, environment: str, label: str, api_key: str,
                 api_secret: str, actor: str, env: dict[str, str] | None = None) -> ApiConnection:
    try:
        environments.get(exchange, environment)
    except environments.UnknownEnvironment as e:
        raise ConnectionInputError(str(e)) from None
    api_key, api_secret, label = api_key.strip(), api_secret.strip(), label.strip()
    if not _LABEL.match(label):
        raise ConnectionInputError("label: 1-48 letters, digits, spaces, _ . -")
    if not _CRED.match(api_key) or not _CRED.match(api_secret):
        raise ConnectionInputError("the API key and secret must be 8-256 characters without spaces")
    if api_key == api_secret:
        raise ConnectionInputError("the API key and the secret must be different")
    fp = secretstore.fingerprint(api_key, env)
    clash = (await session.execute(select(ApiConnection).where(ApiConnection.key_fingerprint == fp))).scalar_one_or_none()
    if clash is not None:
        raise ConnectionInputError(f"this API key is already saved ({clash.exchange} {clash.environment} "
                                   f"'{clash.label}'); demo and live keys must be different keys")
    dup = (await session.execute(select(ApiConnection).where(
        ApiConnection.exchange == exchange, ApiConnection.environment == environment,
        ApiConnection.label == label))).scalar_one_or_none()
    if dup is not None:
        raise ConnectionInputError("a connection with this label already exists for this exchange and environment")
    now = _now()
    c = ApiConnection(exchange=exchange, environment=environment, label=label,
                      key_enc=secretstore.encrypt(api_key, env), secret_enc=secretstore.encrypt(api_secret, env),
                      key_fingerprint=fp, key_hint=secretstore.hint(api_key), enabled=False, status="UNTESTED",
                      created_at=now, updated_at=now, updated_by=actor)
    session.add(c)
    await _event(session, "INFO", f"connection saved: {exchange} {environment} '{label}' (disabled)",
                 {"exchange": exchange, "environment": environment, "label": label, "by": actor})
    return c


async def set_enabled(session: AsyncSession, c: ApiConnection, enabled: bool, actor: str) -> None:
    c.enabled, c.updated_at, c.updated_by = enabled, _now(), actor
    await _event(session, "WARN" if c.environment == LIVE else "INFO",
                 f"connection {'enabled' if enabled else 'disabled'}: {c.exchange} {c.environment} '{c.label}'",
                 {"id": str(c.id), "by": actor})


async def delete(session: AsyncSession, c: ApiConnection, actor: str) -> None:
    from yonixalpha_crypto.db import ExecTrade
    if (await session.execute(select(ExecTrade.id).where(ExecTrade.connection_id == c.id).limit(1))).first():
        raise ConnectionInputError("this connection has trade records: disable it instead (the history is kept)")
    await _event(session, "INFO", f"connection deleted: {c.exchange} {c.environment} '{c.label}'",
                 {"id": str(c.id), "by": actor})
    await session.delete(c)


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def assess_bybit(info: dict[str, Any] | None) -> dict[str, Any]:
    """From GET /v5/user/query-api: readOnly 0 = read-write, 1 = read-only;
    permissions grouped by product, Withdraw under Wallet; ips = bound IPs."""
    if not info:
        return {"verified": False, "withdraw_enabled": None, "ip_restricted": None, "can_trade_spot": None,
                "can_trade_derivatives": None, "read_only": None,
                "notes": ["permissions could not be read in this environment (/v5/user/query-api)"]}
    perms = info.get("permissions") or {}
    read_only = str(info.get("readOnly")) == "1"
    ips = info.get("ips")
    notes = []
    if set(perms.get("Wallet") or []) - {"Withdraw"}:
        notes.append(f"wallet permissions present: {sorted(set(perms.get('Wallet')) - {'Withdraw'})} (not needed)")
    return {"verified": True, "read_only": read_only,
            "withdraw_enabled": "Withdraw" in (perms.get("Wallet") or []),
            "ip_restricted": None if ips is None else bool(ips) and list(ips) != ["*"],
            "can_trade_spot": not read_only and bool(perms.get("Spot")),
            "can_trade_derivatives": not read_only and bool(perms.get("ContractTrade") or perms.get("Derivatives")),
            "notes": notes + ([] if ips is not None else ["IP binding not reported"])}


def assess_binance(account: dict[str, Any], restrictions: dict[str, Any] | None) -> dict[str, Any]:
    """From GET /api/v3/account (canTrade) and, on LIVE only, GET
    /sapi/v1/account/apiRestrictions (enableWithdrawals, ipRestrict, ...)."""
    if restrictions is None:
        return {"verified": False, "read_only": not account.get("canTrade", False),
                "withdraw_enabled": None, "ip_restricted": None,
                "can_trade_spot": bool(account.get("canTrade")), "can_trade_derivatives": None,
                "notes": ["key restrictions are not available in this environment (/sapi)"]}
    notes = []
    if restrictions.get("enableInternalTransfer") or restrictions.get("permitsUniversalTransfer"):
        notes.append("transfer permission is on (not needed)")
    spot = restrictions.get("enableSpotAndMarginTrading")
    if spot is None:
        spot = bool(account.get("canTrade"))
        notes.append("spot trading permission taken from the account (canTrade)")
    return {"verified": True, "read_only": not spot and not restrictions.get("enableFutures"),
            "withdraw_enabled": bool(restrictions.get("enableWithdrawals")),
            "ip_restricted": bool(restrictions.get("ipRestrict")),
            "can_trade_spot": bool(spot), "can_trade_derivatives": bool(restrictions.get("enableFutures")),
            "notes": notes}


def _balances_bybit(res: dict[str, Any] | None) -> list[dict[str, str]]:
    out = []
    for acct in (res or {}).get("list") or []:
        for coin in acct.get("coin") or []:
            if _dec(coin.get("walletBalance")) != 0:
                out.append({"asset": str(coin.get("coin")), "total": str(coin.get("walletBalance"))})
    return out[:10]


def _balances_binance(acct: dict[str, Any]) -> list[dict[str, str]]:
    out = [{"asset": str(b.get("asset")), "total": str(_dec(b.get("free")) + _dec(b.get("locked")))}
           for b in acct.get("balances") or [] if _dec(b.get("free")) + _dec(b.get("locked")) != 0]
    return out[:10]


PUBLIC_CHECK = {"binance": "/api/v3/time", "bybit": "/v5/market/time"}  # unsigned, on the environment's own host
TEST_ORDER = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "10"}  # validated, never placed


def _blank_checks(c: ApiConnection) -> dict[str, Any]:
    live = c.environment == LIVE
    return {
        "public_market_data": diagnose.result(diagnose.SKIPPED, "not run"),
        "account_auth": diagnose.result(diagnose.SKIPPED, "not run"),
        "order_validation": (diagnose.result(diagnose.LOCKED, "LIVE: no order request is ever sent (live orders are "
                                                              "locked in code)") if live else
                             diagnose.result(diagnose.SKIPPED, "not run")),
        "live_orders": diagnose.result(diagnose.LOCKED, "live orders are locked in code (LIVE_ORDERS_UNLOCKED = "
                                                        "False); no test can unlock them"),
        "host": environments.get(c.exchange, c.environment).base_url, "environment": c.environment,
        "origin": "the YonixAlpha API container on the server (not your browser)",
    }


async def test(session: AsyncSession, c: ApiConnection, *, transport: httpx.AsyncBaseTransport | None = None,
               env: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read-only connection test in separate checks (diagnose). Updates the row
    and returns the outcome. Only the environment's own fixed host is ever
    contacted: a failed DEMO / TESTNET test never falls back to LIVE."""
    now = now or _now()
    key, secret = secretstore.decrypt(c.key_enc, env), secretstore.decrypt(c.secret_enc, env)
    c.checked_at = now
    checks = _blank_checks(c)
    if key is None or secret is None:
        c.status, c.last_error = "NEEDS_REENTRY", "saved keys cannot be decrypted (the encryption key changed)"
        c.checks = checks
        await _event(session, "WARN", f"connection test: keys unreadable ({c.exchange} {c.environment})", {"id": str(c.id)})
        return {"ok": False, "status": c.status, "error": c.last_error, "checks": checks}
    host = checks["host"]
    http = SignedHttp(c.exchange, c.environment, key, secret, connection=view(c), transport=transport)
    perms: dict[str, Any] = {}
    try:
        # 1. public reachability of this environment's host (no key sent)
        try:
            await http.request("GET", PUBLIC_CHECK[c.exchange])
            checks["public_market_data"] = diagnose.result(
                diagnose.OK, f"{host} is reachable from this server (public endpoint, no key). This does not prove "
                             "the key, its permissions or the account's eligibility.")
        except (ExchangeRequestError, BlockedRequest) as e:
            checks["public_market_data"] = diagnose.classify(e, c.exchange, host)
        if checks["public_market_data"]["state"] in diagnose.HOST_UNUSABLE:
            why = f"skipped: {host} cannot be used from this server ({checks['public_market_data']['state']})"
            checks["account_auth"] = diagnose.result(diagnose.SKIPPED, why)
            if c.environment != LIVE:
                checks["order_validation"] = diagnose.result(diagnose.SKIPPED, why)
            return await _finish(session, c, checks, None, now)
        # 2. the key: a signed account read
        try:
            if c.exchange == "bybit":
                wallet = await http.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
                try:
                    info = await http.request("GET", "/v5/user/query-api")
                except ExchangeRequestError:
                    if c.environment == LIVE:
                        raise
                    info = None
                perms = assess_bybit(info)
                perms["balances"] = _balances_bybit(wallet)
            else:
                acct = await http.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
                restr = await http.request("GET", "/sapi/v1/account/apiRestrictions") if c.environment == LIVE else None
                perms = assess_binance(acct, restr)
                perms["balances"] = _balances_binance(acct)
            checks["account_auth"] = diagnose.result(diagnose.OK, "the key authenticated and the account was read")
        except (ExchangeRequestError, BlockedRequest) as e:
            checks["account_auth"] = diagnose.classify(e, c.exchange, host)
            if c.environment != LIVE:
                checks["order_validation"] = diagnose.result(diagnose.SKIPPED, "skipped: the account check failed")
            return await _finish(session, c, checks, None, now)
        # 3. practice order path: validated by the exchange, never placed (never on LIVE)
        if c.environment != LIVE:
            if c.exchange == "binance":
                try:
                    await http.request("POST", "/api/v3/order/test", TEST_ORDER)
                    checks["order_validation"] = diagnose.result(
                        diagnose.OK, "the practice order endpoint accepted a test order (validated only, nothing "
                                     "was placed)")
                except (ExchangeRequestError, BlockedRequest) as e:
                    r = diagnose.classify(e, c.exchange, host)
                    if r["state"] == diagnose.ERROR:  # e.g. a filter rule: reachable, but this order was refused
                        r = {**r, "state": diagnose.REJECTED, "ok": False}
                    checks["order_validation"] = r
            else:
                checks["order_validation"] = diagnose.result(
                    diagnose.NOT_CHECKED, "Bybit has no test-order endpoint; the practice lifecycle tool places one "
                                          "real practice order when you choose to run it")
    finally:
        await http.aclose()
    return await _finish(session, c, checks, perms, now)


async def _finish(session: AsyncSession, c: ApiConnection, checks: dict[str, Any], perms: dict[str, Any] | None,
                  now: datetime) -> dict[str, Any]:
    c.checks = checks
    pub, auth = checks["public_market_data"]["state"], checks["account_auth"]["state"]
    if perms is None:  # failed before the key was proven
        failed = checks["public_market_data"] if pub != diagnose.OK else checks["account_auth"]
        state = failed["state"]
        c.status = ("BLOCKED" if state in (diagnose.GEO, diagnose.FORBIDDEN) else
                    "UNREACHABLE" if state in (diagnose.TIMEOUT, diagnose.UNREACHABLE) else
                    "AUTH_FAILED" if state == diagnose.AUTH else "FAILED")
        c.last_error = failed["message"]
        await _event(session, "WARN", f"connection test {c.status}: {c.exchange} {c.environment} '{c.label}'",
                     {"id": str(c.id), "public_market_data": pub, "account_auth": auth,
                      "http_status": failed.get("http_status"), "code": failed.get("code"), "host": checks["host"]})
        return {"ok": False, "status": c.status, "error": c.last_error, "checks": checks}
    c.status, c.last_error, c.last_ok_at, c.permissions = "OK", None, now, perms
    await _event(session, "INFO", f"connection test OK: {c.exchange} {c.environment} '{c.label}'",
                 {"id": str(c.id), "withdraw_enabled": perms.get("withdraw_enabled"),
                  "ip_restricted": perms.get("ip_restricted"), "order_validation": checks["order_validation"]["state"]})
    return {"ok": True, "status": c.status, "permissions": perms, "checks": checks,
            "live_gates": livelock.gates_json(view(c), now) if c.environment == LIVE else None}
