from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from yonixalpha_core.safety.liquidity import BPS, ConstantProductModel, max_size_within_side, side_costs
from yonixalpha_core.safety.models import (
    AccountState,
    ExecutionQuote,
    FinalDecision,
    Finding,
    ManualOverrides,
    Provenance,
    RiskCategory,
    RiskLevel,
    StrategyLevels,
)
from yonixalpha_core.safety.settings import SafetySettings

SIZE_ITERATIONS = 4


@dataclass
class PlannedValue:
    value: Decimal
    provenance: Provenance
    method: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": str(self.value),
            "provenance": self.provenance.value,
            "method": self.method,
            "inputs": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.inputs.items()},
        }


@dataclass
class TakeProfit:
    price: PlannedValue
    exit_fraction: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"price": self.price.to_dict(), "exit_fraction": str(self.exit_fraction)}


@dataclass
class TrailingPlan:
    enabled: bool
    provenance: Provenance
    method: str
    distance_pct: Decimal | None = None
    activation_price: Decimal | None = None
    step_pct: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provenance": self.provenance.value,
            "method": self.method,
            "distance_pct": str(self.distance_pct) if self.distance_pct is not None else None,
            "activation_price": str(self.activation_price) if self.activation_price is not None else None,
            "step_pct": str(self.step_pct) if self.step_pct is not None else None,
        }


@dataclass
class TradePlan:
    entry_price: Decimal | None = None
    stop_loss: PlannedValue | None = None
    stop_distance_pct: Decimal | None = None
    max_loss: PlannedValue | None = None
    position_size: PlannedValue | None = None
    quantity: Decimal | None = None
    take_profits: list[TakeProfit] = field(default_factory=list)
    trailing: TrailingPlan | None = None
    entry_cost_bps: Decimal | None = None
    exit_cost_bps: Decimal | None = None
    binding_cap: str | None = None
    caps: dict = field(default_factory=dict)
    side: str = "LONG"
    breakeven_price: Decimal | None = None
    move_stop_to_breakeven_at_tp1: bool = False
    leverage: Decimal = Decimal(1)
    findings: list[Finding] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (
            self.entry_price is not None
            and self.stop_loss is not None
            and self.max_loss is not None
            and self.position_size is not None
            and self.quantity is not None
            and bool(self.take_profits)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "stop_loss": self.stop_loss.to_dict() if self.stop_loss else None,
            "stop_distance_pct": str(self.stop_distance_pct) if self.stop_distance_pct is not None else None,
            "max_loss": self.max_loss.to_dict() if self.max_loss else None,
            "position_size": self.position_size.to_dict() if self.position_size else None,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "take_profits": [tp.to_dict() for tp in self.take_profits],
            "trailing": self.trailing.to_dict() if self.trailing else None,
            "entry_cost_bps": str(self.entry_cost_bps) if self.entry_cost_bps is not None else None,
            "exit_cost_bps": str(self.exit_cost_bps) if self.exit_cost_bps is not None else None,
            "binding_cap": self.binding_cap,
            "side": self.side,
            "breakeven_price": str(self.breakeven_price) if self.breakeven_price is not None else None,
            "move_stop_to_breakeven_at_tp1": self.move_stop_to_breakeven_at_tp1,
            "leverage": str(self.leverage),
        }


def _block(code: str, message: str, category: RiskCategory = RiskCategory.ACCOUNT) -> Finding:
    return Finding(category, code, RiskLevel.CRITICAL, message, FinalDecision.NO_TRADE, hard_block=True)


def ratchet_trailing_stop(current_stop: Decimal | None, price: Decimal, distance_pct: Decimal, side: str = "LONG") -> Decimal:
    """Trailing stop update that only ever tightens: up for a long, down for
    a short. A trailing stop that loosens as price moves against the
    position is not a stop."""
    if side == "SHORT":
        candidate = price * (1 + distance_pct)
        return candidate if current_stop is None else min(current_stop, candidate)
    candidate = price * (1 - distance_pct)
    return candidate if current_stop is None else max(current_stop, candidate)


