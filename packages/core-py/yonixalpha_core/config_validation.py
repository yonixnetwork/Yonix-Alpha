"""Per-module configuration validation.

`.env` holds secrets and infrastructure (keys, URLs, locks); the database
holds what an operator tunes (modes, risk limits, strategy parameters).
This module checks, for every module, that the `.env` side it needs is
present and well-formed:

- a module that is OFF is DISABLED and never blocks anything;
- an enabled module whose PAPER requirements are missing (e.g. no Solana
  RPC for the Pump.fun engines) is
  CONFIGURATION_ERROR — it cannot run at all;
- a module that is asked to trade live (global mode LIVE and the module
  AUTO/MANUAL) and lacks its LIVE requirements is CONFIGURATION_ERROR too;
- otherwise READY (with `live_missing` listing what LIVE would still need).

Modules in CONFIGURATION_ERROR cannot be switched to AUTO/MANUAL while the
global mode is LIVE, nor can the global mode go LIVE while an AUTO/MANUAL
module lacks its live configuration (apps/api control routes). Messages
name variables, never their values.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from yonixalpha_core.safety.models import GlobalMode, StrategyMode

REDIS_KEY = "yx:config:validation"
DISABLED, READY, CONFIG_ERROR = "DISABLED", "READY", "CONFIGURATION_ERROR"


def _v(settings: Any, name: str) -> str:
    raw = getattr(settings, name, None)
    if raw is None:
        return ""
    return (raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)).strip()


Check = Callable[[Any], list[str]]


def need(*names: str) -> Check:
    def check(s: Any) -> list[str]:
        return [f"{n} is not set" for n in names if not _v(s, n)]
    return check


def _solana_wallet(s: Any) -> list[str]:
    from yonixalpha_core.solana.wallet import WalletError, load_wallet

    try:
        w = load_wallet(s)
    except WalletError as exc:
        return [str(exc)]
    return [] if w is not None else ["WALLET_PRIVATE_KEY is not set"]


@dataclass
class Module:
    name: str
    label: str
    mode_keys: tuple[str, ...]  # strategy/venue mode keys that switch it on (most restrictive wins)
    paper: Check = field(default=lambda s: [])
    live: Check | None = None  # None: no live path in this module
    warn: Callable[[Any], list[str]] = field(default=lambda s: [])


def _solana_paper(s: Any) -> list[str]:
    return need("SOLANA_RPC_URL", "SOLANA_WS_URL")(s)


def modules_for(strategy_venues: dict[str, str] | None = None) -> list[Module]:
    """The Solana modules (BSC / Robinhood Chain are paper-only and have no
    .env requirement beyond optional RPC URLs)."""
    return [
        Module("solana_fresh", "Pump.fun fresh tokens", ("solana_fresh",), _solana_paper, _solana_wallet),
        Module("solana_migration", "Pump.fun migrated tokens (PumpSwap)", ("solana_migration",), _solana_paper, _solana_wallet),
        Module("solana_momentum", "Solana momentum", ("solana_momentum",), _solana_paper, _solana_wallet),
    ]


def _alerts(s: Any) -> list[str]:
    tok, chat = _v(s, "TELEGRAM_BOT_TOKEN"), _v(s, "TELEGRAM_CHAT_ID")
    if bool(tok) != bool(chat):
        return ["TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together"]
    return []


def validate(settings: Any, modes: dict[str, StrategyMode], global_mode: GlobalMode,
             strategy_venues: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    from yonixalpha_core.safety.pipeline import effective_mode
    from yonixalpha_core.safety.store import live_trading_permitted

    out: dict[str, dict[str, Any]] = {}
    for m in modules_for(strategy_venues or {}):
        mode = effective_mode(*(modes.get(k, StrategyMode.PAPER) for k in m.mode_keys))
        paper_errs = m.paper(settings)
        live_errs = m.live(settings) if m.live else ["no live execution path"]
        wants_live = global_mode == GlobalMode.LIVE and mode in (StrategyMode.AUTO, StrategyMode.MANUAL)
        if mode == StrategyMode.OFF:
            status, errors = DISABLED, []
        else:
            errors = paper_errs + (live_errs if wants_live else [])
            status = CONFIG_ERROR if errors else READY
        out[m.name] = {"label": m.label, "status": status, "mode": mode.value, "mode_keys": list(m.mode_keys), "errors": errors,
                       "live_missing": live_errs, "live_ready": not live_errs and not paper_errs,
                       "warnings": m.warn(settings) if wants_live else [],
                       "live_locks_open": live_trading_permitted(settings)}
    for name, label, check in (("alerts", "Telegram alerts", _alerts),):
        errs = check(settings)
        out[name] = {"label": label, "status": CONFIG_ERROR if errs else READY, "mode": None, "mode_keys": [], "errors": errs,
                     "live_missing": [], "live_ready": None, "warnings": [], "live_locks_open": None}
    return out


async def load_and_validate(session, settings: Any) -> dict[str, dict[str, Any]]:
    from yonixalpha_core.safety import store
    from yonixalpha_core.strategies.catalog import MODE_KEYS

    modes = {k: await store.load_strategy_mode(session, k) for k in MODE_KEYS}
    return validate(settings, modes, await store.load_global_mode(session))


async def store_result(redis, result: dict) -> None:
    import json
    from datetime import datetime, timezone

    if redis is not None:
        await redis.set(REDIS_KEY, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "modules": result}), ex=3600)


def blocking_errors(result: dict[str, dict[str, Any]], module: str | None = None) -> list[str]:
    """CONFIGURATION_ERROR messages (of one module, or of all)."""
    items = [(module, result[module])] if module else result.items()
    return [f"{name}: {e}" for name, r in items if r and r["status"] == CONFIG_ERROR for e in r["errors"]]
