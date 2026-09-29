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
    # Token-specific taxes (Token-2022 transfer fee), in percent. Pump.fun /
    # PumpSwap trading fees are protocol fees and are costed separately.
    max_buy_tax_pct: Decimal = Decimal("5")
    max_sell_tax_pct: Decimal = Decimal("5")

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
    # Minimum traded volume (quote) in the flow window. 0 disables.
    min_window_volume_quote: Decimal = Decimal("0")
    # Wallet-behaviour indicators: above these, operator approval is needed.
    # They are indicators of possible coordination, not proof of it.
    max_early_buy_share: Decimal = Decimal("0.30")
    max_sync_buy_cluster: int = 5
    max_round_trip_share: Decimal = Decimal("0.50")
    max_creator_launches_24h: int = 3
    # Demand quality and wallet relationships (indicators, never accusations)
    max_volume_churn: Decimal = Decimal("20")  # gross / |net| volume in the window
    max_repeated_wallet_share: Decimal = Decimal("0.60")  # trades from wallets trading >= 3 times
    funding_check_wallets: int = 6  # early buyers whose funding source is checked (0 = off)
    max_creator_linked_buyers: int = 0
    max_related_wallet_group: int = 3

    # Stops and targets
    stop_volatility_multiple: Decimal = Decimal("2")
    min_stop_pct: Decimal = Decimal("0.05")
    max_stop_pct: Decimal = Decimal("0.30")
    tp_r_multiples: tuple[Decimal, ...] = (Decimal("1"), Decimal("2"), Decimal("3"))
    tp_exit_fractions: tuple[Decimal, ...] = (Decimal("0.4"), Decimal("0.3"), Decimal("0.3"))
    trailing_volatility_multiple: Decimal = Decimal("1.5")
    min_trailing_pct: Decimal = Decimal("0.05")
    # When the trailing stop starts: 0 = at TP1 (the default); otherwise once
    # price reaches this R multiple of the stop distance.
    trailing_activation_r: Decimal = Decimal("0")
    # Largest giveback from the peak the trailing stop allows: 0 = no cap
    # beyond the stop distance (the default); otherwise the trailing distance
    # never exceeds this fraction (0.15 = 15%).
    trailing_max_giveback_pct: Decimal = Decimal("0")
    max_risk_level_for_auto: str = "MODERATE"

    # Fresh pump.fun observation (discovery funnel; read from the
    # solana_fresh scope). Every new token is watched for
    # fresh_observation_seconds and compared at T0 / T+half / T+window before
    # anything is decided; a token still interesting but not yet qualified
    # stays under observation up to fresh_max_monitoring_seconds.
    fresh_observation_seconds: int = 10
    fresh_continue_monitoring: bool = True
    fresh_max_monitoring_seconds: int = 900
    fresh_max_monitored_tokens: int = 300
    fresh_min_trades_to_continue: int = 3  # trades in the latest window to keep watching
    fresh_inactivity_timeout_seconds: int = 120  # no trade for this long: expire
    fresh_promote_min_trades: int = 8  # since launch, to hand the token to the safety gate
    fresh_promote_min_unique_buyers: int = 6
    fresh_promote_min_volume_quote: Decimal = Decimal("0.5")  # SOL traded since launch
    fresh_max_sell_pressure: Decimal = Decimal("2")  # sell/buy volume in the latest half-window
    fresh_max_price_drawdown_pct: Decimal = Decimal("0.35")  # fall from the observed peak
    max_active_candidates: int = 25  # tokens under full (RPC-costly) gate analysis at once
    # Momentum engine (read from the solana_fresh scope by the funnel):
    # established tokens older than momentum_min_age_seconds, plus younger
    # tokens approaching migration (curve progress at or above this share)
    # that the fresh engine did not already take.
    momentum_min_age_seconds: int = 1800
    momentum_near_migration_progress: Decimal = Decimal("0.70")

    # Bonding-curve tokens (fresh, momentum) trade on the pump.fun curve, not
    # a DEX pool: min_liquidity_quote applies to pools only. The curve must
    # be executable (fee known, exact fill simulated both ways within the
    # impact / round-trip limits); 0 means no extra minimum on its reserve.
    min_curve_liquidity_quote: Decimal = Decimal("0")

    # Holder concentration: accounts owned by programs rather than wallets
    # (PDAs) are reported separately from wallets. Pump.fun Mayhem-mode
    # tokens hold part of the supply in the Mayhem agent's vault.
    max_protocol_agent_share: Decimal = Decimal("0.60")  # above: reduce size
    max_program_controlled_share: Decimal = Decimal("0.15")  # unknown program accounts: approval above

    # Exit intelligence (open Solana positions). Two independent signals for
    # REDUCE/EXIT; one emergency signal is enough for EXIT_NOW.
    exit_liquidity_drop_exit: Decimal = Decimal("0.40")
    exit_liquidity_drop_warn: Decimal = Decimal("0.20")
    exit_reduce_fraction: Decimal = Decimal("0.5")
    exit_volume_collapse_ratio: Decimal = Decimal("0.25")  # latest window volume vs previous
    exit_emergency_liquidity_drop: Decimal = Decimal("0.60")
    exit_emergency_price_drop: Decimal = Decimal("0.35")  # from the highest price since entry

    # Creator / developer history (pump.fun). The number of pump.fun tokens
    # the launch creator's wallet has created, counted on chain (see
    # solana.creator_history). Below the minimum is not proof of anything:
    # the action is the operator's choice, WARN by default. When the count
    # cannot be established it is UNKNOWN, never guessed.
    creator_history_check: bool = True
    min_creator_tokens_created: int = 5
    creator_below_threshold_action: str = "WARN"  # WARN | REDUCE_SIZE | REQUIRE_MANUAL_APPROVAL | REJECT
    creator_history_unknown_action: str = "WARN"  # same choices, when the count is unavailable
    # Serial-launcher ("scam farm") ceiling: at or above this many tokens
    # created, operator approval is needed. 0 disables.
    max_creator_tokens_created: int = 0

    # Migrated (PumpSwap) tokens only: minimum USABLE liquidity in USD — the
    # SOL side of the pool (what a seller can actually withdraw) at the
    # current SOL/USD price, not the cosmetic two-sided figure. Below it:
    # NO_TRADE. Never applied to bonding-curve tokens.
    migrated_liquidity_check: bool = True
    min_migrated_liquidity_usd: Decimal = Decimal("10000")
    # Price impact plus pool fee at the planned size, migrated pools only.
    # The pure price-impact limits are max_entry_impact_bps / max_exit_impact_bps.
    migrated_max_entry_slippage_bps: Decimal = Decimal("400")
    migrated_max_exit_slippage_bps: Decimal = Decimal("600")

    # Token name filters (pump.fun names/symbols). Word blacklists are the
    # Rules page; these are structural checks.
    min_name_length: int = 2  # 0 disables
    skip_duplicate_names: bool = True  # a later launch reusing an earlier launch's name/symbol (24 h)
    ascii_names_only: bool = False

    # Intelligence (docs/INTELLIGENCE_AUDIT_2026.md). Features are recorded
    # for every decision; only the *_action settings act on them.
    intel_snapshot_seconds: int = 60  # launch snapshots at T0/5/10/20/30/60 s (and 120/300/600 if longer)
    intel_meaningful_buy_sol: Decimal = Decimal("0.05")
    intel_buyer_breadth_target: int = 20  # unique buyers per minute that count as full breadth
    # Mayhem Mode tokens (and any curve where the constant product does not
    # hold): curve pricing, impact and sizing are invalid for them.
    mayhem_action: str = "NO_TRADE"
    # Manipulation score: HIGH needs this many independent families.
    manipulation_window_seconds: int = 120
    manipulation_high_families: int = 3
    manipulation_round_trip_share: Decimal = Decimal("0.40")
    manipulation_sync_sell_wallets: int = 3
    manipulation_regular_size_cv: Decimal = Decimal("0.05")
    manipulation_dust_share: Decimal = Decimal("0.40")
    manipulation_linear_r2: Decimal = Decimal("0.97")
    manipulation_collapse_pct: Decimal = Decimal("0.30")
    # Whether a family counts toward the level (it is always reported as
    # evidence). Production 2026-09-28/29 (6544 launches): launches where
    # these fired collapsed LESS often than average (docs/
    # INTELLIGENCE_AUDIT_2026.md §7). Default: counted (behaviour unchanged).
    manipulation_count_synchronized_sells: bool = True
    manipulation_count_synchronized_buys: bool = True
    manipulation_count_single_second_collapse: bool = True
    manipulation_count_dust_volume: bool = True
    manipulation_high_action: str = "NO_TRADE"
    manipulation_medium_action: str = "WARN"
    # Migration: create → migrate faster than this is an instant bond (a
    # bundle); BOOST: post-migration buybacks in the first seconds.
    instant_bond_seconds: int = 5
    boost_window_seconds: int = 300
    postmig_dumping_action: str = "WAIT"
    # Wallet intelligence (yonixalpha_core.wallet_intel): features, never a
    # BUY trigger. Reputation is Beta-shrunk toward the base rate and judged
    # on its lower bound; outcomes count only once resolved.
    wallet_early_buyers: int = 20  # first N distinct buyers recorded per launch
    wallet_early_sell_seconds: int = 120  # "sold early": within this long of its first buy
    wallet_sold_early_share: Decimal = Decimal("0.50")  # of the tokens it bought
    wallet_win_peak_pct: Decimal = Decimal("50")  # launch outcome WIN: rose this much within 30 min of the decision
    wallet_loss_drawdown_pct: Decimal = Decimal("50")  # LOSS: no WIN and fell this much
    wallet_prior_strength: Decimal = Decimal("10")  # pseudo-launches of base rate in the prior
    wallet_min_launches: int = 5  # resolved launches before a wallet can be "proven"
    wallet_history_days: int = 30
    wallet_recycled_launches: int = 5  # early buyer of this many other launches in 24 h = recycled
    dump_cluster_min_shared: int = 3  # dumped together in this many failed launches = linked
    dump_cluster_medium_wallets: int = 2
    dump_cluster_high_wallets: int = 4
    dump_cluster_high_action: str = "WARN"
    # Wallet relationships / organic demand (solana.wallet_graph): the
    # organic-demand ratio is reported as one number only when at least this
    # share of volume is attributed (else as lower / upper bounds).
    organic_min_attribution_share: Decimal = Decimal("0.5")
    # Manufactured-pump detector (solana.manufactured_pump). Starting values,
    # to be calibrated on this system's outcomes; stored with every result.
    manufactured_pump_candle_seconds: int = 10
    manufactured_pump_min_candles: int = 6
    manufactured_pump_min_positive_share: Decimal = Decimal("0.65")
    manufactured_pump_max_return_cv: Decimal = Decimal("1.5")
    manufactured_pump_max_buy_ratio_std: Decimal = Decimal("0.15")
    manufactured_pump_min_log_r2: Decimal = Decimal("0.85")
    manufactured_pump_min_window_return: Decimal = Decimal("0.25")
    manufactured_pump_action: str = "WARN"  # when the detector's risk is HIGH

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
    "max_buy_tax_pct": ("max", Decimal("25")),
    "funding_check_wallets": ("max", 12),
    "max_sell_tax_pct": ("max", Decimal("25")),
    "reject_top1_share": ("max", Decimal("0.60")),
    "reject_top10_share": ("max", Decimal("0.95")),
    "max_stop_pct": ("max", Decimal("0.50")),
    "min_stop_pct": ("min", Decimal("0.005")),
    "max_open_positions": ("max", 50),
    "max_leverage": ("max", Decimal("5")),
    "cooldown_after_loss_seconds": ("min", 0),
    "fresh_observation_seconds": ("min", 2),
    "fresh_max_monitored_tokens": ("max", 2000),
    "max_active_candidates": ("max", 100),
    "exit_reduce_fraction": ("max", Decimal("1")),
    "exit_emergency_price_drop": ("min", Decimal("0.05")),
    "exit_emergency_liquidity_drop": ("min", Decimal("0.10")),
    "min_migrated_liquidity_usd": ("min", Decimal("0")),
    "migrated_max_entry_slippage_bps": ("max", Decimal("1500")),
    "migrated_max_exit_slippage_bps": ("max", Decimal("2000")),
    "min_creator_tokens_created": ("min", 0),
    "max_creator_tokens_created": ("min", 0),
    "min_name_length": ("max", 32),
    # One indicator is never enough for a HIGH manipulation level.
    "manipulation_high_families": ("min", 2),
    "intel_snapshot_seconds": ("max", 600),
    "manufactured_pump_candle_seconds": ("min", 2),
    "manufactured_pump_min_candles": ("min", 3),
    "manipulation_window_seconds": ("max", 900),
    "wallet_early_buyers": ("max", 50),
    # A reputation needs a prior and a few resolved launches.
    "wallet_prior_strength": ("min", Decimal("2")),
    "wallet_min_launches": ("min", 3),
    "dump_cluster_min_shared": ("min", 2),
}

