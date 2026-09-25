"""Per-module configuration validation.

`.env` holds secrets and infrastructure (keys, URLs, locks); the database
holds what an operator tunes (modes, risk limits, strategy parameters).
This module checks, for every module, that the `.env` side it needs is
present and well-formed:

- a module that is OFF is DISABLED and never blocks anything;
- an enabled module whose PAPER requirements are missing (e.g. no Solana
  RPC for the Pump.fun engines, no MT5 bridge for an MT5 strategy) is
  CONFIGURATION_ERROR — it cannot run at all;
- a module that is asked to trade live (global mode LIVE and the module
  AUTO/MANUAL) and lacks its LIVE requirements is CONFIGURATION_ERROR too;
- otherwise READY (with `live_missing` listing what LIVE would still need).

Modules in CONFIGURATION_ERROR cannot be switched to AUTO/MANUAL while the
global mode is LIVE, nor can the global mode go LIVE while an AUTO/MANUAL
module lacks its live configuration (apps/api control routes). Messages
name variables, never their values.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from yonixalpha_core.safety.models import GlobalMode, StrategyMode

REDIS_KEY = "yx:config:validation"
DISABLED, READY, CONFIG_ERROR = "DISABLED", "READY", "CONFIGURATION_ERROR"
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
EVM_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
MIN_BRIDGE_TOKEN = 32


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


def _hyperliquid_live(s: Any) -> list[str]:
    errs = need("HYPERLIQUID_ACCOUNT_ADDRESS", "HYPERLIQUID_API_WALLET_PRIVATE_KEY")(s)
    addr = _v(s, "HYPERLIQUID_ACCOUNT_ADDRESS")
    if addr and not EVM_ADDRESS.match(addr):
        errs.append("HYPERLIQUID_ACCOUNT_ADDRESS must be a 0x-prefixed 40-hex-character address")
    key = _v(s, "HYPERLIQUID_API_WALLET_PRIVATE_KEY")
    if key and not EVM_KEY.match(key):
        errs.append("HYPERLIQUID_API_WALLET_PRIVATE_KEY must be a 64-hex-character private key")
    return errs


def _mt5(s: Any) -> list[str]:
    errs = need("MT5_BRIDGE_URL", "MT5_BRIDGE_TOKEN")(s)
    url = _v(s, "MT5_BRIDGE_URL")
    if url and not url.startswith(("http://", "https://")):
        errs.append("MT5_BRIDGE_URL must start with http:// or https://")
    tok = _v(s, "MT5_BRIDGE_TOKEN")
    if tok and len(tok) < MIN_BRIDGE_TOKEN:
        errs.append(f"MT5_BRIDGE_TOKEN must be at least {MIN_BRIDGE_TOKEN} characters")
    return errs


VENUE_LIVE: dict[str, Check] = {
    "binance": need("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    "bybit": need("BYBIT_API_KEY", "BYBIT_API_SECRET"),
    "hyperliquid": _hyperliquid_live,
    "mt5": _mt5,
}
VENUE_PAPER: dict[str, Check] = {"binance": lambda s: [], "bybit": lambda s: [], "hyperliquid": lambda s: [], "mt5": _mt5}
VENUE_WARN: dict[str, Callable[[Any], list[str]]] = {
    "binance": lambda s: ["BINANCE_TESTNET=true: live orders go to the Binance TESTNET"] if getattr(s, "BINANCE_TESTNET", False) else [],
    "bybit": lambda s: ["BYBIT_TESTNET=true: live orders go to the Bybit TESTNET"] if getattr(s, "BYBIT_TESTNET", False) else [],
    "hyperliquid": lambda s: ["HYPERLIQUID_TESTNET=true: live orders go to the Hyperliquid TESTNET"]
    if getattr(s, "HYPERLIQUID_TESTNET", False) else [],
    "mt5": lambda s: [],
}


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


def modules_for(strategy_venues: dict[str, str]) -> list[Module]:
    """strategy_venues: configured venue per futures strategy."""
    out = [
        Module("solana_fresh", "Pump.fun fresh tokens", ("solana_fresh",), _solana_paper, _solana_wallet),
        Module("solana_migration", "Pump.fun migrated tokens (PumpSwap)", ("solana_migration",), _solana_paper, _solana_wallet),
        Module("solana_momentum", "Solana momentum", ("solana_momentum",), _solana_paper, _solana_wallet),
    ]
    engine_for = {"binance": "binance_futures", "bybit": "bybit_futures", "hyperliquid": "hyperliquid_perps", "mt5": "mt5_fx"}
    for strategy, label in (("meta_muse", "Meta Muse Crossover"), ("gold_btc_trend", "Gold vs BTC Dual Trend"),
                            ("confluence_matrix", "Confluence Matrix")):
        venue = strategy_venues.get(strategy, "binance")
        out.append(Module(strategy, f"{label} ({venue})", (strategy, engine_for[venue]), VENUE_PAPER[venue], VENUE_LIVE[venue],
                          VENUE_WARN[venue]))
    out.append(Module("hyperliquid_grid", "Hyperliquid grid", ("hyperliquid_grid", "hyperliquid_perps"), lambda s: [],
                      _hyperliquid_live, VENUE_WARN["hyperliquid"]))
    return out


def _alerts(s: Any) -> list[str]:
    tok, chat = _v(s, "TELEGRAM_BOT_TOKEN"), _v(s, "TELEGRAM_CHAT_ID")
    if bool(tok) != bool(chat):
        return ["TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together"]
    return []


def _bots(s: Any) -> list[str]:
    from yonixalpha_core.external_bots import BOTS

    errs = []
    for b in BOTS.values():
        url, tok = _v(s, b.url_setting), _v(s, b.token_setting)
        if bool(url) != bool(tok):
            errs.append(f"{b.url_setting} and {b.token_setting} must be set together")
        if url and not url.startswith(("http://", "https://")):
            errs.append(f"{b.url_setting} must start with http:// or https://")
    return errs


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
    for name, label, check in (("alerts", "Telegram alerts", _alerts), ("control_apis", "External bot control APIs", _bots)):
        errs = check(settings)
        out[name] = {"label": label, "status": CONFIG_ERROR if errs else READY, "mode": None, "mode_keys": [], "errors": errs,
                     "live_missing": [], "live_ready": None, "warnings": [], "live_locks_open": None}
    return out


async def load_and_validate(session, settings: Any) -> dict[str, dict[str, Any]]:
    from yonixalpha_core.safety import store
    from yonixalpha_core.strategies.catalog import MODE_KEYS

    modes = {k: await store.load_strategy_mode(session, k) for k in MODE_KEYS}
    venues = {}
    for strategy in ("meta_muse", "gold_btc_trend", "confluence_matrix"):
        venues[strategy] = (await store.load_strategy_config(session, strategy)).get("venue") or "binance"
    return validate(settings, modes, await store.load_global_mode(session), venues)


async def store_result(redis, result: dict) -> None:
    import json
    from datetime import datetime, timezone

    if redis is not None:
        await redis.set(REDIS_KEY, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "modules": result}), ex=3600)


def blocking_errors(result: dict[str, dict[str, Any]], module: str | None = None) -> list[str]:
    """CONFIGURATION_ERROR messages (of one module, or of all)."""
    items = [(module, result[module])] if module else result.items()
    return [f"{name}: {e}" for name, r in items if r and r["status"] == CONFIG_ERROR for e in r["errors"]]
