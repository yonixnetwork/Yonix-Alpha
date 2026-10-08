"""System profile: which chains and features run at all (2026-10-08,
Solana-first production).

    SYSTEM_PROFILE        SOLANA_ONLY (production) | MULTI_CHAIN
    CHAIN_BSC_ENABLED     MULTI_CHAIN only; unset = on; off in SOLANA_ONLY
    CHAIN_ROBINHOOD_ENABLED  as CHAIN_BSC_ENABLED
    COPY_TRADING_ENABLED  false = copy-engine is not started
    ML_DATASET_SCOPE / ML_MODEL_SCOPE  SOLANA | ALL (unset: from the profile)

The profile controls the backend, not only the dashboard:
  - scripts/deploy.sh starts a worker only when its compose profile is on
    (scripts/compose-profiles.sh, the same rules as compose_profiles()
    below) and stops and removes the container of a worker that is off;
  - a disabled worker started by hand idles (heartbeat "disabled") instead
    of scanning;
  - the ml service skips the EVM ML cycle; the API refuses EVM orders and
    shows the disabled parts as DISABLED, not as failures.
Nothing is deleted: code, adapters, tables, history and settings stay, and
the profile is reversed by changing .env and redeploying. The Solana stack
is never stopped by the profile: it holds the live positions.
"""

from __future__ import annotations

from typing import Any

SOLANA_ONLY = "SOLANA_ONLY"
MULTI_CHAIN = "MULTI_CHAIN"
PROFILES = (SOLANA_ONLY, MULTI_CHAIN)
EVM_CHAINS = ("bsc", "robinhood")
DISABLED = "DISABLED"

# worker -> compose profile that starts it (infra/docker/docker-compose.yml)
COMPOSE_PROFILE = {"data-evm": "evm", "copy-engine": "copy"}


def profile(settings: Any) -> str:
    v = str(getattr(settings, "SYSTEM_PROFILE", None) or SOLANA_ONLY).strip().upper()
    return v if v in PROFILES else SOLANA_ONLY


def label(settings: Any) -> str:
    return f"DISABLED — {profile(settings)} MODE"


def chain_enabled(settings: Any, chain: str) -> bool:
    chain = str(chain).lower()
    if chain == "solana":
        return True
    if chain not in EVM_CHAINS or profile(settings) == SOLANA_ONLY:
        return False
    flag = getattr(settings, f"CHAIN_{chain.upper()}_ENABLED", None)
    return True if flag is None else bool(flag)


def enabled_chains(settings: Any) -> list[str]:
    return [c for c in ("solana", *EVM_CHAINS) if chain_enabled(settings, c)]


def evm_enabled(settings: Any) -> bool:
    return any(chain_enabled(settings, c) for c in EVM_CHAINS)


def copy_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "COPY_TRADING_ENABLED", False))


def ml_scope(settings: Any, kind: str = "MODEL") -> str:
    """SOLANA or ALL for ML_<kind>_SCOPE; EVM chains switched off by the
    profile are never in scope."""
    v = str(getattr(settings, f"ML_{kind}_SCOPE", None) or "").strip().upper()
    if v not in ("SOLANA", "ALL"):
        v = "SOLANA" if profile(settings) == SOLANA_ONLY else "ALL"
    return v if evm_enabled(settings) else "SOLANA"


def evm_ml_enabled(settings: Any) -> bool:
    return evm_enabled(settings) and ml_scope(settings, "MODEL") == "ALL" and ml_scope(settings, "DATASET") == "ALL"


def disabled_reason(settings: Any, what: str) -> str | None:
    """Why a worker / chain / feature is off, or None when it runs."""
    if what in ("data-evm", "evm"):
        if evm_enabled(settings):
            return None
        return (f"{label(settings)}: BSC and Robinhood are not scanned or traded"
                if profile(settings) == SOLANA_ONLY else "DISABLED: CHAIN_BSC_ENABLED and CHAIN_ROBINHOOD_ENABLED are false")
    if what in EVM_CHAINS:
        if chain_enabled(settings, what):
            return None
        return (f"{label(settings)}" if profile(settings) == SOLANA_ONLY
                else f"DISABLED: CHAIN_{what.upper()}_ENABLED=false")
    if what in ("copy-engine", "copy"):
        return None if copy_enabled(settings) else "DISABLED: COPY_TRADING_ENABLED=false"
    if what == "evm_ml":
        if evm_ml_enabled(settings):
            return None
        if not evm_enabled(settings):
            return disabled_reason(settings, "evm")
        return "DISABLED: ML_MODEL_SCOPE / ML_DATASET_SCOPE = SOLANA"
    return None


