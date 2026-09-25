"""Every strategy and venue the control center manages, with its mode key,
paper account, implementation status, and config validation.

Validation is strict: unknown keys, wrong types and out-of-range values are
rejected (never coerced into something the operator did not type), and
every bound here is a sanity bound — the safety gate's own limits still
apply on top (e.g. grid leverage is capped by max_leverage at build time).
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from yonixalpha_core.strategies import confluence, gold_btc, grid, meta_muse

FUTURES_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"]
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
COIN_RE = re.compile(r"^[A-Z0-9]{1,12}$")


@dataclass(frozen=True)
class Entry:
    name: str
    label: str
    kind: str  # solana | futures | grid | analytics | venue
    engine: str | None  # safety-gate engine (risk settings scope, venue mode)
    account: str | None  # paper account name
    status: str  # what is implemented, stated plainly
    defaults: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, tuple] = field(default_factory=dict)
    has_mode: bool = True


def _dec(lo: str, hi: str) -> tuple:
    return ("decimal", Decimal(lo), Decimal(hi))


def _int(lo: int, hi: int) -> tuple:
    return ("int", lo, hi)


def _opt(lo: str, hi: str) -> tuple:
    return ("optional_range", Decimal(lo), Decimal(hi))


# Operator-set exit plan for the Pump.fun strategies (fractions of the entry
# price: 0.2 = 20%). Unset keys are calculated automatically. Every value is
# validated again by the safety gate against the risk settings (a stop
# outside min/max_stop_pct, TPs out of order, a size above any cap ... are
# refused or reduced, never trusted blindly).
SOLANA_MANUAL_RULES = {
    "manual_stop_loss_pct": _opt("0.01", "0.9"),
    "manual_tp1_pct": _opt("0.01", "100"),
    "manual_tp2_pct": _opt("0.01", "100"),
    "manual_tp3_pct": _opt("0.01", "100"),
    "manual_trailing_pct": _opt("0.01", "0.9"),
    "manual_position_size_sol": _opt("0.001", "1000"),
    "manual_max_risk_sol": _opt("0.0001", "1000"),
}


CATALOG: dict[str, Entry] = {e.name: e for e in [
    Entry("solana_fresh", "Fresh Tokens (pump.fun)", "solana", "solana_fresh", "solana",
          "PAPER by default — pump.fun program stream, safety gate, curve-simulated fills. LIVE path (PumpPortal local "
          "transactions, guard, confirm, reconcile) implemented — awaiting credential verification",
          {}, SOLANA_MANUAL_RULES),
    Entry("solana_migration", "Migrated Tokens (pump.fun → PumpSwap)", "solana", "solana_migration", "solana",
          "PAPER by default — canonical PumpSwap pool read from chain, pool-simulated fills, trader flow from pool "
          "events. LIVE path implemented — awaiting credential verification",
          {}, SOLANA_MANUAL_RULES),
    Entry("solana_momentum", "Solana Momentum", "solana", "solana_momentum", "solana",
          "PAPER — acceleration signal on pump.fun tokens older than 30 min; unvalidated heuristic",
          {}, SOLANA_MANUAL_RULES),
    Entry("meta_muse", "Meta Muse Crossover", "futures", None, "binance_futures",
          "PAPER — ported from the user's repository; BTC/ETH 9/21 EMA divergence on closed candles",
          meta_muse.DEFAULTS,
          {"asset1": ("symbol",), "asset2": ("symbol",), "interval": ("choice", FUTURES_INTERVALS),
           "fast": _int(2, 100), "slow": _int(3, 400), "trend_threshold": _dec("0.0001", "0.05"),
           "stop_pct": _dec("0.002", "0.2"), "target_pct": _dec("0.002", "0.5"), "candles": _int(50, 1000),
           "venue": ("choice", ["binance", "bybit", "hyperliquid"])}),
    Entry("confluence_matrix", "Confluence Matrix", "futures", None, "binance_futures",
          "PAPER — ported scoring on Binance XAUUSDT; MT5/forex execution BLOCKED (no MT5 bridge)",
          confluence.DEFAULTS,
          {"symbol": ("symbol",), "interval": ("choice", FUTURES_INTERVALS), "mode": ("choice", ["V1", "V2"]),
           "threshold": _int(0, 100), "pivot_lookback": _int(3, 100), "tp1_extension": _dec("0.1", "5"),
           "tp2_extension": _dec("0.1", "10"), "sl_buffer_pct": _dec("0", "1"), "rsi_period": _int(2, 100),
           "atr_period": _int(2, 100), "liquidity_stdev_len": _int(5, 200), "use_killzones": ("bool",),
           "killzones": ("hours",), "cooldown_bars": _int(0, 100), "candles": _int(100, 1500),
           "venue": ("choice", ["binance", "bybit", "hyperliquid"])}),
    Entry("hyperliquid_grid", "Hyperliquid Grid", "grid", "hyperliquid_perps", "hyperliquid",
          "PAPER — ported grid math against live Hyperliquid mids; maker fills simulated",
          grid.DEFAULTS,
          {"coin": ("coin",), "range_mode": ("choice", ["auto", "manual"]), "range_pct": _dec("0.05", "50"),
           "range_lower": ("optional_decimal",), "range_upper": ("optional_decimal",), "grid_levels": _int(2, 50),
           "capital": _dec("1", "1000000"), "leverage": _dec("1", "5"), "max_drawdown_pct": _dec("1", "50"),
           "range_break_pct": _dec("0.1", "50"), "flatten_on_pause": ("bool",), "maker_fee_bps": _dec("0", "50"),
           "taker_fee_bps": _dec("0", "50")}),
    Entry("gold_vs_btc", "Gold vs BTC", "analytics", None, None,
          "ANALYTICS ONLY — ratio, z-score and correlation; no trading signal (not backtested)",
          gold_btc.DEFAULTS,
          {"btc": ("symbol",), "gold": ("choice", ["XAUUSDT", "PAXGUSDT"]), "interval": ("choice", ["15m", "1h", "4h", "1d"]),
           "candles": _int(50, 1500), "z_window": _int(10, 1000), "corr_window": _int(10, 1000)},
          has_mode=False),
    Entry("binance_futures", "Binance Futures", "venue", "binance_futures", "binance_futures",
          "PAPER — public market data; account sync implemented, awaiting credential verification; live orders disabled"),
    Entry("bybit_futures", "Bybit", "venue", "bybit_futures", "bybit_futures",
          "PAPER — V5 public data and signed read-only account calls implemented; awaiting credential verification"),
    Entry("hyperliquid_perps", "Hyperliquid", "venue", "hyperliquid_perps", "hyperliquid",
          "PAPER — info API (mids, books, candles, account by address) implemented; no signing / order placement"),
]}

STRATEGY_VENUE_ENGINE = {"binance": "binance_futures", "bybit": "bybit_futures", "hyperliquid": "hyperliquid_perps"}
MODE_KEYS = [n for n, e in CATALOG.items() if e.has_mode]


def _check(key: str, value: Any, rule: tuple) -> tuple[Any, str | None]:
    kind = rule[0]
    if kind == "bool":
        return (value, None) if isinstance(value, bool) else (None, f"{key}: must be true/false")
    if kind in ("symbol", "coin"):
        rx = SYMBOL_RE if kind == "symbol" else COIN_RE
        if not isinstance(value, str) or not rx.match(value):
            return None, f"{key}: must be an uppercase {kind} like {'ETHUSDT' if kind == 'symbol' else 'BTC'}"
        return value, None
    if kind == "choice":
        return (value, None) if value in rule[1] else (None, f"{key}: must be one of {rule[1]}")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"{key}: must be a whole number"
        return (value, None) if rule[1] <= value <= rule[2] else (None, f"{key}: must be between {rule[1]} and {rule[2]}")
    if kind == "optional_range":
        if value is None or value == "":
            return None, None
        if isinstance(value, bool):
            return None, f"{key}: must be a number"
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None, f"{key}: must be a number"
        if not d.is_finite() or not rule[1] <= d <= rule[2]:
            return None, f"{key}: must be empty (automatic) or between {rule[1]} and {rule[2]}"
        return str(d), None
    if kind in ("decimal", "optional_decimal"):
        if value is None and kind == "optional_decimal":
            return None, None
        if isinstance(value, bool):
            return None, f"{key}: must be a number"
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None, f"{key}: must be a number"
        if not d.is_finite():
            return None, f"{key}: must be finite"
        if kind == "optional_decimal":
            return (str(d), None) if d > 0 else (None, f"{key}: must be > 0")
        return (str(d), None) if rule[1] <= d <= rule[2] else (None, f"{key}: must be between {rule[1]} and {rule[2]}")
    if kind == "hours":
        ok = isinstance(value, list) and all(
            isinstance(w, list) and len(w) == 2 and all(isinstance(h, int) and 0 <= h <= 24 for h in w) and w[0] < w[1]
            for w in value)
        return (value, None) if ok else (None, f"{key}: must be a list of [start_hour, end_hour] pairs (UTC)")
    return None, f"{key}: unsupported"


def validate_config(name: str, data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Returns (clean config, errors). Only keys the strategy defines are
    accepted; the clean config holds just the submitted keys, which the
    engines merge over DEFAULTS."""
    entry = CATALOG.get(name)
    if entry is None or not entry.rules:
        return {}, [f"{name} has no editable configuration" + (" (use Risk Settings for its engine)" if entry else "")]
    errors: list[str] = []
    clean: dict[str, Any] = {}
    for key, value in data.items():
        rule = entry.rules.get(key)
        if rule is None:
            errors.append(f"{key}: unknown setting")
            continue
        v, err = _check(key, value, rule)
        if err:
            errors.append(err)
        else:
            clean[key] = v
    merged = {**entry.defaults, **clean}
    if name == "meta_muse" and int(merged["fast"]) >= int(merged["slow"]):
        errors.append("fast EMA must be shorter than slow EMA")
    if name == "meta_muse" and merged["asset1"] == merged["asset2"]:
        errors.append("asset1 and asset2 must differ")
    if name == "confluence_matrix" and Decimal(str(merged["tp2_extension"])) <= Decimal(str(merged["tp1_extension"])):
        errors.append("tp2_extension must be greater than tp1_extension")
    if name == "hyperliquid_grid" and merged["range_mode"] == "manual":
        lo, hi = merged.get("range_lower"), merged.get("range_upper")
        if lo is None or hi is None or Decimal(str(lo)) >= Decimal(str(hi)):
            errors.append("manual range needs range_lower < range_upper")
    if name in ("solana_fresh", "solana_migration", "solana_momentum"):
        tps = [merged.get(f"manual_tp{i}_pct") for i in (1, 2, 3)]
        given = [Decimal(str(t)) for t in tps if t is not None]
        if any(t is None for t in tps[:len(given)]) or given != sorted(set(given)):
            errors.append("manual take-profits must be set in order (TP1, then TP2, then TP3) and strictly increasing")
    if name == "gold_vs_btc" and max(int(merged["z_window"]), int(merged["corr_window"])) >= int(merged["candles"]):
        errors.append("z_window and corr_window must be smaller than candles")
    return clean, errors
