"""EVM trading settings (platform_settings key "evm_trading").

Amounts are per chain in that chain's native asset (BNB on BSC, ETH on
Robinhood Chain), so the Solana risk settings (in SOL) never size an EVM
position. Percentages (stop, take-profit, trailing) come from the shared
risk settings through safety.store.load_settings, exactly as for Solana;
only the quote-denominated fields are replaced from here.

Defaults are deliberately small. Nothing here enables LIVE trading: EVM
execution is paper only in this phase.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import PlatformSetting

KEY = "evm_trading"
CATEGORIES = ("FRESH", "MIGRATED", "MOMENTUM")


@dataclass(frozen=True)
class ChainTradingSettings:
    paper_entries_enabled: bool = True
    position_size: Decimal = Decimal("0.02")
    max_open_positions: int = 3
    max_total_exposure: Decimal = Decimal("0.1")
    max_daily_loss: Decimal = Decimal("0.05")
    min_liquidity: Decimal = Decimal("0.5")
    max_round_trip_loss_bps: int = 2500
    max_buy_tax_bps: int = 1000
    max_sell_tax_bps: int = 1000
    confirmations: int = 3
    backfill_minutes: int = 10
    max_blocks_per_pass: int = 2000
    # Discovery further behind than this (after an RPC outage or rate limits)
    # jumps to the last backfill_minutes instead of replaying hours of old
    # events: a real-time pipeline trades nothing on a stale view. The
    # skipped block range is logged and alerted, never silent. 0 = never skip.
    max_lag_minutes: int = 60
    # Master §56-57: native coin kept back for transaction fees (never
    # traded), and the gas one swap is budgeted at. Before an entry the
    # wallet (paper or live) must cover the buy's and the sell's estimated
    # gas (eth_gasPrice x gas_units_per_swap each) plus this reserve, or the
    # entry is NO_TRADE: INSUFFICIENT GAS.
    gas_reserve: Decimal = Decimal("0.002")
    gas_units_per_swap: int = 300_000


ROBINHOOD_DEFAULTS = ChainTradingSettings(position_size=Decimal("0.005"), max_total_exposure=Decimal("0.03"),
                                          max_daily_loss=Decimal("0.015"), min_liquidity=Decimal("0.2"),
                                          confirmations=1, max_blocks_per_pass=10000, gas_reserve=Decimal("0.0005"))


@dataclass(frozen=True)
class EvmTradingSettings:
    bsc: ChainTradingSettings = field(default_factory=ChainTradingSettings)
    robinhood: ChainTradingSettings = field(default_factory=lambda: ROBINHOOD_DEFAULTS)
    entry_categories: tuple[str, ...] = CATEGORIES
    fresh_max_age_minutes: int = 30
    migrated_window_minutes: int = 60
    stats_window_seconds: int = 300
    fresh_min_buys: int = 5
    fresh_min_unique_buyers: int = 4
    momentum_min_buys: int = 10
    momentum_min_unique_buyers: int = 6
    min_net_buy_ratio: Decimal = Decimal("0.55")  # buy volume / (buy + sell volume) in the window
    max_curve_progress_pons_v2: Decimal = Decimal("0.85")  # graduation → Uniswap V4 (not tradable here)
    allow_safety_warn: bool = False
    reentry_cooldown_hours: int = 24
    honeypot_is_enabled: bool = False  # BSC enrichment only, never the sole check

    def chain(self, chain: str) -> ChainTradingSettings:
        return getattr(self, chain)

    def to_dict(self) -> dict[str, Any]:
        def conv(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, tuple):
                return list(v)
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            return v
        return conv(asdict(self))


def _coerce(cls, data: dict[str, Any], base, errors: list[str], prefix: str = ""):
    kw = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v, cur = data[f.name], getattr(base, f.name)
        name = prefix + f.name
        try:
            if isinstance(cur, bool):
                if not isinstance(v, bool):
                    raise ValueError("must be true or false")
                kw[f.name] = v
            elif isinstance(cur, Decimal):
                d = Decimal(str(v))
                if d < 0:
                    raise ValueError("must not be negative")
                kw[f.name] = d
            elif isinstance(cur, int):
                i = int(v)
                if i < 0:
                    raise ValueError("must not be negative")
                kw[f.name] = i
            elif isinstance(cur, tuple):
                vals = tuple(str(x).upper() for x in v)
                bad = [x for x in vals if x not in CATEGORIES]
                if bad:
                    raise ValueError(f"unknown categories {bad}")
                kw[f.name] = vals
            elif isinstance(cur, ChainTradingSettings):
                kw[f.name] = _coerce(ChainTradingSettings, v or {}, cur, errors, name + ".")
        except (ValueError, TypeError, InvalidOperation) as exc:
            errors.append(f"{name}: {exc}")
    return replace(base, **kw)


def parse(data: dict[str, Any] | None) -> tuple[EvmTradingSettings, list[str]]:
    errors: list[str] = []
    s = _coerce(EvmTradingSettings, data or {}, EvmTradingSettings(), errors)
    for c in ("bsc", "robinhood"):
        cs = s.chain(c)
        if cs.position_size <= 0:
            errors.append(f"{c}.position_size must be positive")
        if cs.position_size > cs.max_total_exposure:
            errors.append(f"{c}.position_size must not exceed max_total_exposure")
        if cs.max_round_trip_loss_bps > 9000:
            errors.append(f"{c}.max_round_trip_loss_bps must be at most 9000")
        if not 21_000 <= cs.gas_units_per_swap <= 5_000_000:
            errors.append(f"{c}.gas_units_per_swap must be between 21000 and 5000000")
    if s.min_net_buy_ratio > 1:
        errors.append("min_net_buy_ratio must be at most 1")
    return s, errors


async def load(session: AsyncSession) -> EvmTradingSettings:
    row = await session.get(PlatformSetting, KEY)
    s, errors = parse(dict(row.value) if row else None)
    return EvmTradingSettings() if errors else s  # a row that no longer validates is not used