def chain_of_engine(engine: str | None) -> str:
    """The chain of a position's engine: evm_<chain> / evm_copy_<chain>
    (data-evm, copy-engine); every other engine is Solana."""
    e = str(engine or "")
    for c in EVM_CHAINS:
        if e in (f"evm_{c}", f"evm_copy_{c}"):
            return c
    return "solana"


def refusal(settings: Any, chain: str, action: str) -> dict[str, Any] | None:
    """The 409 body for an order on a chain the profile switches off."""
    why = disabled_reason(settings, chain)
    if why is None:
        return None
    return {"code": "CHAIN_DISABLED", "chain": chain, "profile": profile(settings),
            "message": f"{action} refused: {chain} is switched off ({why}). Its worker is not running, so nothing would "
                       "process the order. Open paper positions on it are kept as they are (not managed) until the "
                       "chain is switched back on."}


def compose_profiles(settings: Any) -> list[str]:
    """The compose profiles deploy.sh turns on (scripts/compose-profiles.sh
    applies the same rules to .env)."""
    return [p for svc, p in COMPOSE_PROFILE.items() if disabled_reason(settings, svc) is None]


def disabled_services(settings: Any) -> dict[str, str]:
    return {svc: why for svc in COMPOSE_PROFILE if (why := disabled_reason(settings, svc))}


def describe(settings: Any) -> dict[str, Any]:
    p = profile(settings)
    notes = []
    if p == SOLANA_ONLY and any(getattr(settings, f"CHAIN_{c.upper()}_ENABLED", None) for c in EVM_CHAINS):
        notes.append("CHAIN_BSC_ENABLED / CHAIN_ROBINHOOD_ENABLED are ignored in SOLANA_ONLY")
    if getattr(settings, "CHAIN_SOLANA_ENABLED", True) is False:
        notes.append("CHAIN_SOLANA_ENABLED=false is not applied: the Solana stack holds the live positions")
    return {"profile": p, "label": f"{p} MODE", "enabled_chains": enabled_chains(settings),
            "chains": {c: {"enabled": chain_enabled(settings, c), "reason": disabled_reason(settings, c)}
                       for c in ("solana", *EVM_CHAINS)},
            "copy_trading_enabled": copy_enabled(settings),
            "ml": {"dataset_scope": ml_scope(settings, "DATASET"), "model_scope": ml_scope(settings, "MODEL"),
                   "evm_ml": disabled_reason(settings, "evm_ml") or "ON"},
            "workers": {svc: {"enabled": why is None, "reason": why, "compose_profile": COMPOSE_PROFILE[svc]}
                        for svc in COMPOSE_PROFILE for why in [disabled_reason(settings, svc)]},
            "compose_profiles": compose_profiles(settings), "notes": notes,
            "reversible": "change SYSTEM_PROFILE / CHAIN_* / COPY_TRADING_ENABLED in .env and run scripts/deploy.sh"}


async def idle_while_disabled(settings: Any, service: str, reason: str, stop_event=None) -> None:
    """What a worker switched off by the profile does when it is started
    anyway (by hand, or an old compose file): no scanning, no database
    work, only a "disabled" heartbeat with the reason, until it is stopped."""
    import asyncio
    import signal

    from yonixalpha_core.events import heartbeat_loop
    from yonixalpha_core.logging import get_logger

    stop = stop_event or asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    get_logger(f"{service}.main").warning("service.disabled_by_profile", service=service, reason=reason)
    await heartbeat_loop(settings, service, stop, lambda: {"reason": reason, "profile": profile(settings)},
                         status="disabled")


if __name__ == "__main__":
    import json

    from yonixalpha_core.config import get_settings

    print(json.dumps(describe(get_settings()), indent=2))
