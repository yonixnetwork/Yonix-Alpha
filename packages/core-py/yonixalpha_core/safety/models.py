from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

# Bumped whenever gate/planning logic changes in a way that could change a
# decision for identical inputs, so a stored assessment stays reproducible.
RISK_ENGINE_VERSION = "2.0.0"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


RISK_LEVEL_ORDER = [RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH, RiskLevel.CRITICAL]


def max_level(levels: list[RiskLevel]) -> RiskLevel:
    if not levels:
        return RiskLevel.LOW
    return max(levels, key=RISK_LEVEL_ORDER.index)


class RiskCategory(StrEnum):
    DATA = "DATA"
    TOKEN = "TOKEN"
    LIQUIDITY = "LIQUIDITY"
    EXECUTION = "EXECUTION"
    HOLDER = "HOLDER"
    TRADING = "TRADING"
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"
    STRATEGY = "STRATEGY"
    ML = "ML"


class FinalDecision(StrEnum):
    EXECUTE = "EXECUTE"
    REDUCE_SIZE = "REDUCE_SIZE"
    WAIT = "WAIT"
    REQUIRE_MANUAL_APPROVAL = "REQUIRE_MANUAL_APPROVAL"
    REJECT = "REJECT"
    NO_TRADE = "NO_TRADE"


# When several findings demand different outcomes, the most restrictive wins.
# REJECT (the asset itself is unacceptable) outranks NO_TRADE (safety can't be
# established right now) only in label, both block; WAIT means "re-evaluate
# later", which is less restrictive than either.
DECISION_PRECEDENCE = [
    FinalDecision.REJECT,
    FinalDecision.NO_TRADE,
    FinalDecision.WAIT,
    FinalDecision.REQUIRE_MANUAL_APPROVAL,
    FinalDecision.REDUCE_SIZE,
    FinalDecision.EXECUTE,
]


class DataStatus(StrEnum):
    LIVE = "LIVE"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class Provenance(StrEnum):
    MANUAL = "MANUAL"  # operator-specified
    AUTO = "AUTO"  # calculated by the risk engine
    STRATEGY = "STRATEGY"  # supplied by the strategy's own rules (e.g. pivot stops)


class GlobalMode(StrEnum):
    PAPER = "PAPER"
    MANUAL = "MANUAL"
    LIVE = "LIVE"


class StrategyMode(StrEnum):
    OFF = "OFF"
    PAPER = "PAPER"
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class ExecutionTarget(StrEnum):
    NONE = "NONE"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Venue(StrEnum):
    PUMP_BONDING_CURVE = "PUMP_BONDING_CURVE"
    PUMPSWAP = "PUMPSWAP"
    JUPITER = "JUPITER"
    BINANCE_FUTURES = "BINANCE_FUTURES"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Finding:
    category: RiskCategory
    code: str
    level: RiskLevel
    message: str
    action: FinalDecision
    hard_block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "level": self.level.value,
            "message": self.message,
            "action": self.action.value,
            "hard_block": self.hard_block,
        }


@dataclass
class Observation:
    """Freshness envelope for one input. A value the caller never obtained
    must be passed as None, never as a zero or an empty default."""

    source: str
    observed_at: datetime | None


@dataclass
class TokenProgramInfo:
    observation: Observation
    token_program: str
    mint_authority: str | None
    freeze_authority: str | None
    decimals: int
    supply_raw: int
    # Token-2022 extension tags exactly as jsonParsed reports them
    # ("transferFeeConfig", "transferHook", ...).
    extensions: list[str] = field(default_factory=list)
    transfer_fee_bps: int | None = None
    transfer_fee_authority: str | None = None
    transfer_hook_program: str | None = None
    permanent_delegate: str | None = None
    default_account_state: str | None = None
    paused: bool | None = None
    unparseable_extension: bool = False


@dataclass
class HolderInfo:
    observation: Observation
    # Shares of total supply (0..1) held by the largest owners, with known
    # pool/bonding-curve accounts already excluded by the caller.
    top1_share: Decimal
    top10_share: Decimal
    creator_share: Decimal | None
    holders_sampled: int
    excluded_pool_accounts: int
    # top1/top10 count wallets only (owners on the ed25519 curve). Accounts
    # owned by a program-derived address are not anyone's personal wallet
    # and are reported here instead: the Pump.fun Mayhem agent's vault
    # separately from any other program-controlled account.
    top1_owner: str | None = None
    protocol_agent_share: Decimal = Decimal(0)
    program_controlled_share: Decimal = Decimal(0)
    largest_program_owner: str | None = None
    largest_program_share: Decimal = Decimal(0)