def _costs_at(
    size: Decimal,
    model: ConstantProductModel | None,
    quote: ExecutionQuote | None,
    slippage_bps: Decimal,
    transfer_fee_bps: Decimal,
    side: str = "LONG",
) -> tuple[Decimal, Decimal] | None:
    """(entry_cost_bps, exit_cost_bps) at `size`, fees + impact, plus the
    configured slippage allowance and any Token-2022 transfer fee on the
    exit leg. A measured quote for exactly this size wins over the model."""
    if quote is not None and quote.size_quote == size and quote.entry_impact_bps is not None and quote.exit_impact_bps is not None:
        entry = quote.entry_impact_bps + quote.fee_bps_per_side
        exit_ = quote.exit_impact_bps + quote.fee_bps_per_side
    elif model is not None:
        entry, exit_ = side_costs(model, size, side)
    elif quote is not None and quote.entry_impact_bps is not None and quote.exit_impact_bps is not None:
        # A quote for a different size: its impact at a larger size is an
        # upper bound for this smaller one, so reusing it is conservative.
        if size > quote.size_quote:
            return None
        entry = quote.entry_impact_bps + quote.fee_bps_per_side
        exit_ = quote.exit_impact_bps + quote.fee_bps_per_side
    else:
        return None
    return entry, exit_ + slippage_bps + transfer_fee_bps


def _loss_fraction(stop_pct: Decimal, entry_cost_bps: Decimal, exit_cost_bps: Decimal, side: str = "LONG") -> Decimal:
    """Fraction of notional lost if the stop is hit after paying entry and
    exit costs. Long: 1 - (1-c_in)(1-d)(1-c_out). Short: selling N nets
    N(1-c_in); buying back at the stop costs N(1+d)(1+c_out), so the loss is
    (1+d)(1+c_out) - (1-c_in) — slightly more than the long case."""
    c_in = entry_cost_bps / BPS
    c_out = exit_cost_bps / BPS
    if side == "SHORT":
        return (1 + stop_pct) * (1 + c_out) - (1 - c_in)
    return 1 - (1 - c_in) * (1 - stop_pct) * (1 - c_out)