CREATOR_ACTIONS = ("WARN", "REDUCE_SIZE", "REQUIRE_MANUAL_APPROVAL", "REJECT")
INTEL_ACTIONS = ("WARN", "REQUIRE_MANUAL_APPROVAL", "WAIT", "NO_TRADE")
# Fields with a fixed set of values (the dashboard renders a select).
ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "creator_below_threshold_action": CREATOR_ACTIONS,
    "creator_history_unknown_action": CREATOR_ACTIONS,
    "max_risk_level_for_auto": ("LOW", "MODERATE", "HIGH"),
    "mayhem_action": INTEL_ACTIONS,
    "manipulation_high_action": INTEL_ACTIONS,
    "manipulation_medium_action": INTEL_ACTIONS,
    "postmig_dumping_action": INTEL_ACTIONS,
    "dump_cluster_high_action": INTEL_ACTIONS,
    "manufactured_pump_action": INTEL_ACTIONS,
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
    if settings.trailing_activation_r < 0:
        errors.append("trailing_activation_r must be 0 (at TP1) or positive")
    if settings.trailing_max_giveback_pct < 0 or settings.trailing_max_giveback_pct > settings.max_stop_pct:
        errors.append("trailing_max_giveback_pct must be 0 (no cap) or at most max_stop_pct")
    if settings.max_leverage < 1:
        errors.append("max_leverage must be at least 1")
    if settings.risk_per_trade_pct <= 0:
        errors.append("risk_per_trade_pct must be positive")
    if settings.min_position_size_quote > settings.max_position_size_quote:
        errors.append("min_position_size_quote must not exceed max_position_size_quote")
    if settings.max_risk_level_for_auto not in ("LOW", "MODERATE", "HIGH"):
        errors.append("max_risk_level_for_auto must be LOW, MODERATE or HIGH")
    if settings.fresh_max_monitoring_seconds < settings.fresh_observation_seconds:
        errors.append("fresh_max_monitoring_seconds must be at least fresh_observation_seconds")
    if settings.fresh_inactivity_timeout_seconds < settings.fresh_observation_seconds:
        errors.append("fresh_inactivity_timeout_seconds must be at least fresh_observation_seconds")
    if settings.fresh_max_sell_pressure <= 0 or settings.fresh_max_price_drawdown_pct <= 0:
        errors.append("fresh_max_sell_pressure and fresh_max_price_drawdown_pct must be positive")
    if settings.max_active_candidates < 1 or settings.fresh_max_monitored_tokens < 1:
        errors.append("max_active_candidates and fresh_max_monitored_tokens must be at least 1")
    if not (0 < settings.exit_reduce_fraction <= 1):
        errors.append("exit_reduce_fraction must be in (0, 1]")
    if settings.exit_liquidity_drop_warn > settings.exit_liquidity_drop_exit:
        errors.append("exit_liquidity_drop_warn must not exceed exit_liquidity_drop_exit")
    if settings.exit_liquidity_drop_exit > settings.exit_emergency_liquidity_drop:
        errors.append("exit_liquidity_drop_exit must not exceed exit_emergency_liquidity_drop")
    if not (0 < settings.momentum_near_migration_progress <= 1):
        errors.append("momentum_near_migration_progress must be in (0, 1]")
    if not (0 < settings.exit_volume_collapse_ratio < 1):
        errors.append("exit_volume_collapse_ratio must be between 0 and 1")
    for name in ("creator_below_threshold_action", "creator_history_unknown_action"):
        if getattr(settings, name) not in CREATOR_ACTIONS:
            errors.append(f"{name} must be one of {', '.join(CREATOR_ACTIONS)}")
    if settings.max_creator_tokens_created and settings.max_creator_tokens_created <= settings.min_creator_tokens_created:
        errors.append("max_creator_tokens_created must be 0 (off) or above min_creator_tokens_created")
    if settings.migrated_max_entry_slippage_bps <= 0 or settings.migrated_max_exit_slippage_bps <= 0:
        errors.append("migrated_max_entry_slippage_bps and migrated_max_exit_slippage_bps must be positive")
    if settings.min_name_length < 0:
        errors.append("min_name_length must not be negative")
    for name in ("mayhem_action", "manipulation_high_action", "manipulation_medium_action", "postmig_dumping_action",
                 "dump_cluster_high_action", "manufactured_pump_action"):
        if getattr(settings, name) not in INTEL_ACTIONS:
            errors.append(f"{name} must be one of {', '.join(INTEL_ACTIONS)}")
    for name in ("manipulation_round_trip_share", "manipulation_regular_size_cv", "manipulation_dust_share",
                 "manipulation_linear_r2", "manipulation_collapse_pct"):
        if not (0 < getattr(settings, name) <= 1):
            errors.append(f"{name} must be in (0, 1]")
    for name in ("organic_min_attribution_share", "manufactured_pump_min_positive_share",
                 "manufactured_pump_max_buy_ratio_std", "manufactured_pump_min_log_r2"):
        if not (0 < getattr(settings, name) <= 1):
            errors.append(f"{name} must be in (0, 1]")
    if settings.manufactured_pump_max_return_cv <= 0 or settings.manufactured_pump_min_window_return <= 0:
        errors.append("manufactured_pump_max_return_cv and manufactured_pump_min_window_return must be positive")
    if settings.intel_snapshot_seconds < 5 or settings.manipulation_window_seconds < 10:
        errors.append("intel_snapshot_seconds must be at least 5 and manipulation_window_seconds at least 10")
    if settings.wallet_early_buyers < 2 or settings.wallet_early_sell_seconds < 10 or settings.wallet_history_days < 1:
        errors.append("wallet_early_buyers must be at least 2, wallet_early_sell_seconds at least 10, wallet_history_days at least 1")
    if not (0 < settings.wallet_sold_early_share <= 1):
        errors.append("wallet_sold_early_share must be in (0, 1]")
    if settings.wallet_win_peak_pct <= 0 or not (0 < settings.wallet_loss_drawdown_pct <= 100):
        errors.append("wallet_win_peak_pct must be positive and wallet_loss_drawdown_pct in (0, 100]")
    if not (2 <= settings.dump_cluster_medium_wallets <= settings.dump_cluster_high_wallets):
        errors.append("dump_cluster_medium_wallets must be at least 2 and not above dump_cluster_high_wallets")
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
    "mt5_fx": dict(_FUTURES_DEFAULTS),
}


def default_settings_for(engine: str) -> SafetySettings:
    return settings_from_dict(ENGINE_DEFAULTS.get(engine, {}))
