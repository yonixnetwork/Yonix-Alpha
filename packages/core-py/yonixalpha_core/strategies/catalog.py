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
]}

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
    if name in ("solana_fresh", "solana_migration", "solana_momentum"):
        tps = [merged.get(f"manual_tp{i}_pct") for i in (1, 2, 3)]
        given = [Decimal(str(t)) for t in tps if t is not None]
        if any(t is None for t in tps[:len(given)]) or given != sorted(set(given)):
            errors.append("manual take-profits must be set in order (TP1, then TP2, then TP3) and strictly increasing")
    return clean, errors