@dataclass
class TradeFlow:
    observation: Observation
    window_seconds: int
    trade_count: int
    buy_count: int
    sell_count: int
    unique_buyers: int | None
    unique_sellers: int | None
    buy_volume_quote: Decimal
    sell_volume_quote: Decimal
    top3_wallet_volume_share: Decimal | None
    creator_sold: bool | None
    # False when counts come from an aggregator without wallet identities
    # (e.g. DexScreener txns), so per-wallet manipulation checks are impossible.
    wallet_level: bool = True
    # Wallet-behaviour indicators (spec §20-23); None = not measurable.
    early_buy_share: Decimal | None = None  # supply bought in the first 30 s
    sync_buy_cluster: int | None = None  # distinct wallets buying in lockstep
    round_trip_share: Decimal | None = None  # volume from wallets that bought AND sold
    creator_launches_24h: int | None = None  # launches by this creator seen in 24 h
    # Demand-quality indicators (spec: fake volume / manipulation).
    unique_buyers_first_half: int | None = None
    unique_buyers_second_half: int | None = None
    repeated_wallet_share: Decimal | None = None  # trades from wallets trading >= 3 times
    volume_churn: Decimal | None = None  # gross volume / |net buy volume|
    price_change: Decimal | None = None  # first→last traded price in window
    # Funding relationships of early buyers (RPC, bounded). Indicators only —
    # never a claim of common ownership.
    creator_linked_buyers: int | None = None  # early buyers funded by the creator
    related_wallet_groups: int | None = None  # largest set of buyers sharing one funder
    funding_checked_wallets: int | None = None


@dataclass
class ExecutionQuote:
    """Outcome of simulating the planned position against real liquidity,
    in both directions. Impacts/fees are in basis points of notional."""

    observation: Observation
    venue: Venue
    size_quote: Decimal
    buy_route_available: bool | None
    sell_route_available: bool | None
    entry_impact_bps: Decimal | None
    exit_impact_bps: Decimal | None
    round_trip_loss_bps: Decimal | None
    fee_bps_per_side: Decimal
    expected_entry_price: Decimal | None
    expected_exit_price: Decimal | None


@dataclass
class MarketInfo:
    observation: Observation
    price: Decimal | None
    # Realized volatility of returns over the recent window, as a fraction
    # (0.05 = 5%). Drives automatic stop distance.
    volatility: Decimal | None
    liquidity_quote: Decimal | None
    age_seconds: float | None
    curve_complete: bool | None = None
    migrated: bool | None = None
    # Share of the bonding curve's sellable tokens already bought (0..1);
    # None off the curve or when the launch reserve is unknown.
    curve_progress: Decimal | None = None


@dataclass
class StrategySignal:
    name: str
    version: str
    qualified: bool
    strength: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class MLInput:
    model_name: str
    model_version: int
    confidence: float


@dataclass
class AccountState:
    equity: Decimal | None
    available_balance: Decimal | None
    open_positions: int
    current_exposure: Decimal | None
    daily_realized_pnl: Decimal | None
    last_loss_at: datetime | None
    token_exposure: Decimal | None
    kill_switch_engaged: bool


@dataclass
class ManualOverrides:
    """Operator-specified values. Each is validated, never trusted blindly."""

    stop_loss: Decimal | None = None
    position_size_quote: Decimal | None = None
    take_profits: list[Decimal] | None = None
    trailing_distance_pct: Decimal | None = None
    max_risk_quote: Decimal | None = None


@dataclass
class StrategyLevels:
    """Stop/targets a strategy defines itself (e.g. Confluence Matrix: stop
    beyond the opposite pivot, targets at wave extensions). Validated exactly
    like operator values; an operator override still takes precedence."""

    stop_loss: Decimal | None = None
    take_profits: list[Decimal] | None = None
    move_stop_to_breakeven_at_tp1: bool = False
    source: str = ""


@dataclass
class TargetContext:
    """Evidence for automatic take-profits beyond R-multiples. Both inputs
    are optional; absent means "not used", never guessed.
    - resistance: the recent high above the current price (overhead supply
      from earlier buyers), from the token's own trades;
    - historical_mfe: the 75th-percentile maximum favourable excursion
      (fraction, e.g. 0.8 = +80%) of this strategy's closed trades, only
      with at least `samples` >= MIN_HISTORY_SAMPLES of them."""

    resistance: Decimal | None = None
    resistance_source: str = ""
    historical_mfe: Decimal | None = None
    samples: int = 0


@dataclass
class AssessmentInput:
    engine: str
    strategy_name: str
    asset_id: str
    symbol: str
    now: datetime
    market: MarketInfo | None
    token: TokenProgramInfo | None
    holders: HolderInfo | None
    flow: TradeFlow | None
    account: AccountState
    liquidity_model: Any | None = None
    quote: ExecutionQuote | None = None
    signal: StrategySignal | None = None
    ml: MLInput | None = None
    blacklisted_by: str | None = None
    rule_actions: list[tuple[str, FinalDecision]] = field(default_factory=list)
    overrides: ManualOverrides = field(default_factory=ManualOverrides)
    global_mode: GlobalMode = GlobalMode.PAPER
    strategy_mode: StrategyMode = StrategyMode.PAPER
    live_trading_permitted: bool = False
    manual_approval_granted: bool = False
    # Live execution readiness (wallet, executor heartbeat, RPC); None when
    # not evaluated. A LIVE target with anything but True is refused.
    live_ready: bool | None = None
    live_not_ready_reason: str | None = None
    # LONG for spot (Solana); futures strategies may request SHORT.
    side: str = "LONG"
    strategy_levels: StrategyLevels | None = None
    targets: TargetContext | None = None
    # Launch creator wallet (Solana), so holder findings can say whether the
    # largest wallet is the creator's own.
    creator: str | None = None
    # Fresh-token observation (T0 / T+half / T+window comparison) from
    # solana.observation; the gate adds its trend as a finding.
    observation: dict | None = None
