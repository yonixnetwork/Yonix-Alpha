from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class DataQuality(StrEnum):
    """Per spec section 52. Every signal must know whether its inputs are
    reliable — this is the caller's honest assessment of the data feeding a
    decision, never guessed by the risk engine itself.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass
class RiskConfig:
    """Every field is optional — None means "not enforced," per the spec's
    own instruction not to assume every limit is configured. Named to
    mirror yonixalpha_core.config.Settings' TRADING_ENABLED/
    LIVE_TRADING_ENABLED/MAX_DAILY_LOSS/MAX_POSITION_SIZE/MAX_SLIPPAGE/
    MAX_OPEN_POSITIONS fields (declared in Phase 1, unused until now) —
    callers build this from Settings rather than inventing a second
    config surface. min_liquidity/max_price_impact_bps/max_leverage/
    cooldown_after_loss_seconds have no Settings equivalent yet (no
    engine produces liquidity/price-impact/leverage data to check against
    today) and default to unenforced until one does.
    """

    trading_enabled: bool
    live_trading_enabled: bool
    max_position_size: Decimal | None = None
    max_portfolio_exposure: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_open_positions: int | None = None
    max_slippage_bps: Decimal | None = None
    min_liquidity: Decimal | None = None
    max_price_impact_bps: Decimal | None = None
    max_leverage: int | None = None
    cooldown_after_loss_seconds: int | None = None


@dataclass
class RiskContext:
    """The live state a proposed trade is being evaluated against. Every
    field the caller cannot honestly supply should be left None/default
    rather than guessed — evaluate() only enforces the checks it has real
    data for, per config.
    """

    now: datetime
    data_quality: DataQuality
    kill_switch_engaged: bool = False
    proposed_position_size: Decimal = Decimal(0)
    current_portfolio_exposure: Decimal = Decimal(0)
    daily_realized_pnl: Decimal = Decimal(0)
    open_position_count: int = 0
    proposed_slippage_bps: Decimal | None = None
    available_liquidity: Decimal | None = None
    proposed_price_impact_bps: Decimal | None = None
    proposed_leverage: int | None = None
    last_loss_at: datetime | None = None


@dataclass
class RiskVerdict:
    approved: bool
    reasons: list[str] = field(default_factory=list)


def evaluate(config: RiskConfig, context: RiskContext) -> RiskVerdict:
    """Per spec sections 15/36/38: every trade passes through here, and
    risk has final authority — ML or any signal source can produce a
    recommendation, never an override. Collects every violated check
    rather than stopping at the first, so a rejection is fully explained
    (useful both for the audit trail and for an operator debugging why a
    trade never fired). Kill switch and the enabled flags are checked
    first and unconditionally, regardless of what else is configured —
    per section 37, the kill switch "must work even if the ML system is
    malfunctioning."
    """
    reasons: list[str] = []

    if context.kill_switch_engaged:
        reasons.append("kill switch engaged")
    if not config.trading_enabled:
        reasons.append("TRADING_ENABLED is false")
    if not config.live_trading_enabled:
        reasons.append("LIVE_TRADING_ENABLED is false")
    if context.data_quality in (DataQuality.STALE, DataQuality.UNAVAILABLE):
        reasons.append(f"data quality is {context.data_quality.value}")

    if config.max_position_size is not None and context.proposed_position_size > config.max_position_size:
        reasons.append(
            f"proposed position size {context.proposed_position_size} exceeds max_position_size {config.max_position_size}"
        )

    if config.max_portfolio_exposure is not None:
        total_exposure = context.current_portfolio_exposure + context.proposed_position_size
        if total_exposure > config.max_portfolio_exposure:
            reasons.append(
                f"portfolio exposure {total_exposure} would exceed max_portfolio_exposure {config.max_portfolio_exposure}"
            )

    if config.max_daily_loss is not None and context.daily_realized_pnl <= -config.max_daily_loss:
        reasons.append(f"daily realized PnL {context.daily_realized_pnl} has hit max_daily_loss {config.max_daily_loss}")

    if config.max_open_positions is not None and context.open_position_count >= config.max_open_positions:
        reasons.append(f"open position count {context.open_position_count} at/above max_open_positions {config.max_open_positions}")

    if (
        config.max_slippage_bps is not None
        and context.proposed_slippage_bps is not None
        and context.proposed_slippage_bps > config.max_slippage_bps
    ):
        reasons.append(f"proposed slippage {context.proposed_slippage_bps}bps exceeds max_slippage_bps {config.max_slippage_bps}")

    if (
        config.min_liquidity is not None
        and context.available_liquidity is not None
        and context.available_liquidity < config.min_liquidity
    ):
        reasons.append(f"available liquidity {context.available_liquidity} below min_liquidity {config.min_liquidity}")

    if (
        config.max_price_impact_bps is not None
        and context.proposed_price_impact_bps is not None
        and context.proposed_price_impact_bps > config.max_price_impact_bps
    ):
        reasons.append(
            f"proposed price impact {context.proposed_price_impact_bps}bps exceeds max_price_impact_bps {config.max_price_impact_bps}"
        )

    if config.max_leverage is not None and context.proposed_leverage is not None and context.proposed_leverage > config.max_leverage:
        reasons.append(f"proposed leverage {context.proposed_leverage}x exceeds max_leverage {config.max_leverage}x")

    if config.cooldown_after_loss_seconds is not None and context.last_loss_at is not None:
        elapsed = (context.now - context.last_loss_at).total_seconds()
        if elapsed < config.cooldown_after_loss_seconds:
            remaining = config.cooldown_after_loss_seconds - elapsed
            reasons.append(f"cooldown after loss active, {remaining:.0f}s remaining")

    return RiskVerdict(approved=len(reasons) == 0, reasons=reasons)
