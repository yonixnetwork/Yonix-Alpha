"""Live trading activation status: can the global mode go LIVE, and would a
LIVE order actually be created once it is? One answer for the dashboard,
the API and the diagnostics tool, built from the same checks the gate and
the order worker use (nothing here opens a lock or changes a mode).

Two kinds of blocker:
  blocks_activation  the API refuses the switch to LIVE (PUT /modes/global)
  blocks_orders      the switch is accepted, but no LIVE order is created
                     until it is fixed (the decision goes to PAPER or waits)

Each blocker has a `category` the UI shows in plain words:
  LOCKED_BY_SAFETY_POLICY  .env locks closed, kill switch, entry switches
  WALLET_NOT_CONFIGURED    WALLET_PRIVATE_KEY missing
  SIGNING_UNAVAILABLE      key present but unreadable, or the worker has none
  PROVIDER_DISCONNECTED    Solana RPC missing, order worker not reporting
  STRATEGY_DISABLED        no Solana strategy in AUTO or MANUAL
  CONFIGURATION_ERROR      a module that would trade live lacks its .env
  INSUFFICIENT_BALANCE     wallet not synced or at/below the reserve
  STALE_DEPLOYMENT         the API and the order worker read different locks
                           (a container was not recreated after editing .env)

States:
  LOCKED            the switch to LIVE is refused (blocks_activation)
  READY_TO_ACTIVATE every check passes, the global mode is not LIVE yet
  ACTIVE_NOT_READY  global mode LIVE, but blocks_orders stop live orders
  ACTIVE            global mode LIVE and every check passes: decisions that
                    pass the gate are sent to the live wallet. It does not
                    mean a trade is due: `eligible_recent` says how many
                    decisions were executable on LIVE in the last hour.

No secret is read into the result: wallet errors name variables only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import config_validation, kill_switch, live_trading
from yonixalpha_core.chains import controls
from yonixalpha_core.safety import store
from yonixalpha_core.solana.wallet import wallet_status

ENGINES = ("solana_fresh", "solana_migration", "solana_momentum")
ELIGIBLE_SQL = """
SELECT count(*) FROM risk_assessments
WHERE evaluated_at >= :since AND engine LIKE 'solana%' AND executable AND execution_target = 'LIVE'
"""


@dataclass
class Blocker:
    code: str
    category: str
    message: str
    action: str
    blocks_activation: bool = False


def _locks(settings: Any) -> dict[str, bool]:
    return {"TRADING_ENABLED": bool(getattr(settings, "TRADING_ENABLED", False)),
            "LIVE_TRADING_ENABLED": bool(getattr(settings, "LIVE_TRADING_ENABLED", False)),
            "PAPER_TRADING": bool(getattr(settings, "PAPER_TRADING", True))}


def lock_blockers(settings: Any) -> list[Blocker]:
    locks = _locks(settings)
    closed = [k for k in ("TRADING_ENABLED", "LIVE_TRADING_ENABLED") if not locks[k]]
    if locks["PAPER_TRADING"]:
        closed.append("PAPER_TRADING")
    if not closed:
        return []
    want = ", ".join(f"{k}={'false' if k == 'PAPER_TRADING' else 'true'}" for k in closed)
    return [Blocker("ENV_LOCKS_CLOSED", "LOCKED_BY_SAFETY_POLICY",
                    f"live trading is locked on the server: {want} is required in .env (now "
                    + ", ".join(f"{k}={str(locks[k]).lower()}" for k in closed) + ")",
                    "The locks are set only on the server, in .env, and need your explicit decision. After editing "
                    ".env, recreate the containers (scripts/deploy.sh or docker compose up -d), not just restart them.",
                    blocks_activation=True)]


def worker_blockers(settings: Any, worker: dict | None, wallet_sync: dict | None, reserve: Decimal | None,
                    now: datetime) -> list[Blocker]:
    """The order worker (paper-trading) as it reports itself in Redis."""
    out: list[Blocker] = []
    permitted = store.live_trading_permitted(settings)
    if worker is None:
        return [Blocker("WORKER_NOT_RUNNING", "PROVIDER_DISCONNECTED",
                        "the live order worker (paper-trading service) is not reporting",
                        "Check that the paper-trading container is running (docker compose ps).")]
    status, reason = worker.get("status"), str(worker.get("reason") or "")
    if status != "ready":
        if permitted and "environment locks closed" in reason:
            out.append(Blocker("STALE_DEPLOYMENT", "STALE_DEPLOYMENT",
                               "the API reads the live locks as open, but the order worker still reads them as closed",
                               "The paper-trading container was started before .env changed. Recreate it "
                               "(scripts/deploy.sh or docker compose up -d paper-trading)."))
        elif "WALLET_PRIVATE_KEY" in reason or "wallet" in reason.lower():
            out.append(Blocker("WORKER_NO_SIGNER", "SIGNING_UNAVAILABLE", f"the order worker cannot sign: {reason}",
                               "Set the wallet key on the server (scripts/set-keys.sh), then recreate the containers."))
        elif "RPC" in reason:
            out.append(Blocker("WORKER_NO_RPC", "PROVIDER_DISCONNECTED", f"the order worker has no Solana RPC: {reason}",
                               "Configure a Solana RPC (Settings: providers, or SOLANA_RPC_URL)."))
        elif permitted:
            out.append(Blocker("WORKER_NOT_READY", "PROVIDER_DISCONNECTED",
                               f"the order worker is {status}" + (f": {reason}" if reason else ""),
                               "See Live Execution: preflight, and the paper-trading logs."))
        return out
    if wallet_sync is None:
        out.append(Blocker("WALLET_NOT_SYNCED", "INSUFFICIENT_BALANCE", "the wallet balance has not been synced yet",
                           "Wait for the worker's first reconciliation (about 30 s)."))
        return out
    try:
        age = (now - datetime.fromisoformat(str(wallet_sync["at"]))).total_seconds()
    except (KeyError, ValueError):
        age = None
    max_age = float(worker.get("wallet_max_age_seconds", 120))
    if age is None or age > max_age:
        out.append(Blocker("WALLET_SYNC_STALE", "PROVIDER_DISCONNECTED",
                           f"the wallet balance is {'of unknown age' if age is None else f'{age:.0f} s old'} (limit {max_age:.0f} s)",
                           "The worker cannot read the wallet from the RPC: check the Solana RPC provider."))
    res = reserve if reserve is not None else Decimal(str(worker.get("min_sol_reserve", "0.05")))
    try:
        sol = Decimal(str(wallet_sync.get("sol")))
    except Exception:  # noqa: BLE001 - an unreadable stored balance is treated as unknown
        sol = None
    if sol is not None and sol <= res:
        out.append(Blocker("BALANCE_AT_RESERVE", "INSUFFICIENT_BALANCE",
                           f"the wallet holds {sol} SOL, at or below the {res} SOL reserve that is never spent",
                           "Add SOL to the trading wallet, or lower the reserve on Live Execution (audited)."))
    return out


def evaluate(settings: Any, *, global_mode: str, strategy_modes: dict[str, str], ctl: dict, kill_engaged: bool,
             kill_reason: str | None, worker: dict | None, wallet_sync: dict | None, reserve: Decimal | None,
             config_errors: list[str], eligible_recent: int | None, now: datetime) -> dict[str, Any]:
    blockers: list[Blocker] = list(lock_blockers(settings))
    w = wallet_status(settings)
    if not w["configured"]:
        blockers.append(Blocker("WALLET_NOT_CONFIGURED", "WALLET_NOT_CONFIGURED", "no wallet key is configured on the server",
                                "Set WALLET_PRIVATE_KEY on the server with scripts/set-keys.sh (never in the dashboard).",
                                blocks_activation=False))
    elif not w["valid"]:
        blockers.append(Blocker("WALLET_KEY_INVALID", "SIGNING_UNAVAILABLE", f"the wallet key cannot be used: {w['error']}",
                                "Re-enter the key on the server with scripts/set-keys.sh."))
    if not getattr(settings, "SOLANA_RPC_URL", None):
        blockers.append(Blocker("RPC_NOT_CONFIGURED", "PROVIDER_DISCONNECTED", "no Solana RPC is configured",
                                "Set SOLANA_RPC_URL (or a Helius key) on the server, or add a provider in Settings."))
    if config_errors:
        blockers.append(Blocker("CONFIGURATION_ERROR", "CONFIGURATION_ERROR",
                                "a strategy that would trade live lacks its configuration: " + "; ".join(config_errors),
                                "Fix the named .env values on the server, or set that strategy to PAPER/OFF.",
                                blocks_activation=store.live_trading_permitted(settings)))
    # Worker checks only add information once the locks are open: with the
    # locks closed the worker is "disabled" by design.
    if store.live_trading_permitted(settings):
        blockers += worker_blockers(settings, worker, wallet_sync, reserve, now)
    elif worker is not None and worker.get("status") == "ready":
        blockers.append(Blocker("STALE_DEPLOYMENT", "STALE_DEPLOYMENT",
                                "the order worker reads the live locks as open, but the API reads them as closed",
                                "Recreate every container from the same .env (scripts/deploy.sh)."))
    if kill_engaged:
        blockers.append(Blocker("KILL_SWITCH", "LOCKED_BY_SAFETY_POLICY",
                                "the kill switch is engaged" + (f": {kill_reason}" if kill_reason else ""),
                                "Release it on the Risk page when you are ready (exits keep running either way)."))
    for key, label in (("new_entries", "New entries"), ("sniper", "Autonomous entries (sniper)"), ("chain:solana", "Solana")):
        if ctl.get(key, {}).get("enabled", True) is False:
            blockers.append(Blocker(f"SWITCH_OFF:{key}", "LOCKED_BY_SAFETY_POLICY", f"the '{label}' switch is OFF",
                                    "Turn it on under Launchpads: trading controls."))
    live_strats = [e for e, m in strategy_modes.items() if m in ("AUTO", "MANUAL")]
    if not live_strats:
        blockers.append(Blocker("NO_STRATEGY_LIVE", "STRATEGY_DISABLED",
                                "no Solana strategy is AUTO or MANUAL (PAPER and OFF never trade live)",
                                "Choose which strategy may trade live and set it to MANUAL (approve each entry) or AUTO."))
    activation = [b for b in blockers if b.blocks_activation]
    is_live = global_mode == "LIVE"
    if is_live:
        state = "ACTIVE_NOT_READY" if blockers else "ACTIVE"
    else:
        state = "LOCKED" if activation else "READY_TO_ACTIVATE"
    summary = {
        "LOCKED": "LIVE cannot be activated: " + activation[0].message if activation else "",
        "READY_TO_ACTIVATE": ("LIVE can be activated now" if not blockers else
                              "LIVE can be activated, but no live order will be placed until the items below are fixed"),
        "ACTIVE_NOT_READY": "the global mode is LIVE, but no live order will be placed until the items below are fixed",
        "ACTIVE": ("LIVE is active: decisions that pass every safety check go to the live wallet. "
                   + ("No decision was executable on LIVE in the last hour: no token currently qualifies."
                      if eligible_recent == 0 else
                      f"{eligible_recent} decision(s) were executable on LIVE in the last hour." if eligible_recent else "")),
    }[state]
    return {
        "state": state, "summary": summary, "global_mode": global_mode, "can_activate": not activation and not is_live,
        "live_orders_possible": is_live and not blockers, "locks": _locks(settings),
        "locks_open": store.live_trading_permitted(settings), "wallet": {"configured": w["configured"], "valid": w["valid"],
                                                                         "pubkey": w["pubkey"]},
        "worker": {"status": (worker or {}).get("status", "not reporting"), "reason": (worker or {}).get("reason"),
                   "at": (worker or {}).get("at")},
        "wallet_sync": {"sol": (wallet_sync or {}).get("sol"), "at": (wallet_sync or {}).get("at")} if wallet_sync else None,
        "strategies": strategy_modes, "live_strategies": live_strats, "kill_switch": kill_engaged,
        "eligible_recent": eligible_recent, "blockers": [asdict(b) for b in blockers],
        "checked_at": now.isoformat(),
    }


async def status(session: AsyncSession, redis: Any, settings: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    gm = (await store.load_global_mode(session)).value
    modes = {e: (await store.load_strategy_mode(session, e)).value for e in ENGINES}
    ctl = await controls.load(session)
    live_settings = await live_trading.load_live_settings(session)
    result = await config_validation.load_and_validate(session, settings)
    # The errors a switch to LIVE would raise: modules AUTO/MANUAL lacking live config.
    errs = [f"{name}: {e}" for name, r in result.items()
            if r.get("mode") in ("AUTO", "MANUAL") and store.live_trading_permitted(settings)
            for e in (r.get("live_missing") or [])]
    worker = wallet_sync = None
    engaged, reason = False, None
    if redis is not None:
        raw = await redis.get(live_trading.READY_KEY)
        worker = json.loads(raw) if raw else None
        raw = await redis.get(live_trading.WALLET_KEY)
        wallet_sync = json.loads(raw) if raw else None
        engaged = await kill_switch.is_engaged(redis)
        reason = await kill_switch.get_reason(redis) if engaged else None
    eligible = (await session.execute(text(ELIGIBLE_SQL), {"since": now - timedelta(hours=1)})).scalar_one()
    return evaluate(settings, global_mode=gm, strategy_modes=modes, ctl=ctl, kill_engaged=engaged, kill_reason=reason,
                    worker=worker, wallet_sync=wallet_sync, reserve=live_settings.min_sol_reserve, config_errors=errs,
                    eligible_recent=int(eligible), now=now)
