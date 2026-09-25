from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Any


@dataclass
class SafetySettings:
    """Runtime-configurable thresholds (stored in the DB, edited from the
    dashboard). Every value is clamped to HARD_LIMITS by clamp(), so a
    dashboard edit can make the system more cautious but never less than
    the immutable floor below.

    Defaults are deliberately conservative starting points for small
    paper-trading accounts, not values derived from a backtest — none has
    been run on this data yet.
    """

    # Account risk
    risk_per_trade_pct: Decimal = Decimal("0.01")
    max_position_size_quote: Decimal = Decimal("1")
    min_position_size_quote: Decimal = Decimal("0.01")
    max_open_positions: int = 3
    max_daily_loss_quote: Decimal = Decimal("0.5")
    max_total_exposure_quote: Decimal = Decimal("3")
    max_token_exposure_quote: Decimal = Decimal("1")
    cooldown_after_loss_seconds: int = 300
    # Futures only: notional allowed per unit of paper balance. 1 = no
    # leverage. Hard-capped below.
    max_leverage: Decimal = Decimal("1")

    # Liquidity / execution
    min_liquidity_quote: Decimal = Decimal("20")
    max_pool_fraction: Decimal = Decimal("0.01")
    max_entry_impact_bps: Decimal = Decimal("300")
    max_exit_impact_bps: Decimal = Decimal("500")
    max_round_trip_loss_bps: Decimal = Decimal("1000")
    max_slippage_bps: Decimal = Decimal("300")
    wait_for_liquidity_max_age_seconds: int = 1800

    # Data freshness
    max_data_age_seconds: int = 60

    # Token safety (Solana)
    reject_active_mint_authority: bool = True
    max_transfer_fee_bps: int = 100

    # Holder concentration (shares of supply, pool accounts excluded)
    max_top1_share: Decimal = Decimal("0.15")
    reject_top1_share: Decimal = Decimal("0.40")
    max_top10_share: Decimal = Decimal("0.45")
    reject_top10_share: Decimal = Decimal("0.80")
    max_creator_share: Decimal = Decimal("0.10")

    # Flow / manipulation
    min_unique_buyers: int = 10
    max_top3_volume_share: Decimal = Decimal("0.60")
    min_trades_in_window: int = 10

    # Stops and targets
    stop_volatility_multiple: Decimal = Decimal("2")
    min_stop_pct: Decimal = Decimal("0.05")
    max_stop_pct: Decimal = Decimal("0.30")
    tp_r_multiples: tuple[Decimal, ...] = (Decimal("1"), Decimal("2"), Decimal("3"))
    tp_exit_fractions: tuple[Decimal, ...] = (Decimal("0.4"), Decimal("0.3"), Decimal("0.3"))
    trailing_volatility_multiple: Decimal = Decimal("1.5")
    min_trailing_pct: Decimal = Decimal("0.05")
    max_risk_level_for_auto: str = "MODERATE"

    # ML
    min_ml_confidence: float | None = None


# Immutable ceilings/floors. clamp() enforces these on anything loaded from
# the database or submitted from the dashboard. Keys are field names; each
# value is ("max"|"min", bound).
HARD_LIMITS: dict[str, tuple[str, Any]] = {
    "risk_per_trade_pct": ("max", Decimal("0.02")),
    "max_pool_fraction": ("max", Decimal("0.05")),
    "max_entry_impact_bps": ("max", Decimal("1000")),
    "max_exit_impact_bps": ("max", Decimal("1500")),
    "max_round_trip_loss_bps": ("max", Decimal("2500")),
    "max_slippage_bps": ("max", Decimal("1000")),
    "max_data_age_seconds": ("max", 300),
    "max_transfer_fee_bps": ("max", 500),
    "reject_top1_share": ("max", Decimal("0.60")),
    "reject_top10_share": ("max", Decimal("0.95")),
    "max_stop_pct": ("max", Decimal("0.50")),
    "min_stop_pct": ("min", Decimal("0.005")),
    "max_open_positions": ("max", 50),
    "max_leverage": ("max", Decimal("5")),
    "cooldown_after_loss_seconds": ("min", 0),
}

_TUPLE_FIELDS = {"tp_r_multiples", "tp_exit_fractions"}


def clamp(settings: SafetySettings) -> tuple[SafetySettings, list[str]]:
    """Returns a copy with every HARD_LIMITS bound enforced, plus a note for
    each value that had to be changed — never silently."""
    values = asdict(settings)
    notes: list[str] = []
    for name, (kind, bound) in HARD_LIMITS.items():
        current = values[name]
        if kind == "max" and current > bound:
            values[name] = bound
            notes.append(f"{name}={current} exceeds hard limit {bound}; clamped")
        elif kind == "min" and current < bound:
            values[name] = bound
            notes.append(f"{name}={current} below hard floor {bound}; clamped")
    for name in _TUPLE_FIELDS:
        values[name] = tuple(values[name])
    return SafetySettings(**values), notes


