"""Chain / launchpad vocabulary and the adapter interfaces.

Every launchpad declares how it actually works (lifecycle, curve, liquidity,
migration, execution, safety models, the events it emits and what it
supports). Nothing assumes the Pump.fun model: a launchpad without a
graduation event says so, and migration detection is then declared
unsupported rather than invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Chain(str, Enum):
    SOLANA = "solana"
    BSC = "bsc"  # BNB Smart Chain: one network, one adapter
    ROBINHOOD = "robinhood"


class Lifecycle(str, Enum):
    DIRECT_DEX = "DIRECT_DEX"
    BONDING_CURVE = "BONDING_CURVE"
    BONDING_CURVE_TO_DEX = "BONDING_CURVE_TO_DEX"
    INSTANT_POOL = "INSTANT_POOL"
    OTHER = "OTHER"


class LaunchpadStatus(str, Enum):
    LIVE = "LIVE"  # discovery, quotes, safety AND a real buy + sell verified; operator allows live
    PAPER_ONLY = "PAPER_ONLY"  # discovery + events + quote + sell check verified on the real chain
    DEGRADED = "DEGRADED"  # was verified, a recent check failed
    UNVERIFIED = "UNVERIFIED"  # implemented, never proven on the real chain
    DISABLED = "DISABLED"  # inactive venue, or switched off by the operator


class TokenCategory(str, Enum):
    FRESH = "FRESH"
    MIGRATED = "MIGRATED"
    MOMENTUM = "MOMENTUM"
    OTHER = "OTHER"


# The checks that must pass (on the real chain) before a launchpad may trade.
CHECKS = ("ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "LIQUIDITY", "SAFETY", "MIGRATION_DETECTION",
          "TX_MONITORING", "BUY", "SELL")
PAPER_REQUIRED = ("ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "SAFETY")
LIVE_REQUIRED = CHECKS


@dataclass(frozen=True)
class ChainSpec:
    chain: Chain
    name: str
    native_symbol: str
    account_model: str  # "solana" | "evm"
    evm_chain_id: int | None = None
    explorer: str | None = None
    public_rpc: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class LaunchpadSpec:
    key: str
    chain: Chain
    name: str
    lifecycle: Lifecycle
    curve_model: str
    liquidity_model: str
    migration_model: str  # how graduation is detected, or "none"
    execution_model: str
    safety_model: str
    supported_events: tuple[str, ...]
    contracts: dict[str, str] = field(default_factory=dict)
    supports_trading: bool = True
    supports_copy_trading: bool = True
    active: bool = True  # False: the venue itself is inactive (never tradable)
    inactive_reason: str | None = None
    quote_asset: str = ""
    sources: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "chain": self.chain.value, "name": self.name, "lifecycle": self.lifecycle.value,
                "curve_model": self.curve_model, "liquidity_model": self.liquidity_model,
                "migration_model": self.migration_model, "execution_model": self.execution_model,
                "safety_model": self.safety_model, "supported_events": list(self.supported_events),
                "contracts": dict(self.contracts), "supports_trading": self.supports_trading,
                "supports_copy_trading": self.supports_copy_trading, "active": self.active,
                "inactive_reason": self.inactive_reason, "quote_asset": self.quote_asset,
                "sources": list(self.sources), "notes": self.notes}


# --- normalized data ----------------------------------------------------------------------------

@dataclass
class Launch:
    chain: Chain
    launchpad: str
    token: str
    creator: str | None
    created_at: datetime
    tx_hash: str | None
    block: int | None
    name: str | None = None
    symbol: str | None = None
    quote_token: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeEvent:
    chain: Chain
    launchpad: str
    token: str
    trader: str
    is_buy: bool
    token_amount: int
    quote_amount: int  # smallest unit of the quote asset (wei / lamports)
    at: datetime
    tx_hash: str | None
    log_index: int | None
    block: int | None
    fee: int | None = None
    price: Decimal | None = None  # quote per whole token when the event carries it
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        """Idempotency key: the same log is never processed twice."""
        return f"{self.chain.value}:{self.tx_hash}:{self.log_index}"


@dataclass
class Quote:
    ok: bool
    amount_in: int
    amount_out: int | None
    fee: int | None = None
    price_impact: Decimal | None = None
    route: str | None = None
    source: str = ""
    error: str | None = None
    at: datetime | None = None
    # False when computed off-chain from reserves (the contract's formula
    # applied locally) rather than returned by the contract / a simulation.
    exact: bool = True


@dataclass
class TokenState:
    chain: Chain
    launchpad: str
    token: str
    category: TokenCategory
    stage: str  # CURVE | GRADUATING | DEX | KILLED | UNKNOWN
    price: Decimal | None  # quote per whole token
    liquidity_quote: Decimal | None
    progress: Decimal | None  # 0..1 of the curve, when the launchpad has one
    pool: str | None
    buy_tax_bps: int | None
    sell_tax_bps: int | None
    source: str
    at: datetime
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SafetyReport:
    token: str
    verdict: str  # PASS | WARN | FAIL | UNKNOWN (UNKNOWN never counts as safe)
    findings: list[dict[str, Any]]
    sellable: bool | None
    buy_tax_bps: int | None
    sell_tax_bps: int | None
    sources: list[str]
    at: datetime


# --- interfaces ---------------------------------------------------------------------------------

@runtime_checkable
class LaunchpadAdapter(Protocol):
    spec: LaunchpadSpec

    async def discover_launches(self, from_block: int, to_block: int) -> list[Launch]: ...
    async def trades(self, from_block: int, to_block: int) -> list[TradeEvent]: ...
    async def get_token_state(self, token: str) -> TokenState: ...
    async def quote_buy(self, token: str, quote_in: int) -> Quote: ...
    async def quote_sell(self, token: str, tokens_in: int) -> Quote: ...
    async def detect_migration(self, token: str) -> dict[str, Any] | None: ...
    def venue_status(self) -> dict[str, Any]: ...


@runtime_checkable
class ChainAdapter(Protocol):
    spec: ChainSpec

    async def head(self) -> int: ...
    async def native_balance(self, address: str) -> int: ...


@runtime_checkable
class TokenSafetyAdapter(Protocol):
    async def check(self, token: str, state: TokenState | None) -> SafetyReport: ...


@runtime_checkable
class ExecutionProvider(Protocol):
    """Stages: BUILT → SIGNED → SUBMITTED → LANDED → CONFIRMED | FAILED | UNKNOWN.
    Submission is never treated as execution."""

    async def buy(self, token: str, quote_in: int, min_out: int) -> dict[str, Any]: ...
    async def sell(self, token: str, tokens_in: int, min_quote_out: int) -> dict[str, Any]: ...


@runtime_checkable
class WalletActivityAdapter(Protocol):
    async def wallet_trades(self, wallet: str, since: datetime) -> list[TradeEvent]: ...
