"""Exchange connections: save (encrypted), describe (never the key), test
(read-only requests only) and assess least privilege.

A test proves three things and records them on the row:
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
from yonixalpha_crypto.private import environments, livelock, secretstore
from yonixalpha_crypto.private.environments import LIVE, PRACTICE
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError, SignedHttp

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


async def test(session: AsyncSession, c: ApiConnection, *, transport: httpx.AsyncBaseTransport | None = None,
               env: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read-only connection test. Updates the row and returns the outcome."""
    now = now or _now()
    key, secret = secretstore.decrypt(c.key_enc, env), secretstore.decrypt(c.secret_enc, env)
    c.checked_at = now
    if key is None or secret is None:
        c.status, c.last_error = "NEEDS_REENTRY", "saved keys cannot be decrypted (the encryption key changed)"
        await _event(session, "WARN", f"connection test: keys unreadable ({c.exchange} {c.environment})", {"id": str(c.id)})
        return {"ok": False, "status": c.status, "error": c.last_error}
    http = SignedHttp(c.exchange, c.environment, key, secret, connection=view(c), transport=transport)
    perms: dict[str, Any]
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
    except ExchangeRequestError as e:
        c.status, c.last_error = "FAILED", str(e)
        await _event(session, "WARN", f"connection test failed: {c.exchange} {c.environment} '{c.label}'",
                     {"id": str(c.id), "error": str(e)})
        return {"ok": False, "status": c.status, "error": c.last_error}
    finally:
        await http.aclose()
    c.status, c.last_error, c.last_ok_at, c.permissions = "OK", None, now, perms
    await _event(session, "INFO", f"connection test OK: {c.exchange} {c.environment} '{c.label}'",
                 {"id": str(c.id), "withdraw_enabled": perms.get("withdraw_enabled"),
                  "ip_restricted": perms.get("ip_restricted")})
    return {"ok": True, "status": c.status, "permissions": perms,
            "live_gates": livelock.gates_json(view(c), now) if c.environment == LIVE else None}