def settings_to_dict(settings: SafetySettings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(settings):
        value = getattr(settings, f.name)
        if isinstance(value, Decimal):
            out[f.name] = str(value)
        elif isinstance(value, tuple):
            out[f.name] = [str(v) for v in value]
        else:
            out[f.name] = value
    return out


def settings_from_dict(data: dict[str, Any]) -> SafetySettings:
    """Builds settings from stored/submitted JSON. Unknown keys are ignored;
    missing keys keep their defaults; types are coerced from each field's
    default so a string "0.01" becomes Decimal."""
    defaults = SafetySettings()
    kwargs: dict[str, Any] = {}
    for f in fields(SafetySettings):
        if f.name not in data or (data[f.name] is None and f.name != "min_ml_confidence"):
            continue
        raw = data[f.name]
        default = getattr(defaults, f.name)
        if f.name in _TUPLE_FIELDS:
            kwargs[f.name] = tuple(Decimal(str(v)) for v in raw)
        elif isinstance(default, bool):
            kwargs[f.name] = bool(raw)
        elif isinstance(default, int):
            kwargs[f.name] = int(raw)
        elif isinstance(default, Decimal):
            kwargs[f.name] = Decimal(str(raw))
        elif f.name == "min_ml_confidence":
            kwargs[f.name] = None if raw is None else float(raw)
        else:
            kwargs[f.name] = raw
    return SafetySettings(**kwargs)


def validate(settings: SafetySettings) -> list[str]:
    """Internal-consistency errors that clamp() can't fix by bounding a single
    value. An empty list means valid."""
    errors: list[str] = []
    if settings.min_stop_pct >= settings.max_stop_pct:
        errors.append("min_stop_pct must be below max_stop_pct")
    if settings.max_top1_share > settings.reject_top1_share:
        errors.append("max_top1_share must not exceed reject_top1_share")
    if settings.max_top10_share > settings.reject_top10_share:
        errors.append("max_top10_share must not exceed reject_top10_share")
    if len(settings.tp_r_multiples) != len(settings.tp_exit_fractions):
        errors.append("tp_r_multiples and tp_exit_fractions must have the same length")
    if sum(settings.tp_exit_fractions) > Decimal("1"):
        errors.append("tp_exit_fractions must not sum above 1")
    if list(settings.tp_r_multiples) != sorted(settings.tp_r_multiples) or any(r <= 0 for r in settings.tp_r_multiples):
        errors.append("tp_r_multiples must be positive and ascending")
    if settings.max_leverage < 1:
        errors.append("max_leverage must be at least 1")
    if settings.risk_per_trade_pct <= 0:
        errors.append("risk_per_trade_pct must be positive")
    if settings.min_position_size_quote > settings.max_position_size_quote:
        errors.append("min_position_size_quote must not exceed max_position_size_quote")
    if settings.max_risk_level_for_auto not in ("LOW", "MODERATE", "HIGH"):
        errors.append("max_risk_level_for_auto must be LOW, MODERATE or HIGH")
    return errors


# Per-engine starting points, used when no settings row exists for the
# engine or GLOBAL. Only values forced by an engine's cost structure differ:
# a pump.fun round trip pays ~1.25-1.3% fee per side (the fee bps pump.fun's
# own TradeEvents report) plus impact plus the slippage allowance, ~6-7% in
# total at small size, so a 5% stop would sit inside the costs and every
# trade would be refused as STOP_INSIDE_COSTS. 10% keeps the stop outside
# them; sizing then shrinks the position so the loss at that stop still
# equals risk_per_trade_pct of equity.
#
# Futures engines are denominated in USDT against a 1,000 USDT paper book and
# trade deep order books, so their thresholds differ in scale: tight stops
# are meaningful (Meta Muse's own stop is 1%), impact limits are in single
# basis points, and "liquidity" is visible book depth within 2% of mid.
_FUTURES_DEFAULTS: dict[str, Any] = {
    "min_stop_pct": "0.005",
    "max_stop_pct": "0.10",
    "max_position_size_quote": "200",
    "min_position_size_quote": "10",
    "max_total_exposure_quote": "600",
    "max_token_exposure_quote": "200",
    "max_daily_loss_quote": "50",
    "min_liquidity_quote": "50000",
    "max_pool_fraction": "0.01",
    "max_entry_impact_bps": "20",
    "max_exit_impact_bps": "30",
    "max_round_trip_loss_bps": "60",
    "max_slippage_bps": "10",
}
ENGINE_DEFAULTS: dict[str, dict[str, Any]] = {
    "solana_fresh": {"min_stop_pct": "0.10"},
    "solana_migration": {"min_stop_pct": "0.10"},
    "solana_momentum": {"min_stop_pct": "0.10"},
    "binance_futures": dict(_FUTURES_DEFAULTS),
    "bybit_futures": dict(_FUTURES_DEFAULTS),
    "hyperliquid_perps": dict(_FUTURES_DEFAULTS),
}


def default_settings_for(engine: str) -> SafetySettings:
    return settings_from_dict(ENGINE_DEFAULTS.get(engine, {}))
