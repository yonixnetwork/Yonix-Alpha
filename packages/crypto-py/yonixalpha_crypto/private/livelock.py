"""The lock on real orders.

A request that can change a LIVE account (order, amend, cancel, protective
stop, leverage) is sent only when EVERY gate below passes. The first gate is
in code: while LIVE_ORDERS_UNLOCKED is False nothing in the database, the
dashboard or the environment can open it. Turning it on is a reviewed code
change, deployed on purpose, after the operator's written authorization.

The other gates are per connection, so a later unlock still refuses a key
that can withdraw, is not IP-restricted, cannot trade, was never tested or
is switched off. DEMO / TESTNET connections never pass: they are practice
accounts and their keys can only reach practice hosts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yonixalpha_crypto.private.environments import LIVE

LIVE_ORDERS_UNLOCKED = False
TEST_MAX_AGE = timedelta(hours=24)


class LiveLocked(RuntimeError):
    """A LIVE account change refused before anything was sent."""


@dataclass(frozen=True)
class Gate:
    id: str
    ok: bool
    detail: str


def live_gates(conn: dict[str, Any] | None, now: datetime | None = None) -> list[Gate]:
    """The gates for one connection view (connections.view)."""
    now = now or datetime.now(timezone.utc)
    gates = [Gate("code_lock", LIVE_ORDERS_UNLOCKED,
                  "unlocked in code" if LIVE_ORDERS_UNLOCKED
                  else "LIVE_ORDERS_UNLOCKED is False: real orders are locked until the operator authorizes it")]
    if conn is None:
        return gates + [Gate("connection", False, "no connection")]
    perms = conn.get("permissions") or {}
    checked = conn.get("last_ok_at")
    checked_at = datetime.fromisoformat(checked) if isinstance(checked, str) else checked
    gates += [
        Gate("environment", conn.get("environment") == LIVE, f"environment {conn.get('environment')}"),
        Gate("enabled", bool(conn.get("enabled")), "connection enabled" if conn.get("enabled") else "connection disabled"),
        Gate("credentials", conn.get("status") != "NEEDS_REENTRY", "keys readable"
             if conn.get("status") != "NEEDS_REENTRY" else "keys cannot be decrypted: enter them again"),
        Gate("tested", conn.get("status") == "OK" and checked_at is not None and now - checked_at <= TEST_MAX_AGE,
             f"last successful test {checked or 'never'} (must be OK and under 24 h old)"),
        Gate("no_withdrawal", perms.get("withdraw_enabled") is False,
             {False: "withdrawal permission is off", True: "the key can WITHDRAW: make a new key without it"}
             .get(perms.get("withdraw_enabled"), "withdrawal permission not verified")),
        Gate("ip_restricted", perms.get("ip_restricted") is True,
             {True: "key is restricted to listed IPs", False: "key is not IP-restricted"}
             .get(perms.get("ip_restricted"), "IP restriction not verified")),
        Gate("can_trade", bool(perms.get("can_trade_spot") or perms.get("can_trade_derivatives")),
             "trading permission present" if (perms.get("can_trade_spot") or perms.get("can_trade_derivatives"))
             else "the key has no trading permission (or it was not verified)"),
    ]
    return gates


def require_live_orders(conn: dict[str, Any] | None, now: datetime | None = None) -> None:
    failed = [g for g in live_gates(conn, now) if not g.ok]
    if failed:
        raise LiveLocked("live order refused: " + "; ".join(f"{g.id}: {g.detail}" for g in failed))


def gates_json(conn: dict[str, Any] | None, now: datetime | None = None) -> list[dict[str, Any]]:
    return [asdict(g) for g in live_gates(conn, now)]