def plan_trade(
    *,
    entry_price: Decimal | None,
    volatility: Decimal | None,
    liquidity_quote: Decimal | None,
    settings: SafetySettings,
    account: AccountState,
    overrides: ManualOverrides,
    model: ConstantProductModel | None,
    quote: ExecutionQuote | None,
    transfer_fee_bps: int | None,
    size_multiplier: Decimal = Decimal(1),
    side: str = "LONG",
    strategy_levels: StrategyLevels | None = None,
    leverage: Decimal = Decimal(1),
) -> TradePlan:
    """Builds the full risk plan or explains exactly why it can't. Every
    missing input that the plan depends on is a NO_TRADE finding: per the
    spec, a trade with undefined risk, size or maximum loss never exists."""
    plan = TradePlan(side=side, leverage=leverage)
    f = plan.findings
    if side not in ("LONG", "SHORT"):
        f.append(_block("SIDE_INVALID", f"unknown side {side}"))
        return plan
    sign = Decimal(1) if side == "LONG" else Decimal(-1)
    levels = strategy_levels or StrategyLevels()
    tfee = Decimal(transfer_fee_bps or 0)
    slip = settings.max_slippage_bps

    if entry_price is None or entry_price <= 0:
        f.append(_block("ENTRY_PRICE_UNAVAILABLE", "entry price unavailable — risk cannot be defined", RiskCategory.DATA))
        return plan
    plan.entry_price = entry_price

    # --- Stop loss ---------------------------------------------------------
    # Precedence: operator value, then the strategy's own level, then AUTO.
    given_sl, sl_prov, sl_method = None, None, None
    if overrides.stop_loss is not None:
        given_sl, sl_prov, sl_method = overrides.stop_loss, Provenance.MANUAL, "operator-specified, validated"
    elif levels.stop_loss is not None:
        given_sl, sl_prov = levels.stop_loss, Provenance.STRATEGY
        sl_method = f"strategy rule ({levels.source or 'strategy'}), validated"
    if given_sl is not None:
        sl = given_sl
        wrong_side = sl >= entry_price if side == "LONG" else sl <= entry_price
        if sl <= 0 or wrong_side:
            where = "below" if side == "LONG" else "above"
            f.append(_block("MANUAL_SL_INVALID", f"{sl_prov.value.lower()} stop {sl} must be above 0 and {where} entry {entry_price} for a {side}"))
            return plan
        stop_pct = sign * (1 - sl / entry_price)
        if stop_pct < settings.min_stop_pct:
            f.append(_block("MANUAL_SL_TOO_TIGHT", f"{sl_prov.value.lower()} stop {stop_pct:.2%} is tighter than min_stop_pct {settings.min_stop_pct:.2%}"))
            return plan
        if stop_pct > settings.max_stop_pct:
            f.append(_block("MANUAL_SL_TOO_WIDE", f"{sl_prov.value.lower()} stop {stop_pct:.2%} is wider than max_stop_pct {settings.max_stop_pct:.2%}"))
            return plan
        plan.stop_loss = PlannedValue(sl, sl_prov, sl_method, {"entry": entry_price, "side": side})
    else:
        if volatility is None:
            f.append(_block("AUTO_SL_NO_VOLATILITY", "automatic stop requires volatility data, which is unavailable", RiskCategory.DATA))
            return plan
        raw_pct = settings.stop_volatility_multiple * volatility
        if raw_pct > settings.max_stop_pct:
            f.append(
                Finding(
                    RiskCategory.MARKET,
                    "VOLATILITY_EXCEEDS_MAX_STOP",
                    RiskLevel.HIGH,
                    f"volatility-based stop {raw_pct:.2%} exceeds max_stop_pct {settings.max_stop_pct:.2%}; "
                    "a tighter stop would sit inside normal noise",
                    FinalDecision.NO_TRADE,
                    hard_block=True,
                )
            )
            return plan
        stop_pct = max(raw_pct, settings.min_stop_pct)
        sl = entry_price * (1 - sign * stop_pct)
        plan.stop_loss = PlannedValue(
            sl,
            Provenance.AUTO,
            "max(stop_volatility_multiple * volatility, min_stop_pct)",
            {"volatility": volatility, "multiple": settings.stop_volatility_multiple, "min_stop_pct": settings.min_stop_pct},
        )
    plan.stop_distance_pct = stop_pct

    # --- Maximum loss ------------------------------------------------------
    if account.equity is None or account.equity <= 0:
        f.append(_block("EQUITY_UNAVAILABLE", "account equity unavailable — maximum loss cannot be calculated"))
        return plan
    configured_max_loss = account.equity * settings.risk_per_trade_pct
    if overrides.max_risk_quote is not None:
        if overrides.max_risk_quote <= 0:
            f.append(_block("MANUAL_MAX_RISK_INVALID", "manual maximum risk must be positive"))
            return plan
        if overrides.max_risk_quote > configured_max_loss:
            f.append(
                Finding(
                    RiskCategory.ACCOUNT,
                    "MANUAL_MAX_RISK_REDUCED",
                    RiskLevel.MODERATE,
                    f"manual maximum risk {overrides.max_risk_quote} exceeds risk_per_trade limit {configured_max_loss:.6f}; reduced",
                    FinalDecision.REDUCE_SIZE,
                )
            )
            plan.max_loss = PlannedValue(configured_max_loss, Provenance.AUTO, "equity * risk_per_trade_pct (manual value exceeded it)")
        else:
            plan.max_loss = PlannedValue(overrides.max_risk_quote, Provenance.MANUAL, "operator-specified, validated")
    else:
        plan.max_loss = PlannedValue(
            configured_max_loss,
            Provenance.AUTO,
            "equity * risk_per_trade_pct",
            {"equity": account.equity, "risk_per_trade_pct": settings.risk_per_trade_pct},
        )
    max_loss = plan.max_loss.value

    # --- Caps (fail closed when a capped quantity can't be measured) -------
    caps: dict[str, Decimal] = {"max_position_size": settings.max_position_size_quote}
    if account.available_balance is None:
        f.append(_block("BALANCE_UNAVAILABLE", "available balance unavailable"))
        return plan
    # Futures margin: the balance supports notional up to balance * leverage.
    caps["available_balance"] = account.available_balance * leverage
    if account.current_exposure is None:
        f.append(_block("EXPOSURE_UNAVAILABLE", "current exposure unavailable — exposure limit can't be enforced"))
        return plan
    caps["total_exposure"] = settings.max_total_exposure_quote - account.current_exposure
    if account.token_exposure is None:
        f.append(_block("TOKEN_EXPOSURE_UNAVAILABLE", "existing exposure to this asset unavailable"))
        return plan
    caps["token_exposure"] = settings.max_token_exposure_quote - account.token_exposure
    if liquidity_quote is None:
        f.append(_block("LIQUIDITY_UNAVAILABLE", "liquidity unavailable — pool-fraction cap can't be enforced", RiskCategory.LIQUIDITY))
        return plan
    caps["pool_fraction"] = settings.max_pool_fraction * liquidity_quote
    if model is not None:
        upper = min(caps.values())
        if upper > 0:
            caps["impact_limit"] = max_size_within_side(
                model, settings.max_entry_impact_bps, settings.max_exit_impact_bps, upper, side
            )

    # --- Size: risk-based, iterated so costs are evaluated at the size used --
    size = min(caps.values())
    risk_size: Decimal | None = None
    costs: tuple[Decimal, Decimal] | None = None
    for _ in range(SIZE_ITERATIONS):
        if size <= 0:
            break
        costs = _costs_at(size, model, quote, slip, tfee, side)
        if costs is None:
            f.append(
                _block(
                    "COSTS_UNDEFINED",
                    "execution costs at the planned size can't be estimated (no liquidity model and no quote for this size)",
                    RiskCategory.EXECUTION,
                )
            )
            return plan
        loss_frac = _loss_fraction(stop_pct, costs[0], costs[1], side)
        if loss_frac <= 0:
            f.append(_block("LOSS_UNDEFINED", "loss fraction non-positive — invalid cost/stop inputs"))
            return plan
        risk_size = max_loss / loss_frac
        new_size = min([risk_size, *caps.values()])
        if new_size == size:
            break
        size = new_size

    if costs is not None and costs[0] + costs[1] >= stop_pct * BPS:
        f.append(
            _block(
                "STOP_INSIDE_COSTS",
                f"round-trip costs {(costs[0] + costs[1]) / 100:.2f}% meet or exceed the stop distance {stop_pct:.2%} — the trade can't be profitable before the stop",
                RiskCategory.EXECUTION,
            )
        )
        return plan

    binding = min(caps, key=lambda k: caps[k])
    plan.caps = dict(caps)
    plan.binding_cap = "risk" if risk_size is not None and risk_size <= caps[binding] else binding

    if overrides.position_size_quote is not None:
        requested = overrides.position_size_quote
        if requested <= 0:
            f.append(_block("MANUAL_SIZE_INVALID", "manual position size must be positive"))
            return plan
        if requested > size:
            f.append(
                Finding(
                    RiskCategory.ACCOUNT,
                    "MANUAL_SIZE_REDUCED",
                    RiskLevel.MODERATE,
                    f"manual size {requested} exceeds safe size {size:.6f} (bound by {plan.binding_cap}); reduced",
                    FinalDecision.REDUCE_SIZE,
                )
            )
            final_size, prov, method = size, Provenance.AUTO, f"manual size reduced to {plan.binding_cap} bound"
        else:
            final_size, prov, method = requested, Provenance.MANUAL, "operator-specified, validated against every cap"
    else:
        final_size, prov, method = size, Provenance.AUTO, f"min(max_loss / loss_fraction, caps) — bound by {plan.binding_cap}"

    if size_multiplier < 1:
        final_size = final_size * size_multiplier
        method += f"; x{size_multiplier} for soft risk warnings"

    # Enforce the invariant directly rather than trusting the iteration
    # above to have converged: size * loss_fraction(costs at that size) must
    # not exceed max_loss. Costs only fall as size falls, so this terminates.
    final_costs = costs
    for _ in range(SIZE_ITERATIONS * 2):
        if final_size <= 0:
            break
        final_costs = _costs_at(final_size, model, quote, slip, tfee, side) or final_costs
        if final_costs is None:
            break
        allowed = max_loss / _loss_fraction(stop_pct, final_costs[0], final_costs[1], side)
        if final_size <= allowed:
            break
        final_size = allowed
        method += "; reduced so loss at stop (after costs) stays within max_loss"

    if final_size <= 0 or final_size < settings.min_position_size_quote or final_costs is None:
        f.append(
            _block(
                "SIZE_BELOW_MINIMUM",
                f"safe size {max(final_size, Decimal(0)):.6f} is below min_position_size {settings.min_position_size_quote} (bound by {plan.binding_cap})",
            )
        )
        return plan

    plan.entry_cost_bps, plan.exit_cost_bps = final_costs
    plan.position_size = PlannedValue(
        final_size, prov, method, {k: v for k, v in caps.items()} | {"risk_size": risk_size, "max_loss": max_loss}
    )
    if side == "LONG":
        plan.quantity = final_size * (1 - plan.entry_cost_bps / BPS) / entry_price
    else:
        plan.quantity = final_size / entry_price

    # --- Take profits -------------------------------------------------------
    round_trip_frac = (plan.entry_cost_bps + plan.exit_cost_bps) / BPS
    breakeven = entry_price * (1 + sign * round_trip_frac)
    plan.breakeven_price = breakeven
    plan.move_stop_to_breakeven_at_tp1 = levels.move_stop_to_breakeven_at_tp1

    def ordered(values: list[Decimal]) -> bool:
        return values == (sorted(values) if side == "LONG" else sorted(values, reverse=True))

    def clears(value: Decimal) -> bool:
        return value > breakeven if side == "LONG" else value < breakeven

    given_tps, tp_prov, tp_method = None, None, None
    if overrides.take_profits:
        given_tps, tp_prov, tp_method = list(overrides.take_profits), Provenance.MANUAL, "operator-specified, validated"
    elif levels.take_profits:
        given_tps, tp_prov = list(levels.take_profits), Provenance.STRATEGY
        tp_method = f"strategy rule ({levels.source or 'strategy'}), validated"
    if given_tps:
        if not ordered(given_tps) or not clears(given_tps[0]):
            direction = "ascending and above" if side == "LONG" else "descending and below"
            f.append(
                _block(
                    "MANUAL_TP_INVALID",
                    f"{tp_prov.value.lower()} take-profits must be {direction} breakeven {breakeven} (entry plus round-trip costs)",
                )
            )
            return plan
        fractions = _fractions_for(len(given_tps), settings)
        plan.take_profits = [TakeProfit(PlannedValue(tp, tp_prov, tp_method), fr) for tp, fr in zip(given_tps, fractions)]
    else:
        tps = []
        for r, fr in zip(settings.tp_r_multiples, settings.tp_exit_fractions):
            price = entry_price * (1 + sign * r * stop_pct)
            method = "entry * (1 + R * stop_distance)" if side == "LONG" else "entry * (1 - R * stop_distance)"
            tps.append(TakeProfit(PlannedValue(price, Provenance.AUTO, method, {"R": r, "stop_distance": stop_pct}), fr))
        if not clears(tps[0].price.value):
            f.append(
                _block(
                    "TP_BELOW_BREAKEVEN",
                    f"first target {tps[0].price.value} doesn't clear breakeven {breakeven} after execution costs",
                    RiskCategory.EXECUTION,
                )
            )
            return plan
        plan.take_profits = tps

    # --- Trailing stop ------------------------------------------------------
    activation = plan.take_profits[0].price.value
    if overrides.trailing_distance_pct is not None:
        dist = overrides.trailing_distance_pct
        if dist <= 0 or dist > settings.max_stop_pct:
            f.append(_block("MANUAL_TRAILING_INVALID", f"manual trailing distance must be in (0, {settings.max_stop_pct}]"))
            return plan
        plan.trailing = TrailingPlan(True, Provenance.MANUAL, "operator-specified, validated", dist, activation, dist / 4)
    elif volatility is not None:
        dist = max(settings.trailing_volatility_multiple * volatility, settings.min_trailing_pct)
        dist = min(dist, stop_pct)
        plan.trailing = TrailingPlan(
            True,
            Provenance.AUTO,
            "max(trailing_volatility_multiple * volatility, min_trailing_pct), capped at stop distance; activates at TP1",
            dist,
            activation,
            dist / 4,
        )
    else:
        plan.trailing = TrailingPlan(False, Provenance.AUTO, "no volatility data — fixed stop only")

    return plan


def _fractions_for(count: int, settings: SafetySettings) -> list[Decimal]:
    if count == len(settings.tp_exit_fractions):
        return list(settings.tp_exit_fractions)
    return [Decimal(1) / count] * count
