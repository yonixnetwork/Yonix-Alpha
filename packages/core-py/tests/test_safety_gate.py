"""Spec section 87 risk test matrix, plus hierarchy/approval/ML/mode rules.

Every case starts from one healthy input that must reach EXECUTE, then
changes exactly one thing, so each assertion isolates one gate."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from yonixalpha_core.safety import ConstantProductModel, SafetySettings, assess, format_summary, ratchet_trailing_stop
from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    ExecutionQuote,
    ExecutionTarget,
    FinalDecision,
    GlobalMode,
    HolderInfo,
    ManualOverrides,
    MarketInfo,
    MLInput,
    Observation,
    Provenance,
    RiskLevel,
    StrategyMode,
    StrategySignal,
    TokenProgramInfo,
    TradeFlow,
    Venue,
)
from yonixalpha_core.safety.planning import _loss_fraction

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FRESH = Observation("test", NOW - timedelta(seconds=5))

# pump.fun-like curve: 60 SOL virtual / 30 SOL real, 1.25% fee.
MODEL = ConstantProductModel(Decimal("60"), Decimal("536500000"), Decimal("125"), real_quote_reserve=Decimal("30"))


def healthy(**changes) -> AssessmentInput:
    base = AssessmentInput(
        engine="solana_momentum",
        strategy_name="solana_momentum",
        asset_id="MINT111",
        symbol="TEST",
        now=NOW,
        market=MarketInfo(FRESH, price=MODEL.marginal_price, volatility=Decimal("0.05"),
                          liquidity_quote=Decimal("30"), age_seconds=600, curve_complete=False, migrated=False),
        token=TokenProgramInfo(FRESH, "Tokenkeg", None, None, 6, 10**15),
        holders=HolderInfo(FRESH, Decimal("0.05"), Decimal("0.25"), Decimal("0.03"), 20, 1),
        flow=TradeFlow(FRESH, 300, 40, 30, 10, 25, 8, Decimal("12"), Decimal("4"), Decimal("0.30"), False),
        account=AccountState(Decimal("10"), Decimal("10"), 0, Decimal("0"), Decimal("0"), None, Decimal("0"), False),
        liquidity_model=MODEL,
        signal=StrategySignal("solana_momentum", "1", True, 0.7, ["acceleration"]),
    )
    return replace(base, **changes)


def decide(inp=None, settings=None):
    return assess(inp or healthy(), settings or SafetySettings())


def codes(a):
    return {f.code for f in a.findings}


# --- baseline ---------------------------------------------------------------

def test_healthy_input_executes_in_paper_with_full_auto_plan():
    a = decide()
    assert a.decision == FinalDecision.EXECUTE, a.reasons
    assert a.executable and a.qualified
    assert a.execution_target == ExecutionTarget.PAPER
    p = a.plan
    assert p.complete
    for v in (p.stop_loss, p.position_size, p.max_loss, *[tp.price for tp in p.take_profits]):
        assert v.provenance == Provenance.AUTO
    assert p.trailing.enabled and p.trailing.provenance == Provenance.AUTO


def test_loss_at_stop_after_costs_never_exceeds_max_loss():
    p = decide().plan
    loss = p.position_size.value * _loss_fraction(p.stop_distance_pct, p.entry_cost_bps, p.exit_cost_bps)
    assert loss <= p.max_loss.value * Decimal("1.000001")


def test_summary_mentions_provenance():
    text = "\n".join(format_summary(decide()))
    assert "Stop Loss: AUTO" in text and "TP1: AUTO" in text and "EXECUTABLE" in text


# --- section 87 matrix --------------------------------------------------------

def test_manual_sl_is_validated_and_used():
    entry = MODEL.marginal_price
    a = decide(healthy(overrides=ManualOverrides(stop_loss=entry * Decimal("0.88"))))
    assert a.decision == FinalDecision.EXECUTE
    assert a.plan.stop_loss.provenance == Provenance.MANUAL
    assert a.plan.stop_loss.value == entry * Decimal("0.88")


def test_manual_sl_above_entry_is_rejected_not_replaced():
    a = decide(healthy(overrides=ManualOverrides(stop_loss=MODEL.marginal_price * 2)))
    assert a.decision == FinalDecision.NO_TRADE
    assert "MANUAL_SL_INVALID" in codes(a)


def test_manual_sl_too_wide_is_not_accepted():
    a = decide(healthy(overrides=ManualOverrides(stop_loss=MODEL.marginal_price * Decimal("0.5"))))
    assert a.decision == FinalDecision.NO_TRADE and "MANUAL_SL_TOO_WIDE" in codes(a)


def test_missing_sl_is_calculated_from_volatility():
    a = decide()
    assert a.plan.stop_distance_pct == Decimal("0.10")  # 2 x 5% volatility


def test_manual_position_size_within_caps_is_used():
    a = decide(healthy(overrides=ManualOverrides(position_size_quote=Decimal("0.1"))))
    assert a.plan.position_size.provenance == Provenance.MANUAL
    assert a.plan.position_size.value == Decimal("0.1")


def test_manual_position_size_above_safe_size_is_reduced():
    a = decide(healthy(overrides=ManualOverrides(position_size_quote=Decimal("5"))))
    assert a.decision == FinalDecision.REDUCE_SIZE
    assert "MANUAL_SIZE_REDUCED" in codes(a)
    assert a.plan.position_size.value < Decimal("5")


def test_missing_position_size_is_calculated_and_bound_by_pool_fraction():
    a = decide()
    assert a.plan.binding_cap in ("pool_fraction", "risk")
    assert a.plan.position_size.value <= Decimal("0.3")  # 1% of 30 SOL real liquidity


def test_manual_tp_is_validated_and_used():
    e = MODEL.marginal_price
    a = decide(healthy(overrides=ManualOverrides(take_profits=[e * Decimal("1.2"), e * Decimal("1.4")])))
    assert a.decision == FinalDecision.EXECUTE
    assert [tp.price.provenance for tp in a.plan.take_profits] == [Provenance.MANUAL] * 2


def test_manual_tp_below_breakeven_is_rejected():
    e = MODEL.marginal_price
    a = decide(healthy(overrides=ManualOverrides(take_profits=[e * Decimal("1.001")])))
    assert a.decision == FinalDecision.NO_TRADE and "MANUAL_TP_INVALID" in codes(a)


def test_missing_tp_is_calculated_as_r_multiples():
    a = decide()
    e, d = a.plan.entry_price, a.plan.stop_distance_pct
    assert [tp.price.value for tp in a.plan.take_profits] == [e * (1 + r * d) for r in (1, 2, 3)]


def test_missing_trailing_is_calculated_and_activates_at_tp1():
    t = decide().plan
    assert t.trailing.enabled
    assert t.trailing.activation_price == t.take_profits[0].price.value
    assert t.trailing.distance_pct <= t.stop_distance_pct


def test_trailing_activation_and_max_giveback_are_configurable():
    from yonixalpha_core.safety.settings import validate
    base = decide().plan
    t = decide(settings=SafetySettings(trailing_activation_r=Decimal("1.5"))).plan
    assert t.trailing.activation_price == base.entry_price * (1 + Decimal("1.5") * base.stop_distance_pct)
    assert "activates at 1.5R" in t.trailing.method
    assert t.trailing.distance_pct == base.trailing.distance_pct  # activation changes nothing else
    cap = base.trailing.distance_pct / 2
    c = decide(settings=SafetySettings(trailing_max_giveback_pct=cap)).plan
    assert c.trailing.distance_pct == cap and "trailing_max_giveback_pct" in c.trailing.method
    assert c.stop_loss.value == base.stop_loss.value  # the stop itself is untouched
    # 0 = defaults: exactly today's plan
    d = decide(settings=SafetySettings(trailing_activation_r=Decimal("0"), trailing_max_giveback_pct=Decimal("0"))).plan
    assert d.trailing.to_dict() == base.trailing.to_dict()
    assert validate(SafetySettings(trailing_activation_r=Decimal("-1")))
    assert validate(SafetySettings(trailing_max_giveback_pct=Decimal("0.9")))


def test_trailing_stop_never_loosens():
    stop = ratchet_trailing_stop(None, Decimal("100"), Decimal("0.1"))
    assert stop == Decimal("90")
    assert ratchet_trailing_stop(stop, Decimal("80"), Decimal("0.1")) == Decimal("90")
    assert ratchet_trailing_stop(stop, Decimal("120"), Decimal("0.1")) == Decimal("108")


def _quote(**kw):
    base = dict(observation=FRESH, venue=Venue.JUPITER, size_quote=Decimal("0.3"), buy_route_available=True,
                sell_route_available=True, entry_impact_bps=Decimal("50"), exit_impact_bps=Decimal("60"),
                round_trip_loss_bps=Decimal("300"), fee_bps_per_side=Decimal("30"),
                expected_entry_price=MODEL.marginal_price, expected_exit_price=MODEL.marginal_price)
    base.update(kw)
    return ExecutionQuote(**base)


def test_sell_route_unavailable_is_no_trade():
    a = decide(healthy(liquidity_model=None, quote=_quote(sell_route_available=False)))
    assert a.decision == FinalDecision.NO_TRADE and "NO_SELL_ROUTE" in codes(a)


def test_unverified_route_is_no_trade():
    a = decide(healthy(liquidity_model=None, quote=_quote(sell_route_available=None)))
    assert a.decision == FinalDecision.NO_TRADE and "ROUTE_UNVERIFIED" in codes(a)


def _pool(**market):
    """A migrated token trading on its DEX pool: the pool liquidity minimum applies."""
    return healthy(engine="solana_migration", market=replace(healthy().market, curve_complete=True, migrated=True, **market))


def test_insufficient_liquidity_on_established_token_is_no_trade():
    a = decide(_pool(liquidity_quote=Decimal("5"), age_seconds=86400))
    assert a.decision == FinalDecision.NO_TRADE and "INSUFFICIENT_LIQUIDITY" in codes(a)


def test_insufficient_liquidity_on_young_token_waits():
    # With the migrated USD minimum switched off, a young pool below the SOL
    # minimum still waits for liquidity, as before.
    off = replace(SafetySettings(), migrated_liquidity_check=False)
    a = decide(_pool(liquidity_quote=Decimal("5"), age_seconds=60), off)
    assert a.decision == FinalDecision.WAIT
    assert a.status_label == "WAITING_FOR_LIQUIDITY"
    # With it on (the default), $750 of usable liquidity is below $10,000: NO_TRADE.
    a = decide(replace(_pool(liquidity_quote=Decimal("5"), age_seconds=60), sol_usd=Decimal("150")))
    assert a.decision == FinalDecision.NO_TRADE and "INSUFFICIENT_MIGRATED_LIQUIDITY" in codes(a)
    assert "WAITING_FOR_LIQUIDITY" in codes(a)


def test_curve_token_is_not_blocked_by_the_dex_pool_liquidity_minimum():
    # A fresh pump.fun token: 5 SOL real reserve on the curve, no DEX pool.
    # The 20 SOL pool minimum does not apply; the curve is the market and the
    # exact fill simulation decides executability.
    for engine in ("solana_fresh", "solana_momentum"):
        a = decide(healthy(engine=engine, market=replace(healthy().market, liquidity_quote=Decimal("5"), curve_progress=Decimal("0.07"))))
        assert "INSUFFICIENT_LIQUIDITY" not in codes(a) and "WAITING_FOR_LIQUIDITY" not in codes(a), (engine, a.reasons)
        bc = next(f for f in a.findings if f.code == "BONDING_CURVE_MARKET")
        assert "NO DEX POOL YET" in bc.message and "7% of the curve sold" in bc.message and "simulated on the curve" in bc.message
    # An operator minimum for curve reserves is honoured when set.
    a = decide(healthy(market=replace(healthy().market, liquidity_quote=Decimal("5"), age_seconds=60)),
               SafetySettings(min_curve_liquidity_quote=Decimal("8")))
    assert a.status_label == "WAITING_FOR_LIQUIDITY"


def test_curve_token_without_a_curve_model_is_not_executable():
    a = decide(healthy(liquidity_model=None))
    assert not a.executable and "EXECUTION_UNAVAILABLE" in codes(a)


def test_excessive_price_impact_blocks_at_that_size():
    a = decide(healthy(liquidity_model=None, quote=_quote(entry_impact_bps=Decimal("900"))))
    assert a.decision == FinalDecision.NO_TRADE and "ENTRY_IMPACT" in codes(a)


def test_excessive_exit_impact_blocks():
    a = decide(healthy(liquidity_model=None, quote=_quote(exit_impact_bps=Decimal("900"))))
    assert a.decision == FinalDecision.NO_TRADE and "EXIT_IMPACT" in codes(a)


def test_excessive_round_trip_loss_is_no_trade():
    a = decide(healthy(liquidity_model=None, quote=_quote(round_trip_loss_bps=Decimal("1500"))))
    assert a.decision == FinalDecision.NO_TRADE and "ROUND_TRIP_LOSS" in codes(a)


def test_risk_impossible_without_volatility_or_manual_sl():
    m = replace(healthy().market, volatility=None)
    a = decide(healthy(market=m))
    assert a.decision == FinalDecision.NO_TRADE and "AUTO_SL_NO_VOLATILITY" in codes(a)
    assert not a.plan.complete


def test_risk_impossible_without_equity():
    acct = replace(healthy().account, equity=None)
    a = decide(healthy(account=acct))
    assert a.decision == FinalDecision.NO_TRADE and "EQUITY_UNAVAILABLE" in codes(a)


def test_provider_unavailable_is_no_trade():
    a = decide(healthy(holders=None))
    assert a.decision == FinalDecision.NO_TRADE and "HOLDERS_UNAVAILABLE" in codes(a)


def test_stale_critical_data_is_no_trade():
    stale = Observation("rpc", NOW - timedelta(seconds=600))
    a = decide(healthy(market=replace(healthy().market, observation=stale)))
    assert a.decision == FinalDecision.NO_TRADE and "MARKET_STALE" in codes(a)
    assert a.data_status["market"] == "STALE"


# --- token safety -------------------------------------------------------------

def test_freeze_authority_rejects_as_token_risk():
    t = replace(healthy().token, freeze_authority="FrzAuth")
    a = decide(healthy(token=t))
    assert a.decision == FinalDecision.REJECT
    assert a.status_label == "REJECTED — TOKEN RISK"


def test_mint_authority_rejects_by_default_and_is_configurable():
    t = replace(healthy().token, mint_authority="MintAuth")
    assert decide(healthy(token=t)).decision == FinalDecision.REJECT
    relaxed = SafetySettings(reject_active_mint_authority=False)
    assert decide(healthy(token=t), relaxed).decision == FinalDecision.REQUIRE_MANUAL_APPROVAL


@pytest.mark.parametrize("ext", ["permanentDelegate", "nonTransferable", "transferHook"])
def test_blocking_token2022_extensions_reject(ext):
    t = replace(healthy().token, extensions=[ext])
    assert decide(healthy(token=t)).decision == FinalDecision.REJECT


def test_small_transfer_fee_is_allowed_but_charged_on_exit():
    base_exit = decide().plan.exit_cost_bps
    t = replace(healthy().token, transfer_fee_bps=50, extensions=["transferFeeConfig"])
    a = decide(healthy(token=t))
    assert a.executable
    assert a.plan.exit_cost_bps >= base_exit + 50 - Decimal("1")


def test_token_tax_above_limit_is_no_trade_and_reported():
    t = replace(healthy().token, transfer_fee_bps=900, extensions=["transferFeeConfig"])
    a = decide(healthy(token=t))
    assert a.decision == FinalDecision.NO_TRADE and {"BUY_TAX_EXCESSIVE", "SELL_TAX_EXCESSIVE"} <= codes(a)
    tax = a.reports["tax"]
    assert (tax["buy_tax_pct"], tax["sell_tax_pct"], tax["confidence"]) == ("9", "9", "HIGH") and tax["buy_limit_pct"] == "5"
    assert tax["decision"] == "NO_TRADE"


def test_tax_within_limit_executes_and_protocol_fees_are_not_tax():
    t = replace(healthy().token, transfer_fee_bps=300, extensions=["transferFeeConfig"])
    a = decide(healthy(token=t))
    assert a.decision == FinalDecision.EXECUTE and a.reports["tax"]["sell_tax_pct"] == "3"
    plain = decide(healthy())  # SPL Token mint: the curve's 1%+ trading fee is not a token tax
    assert plain.reports["tax"]["buy_tax_pct"] == "0" and plain.reports["tax"]["confidence"] == "HIGH"
    assert a.reports["tax"]["decision"] == plain.reports["tax"]["decision"] == "PASS"


def test_unknown_tax_is_no_trade_in_auto_and_approval_in_manual():
    t = replace(healthy().token, unparseable_extension=True)
    auto = decide(healthy(token=t, strategy_mode=StrategyMode.AUTO))
    assert auto.decision == FinalDecision.NO_TRADE and "AUTO_NO_APPROVAL" in codes(auto) and "TAX_UNKNOWN" in codes(auto)
    assert auto.reports["tax"]["confidence"] == "UNKNOWN" and auto.reports["tax"]["decision"] == "NO_TRADE"
    manual = decide(healthy(token=t, strategy_mode=StrategyMode.MANUAL))
    assert manual.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert manual.reports["tax"]["decision"] == "REQUIRE_MANUAL_APPROVAL"


def test_auto_mode_never_waits_for_approval():
    m = replace(healthy().market, volatility=Decimal("0.12"))
    a = decide(healthy(market=m, strategy_mode=StrategyMode.AUTO), SafetySettings(max_stop_pct=Decimal("0.30")))
    assert a.decision == FinalDecision.NO_TRADE and "AUTO_NO_APPROVAL" in codes(a)
    ok = decide(healthy(strategy_mode=StrategyMode.AUTO))
    assert ok.decision == FinalDecision.EXECUTE and ok.execution_target == ExecutionTarget.PAPER


def test_sellability_and_liquidity_reports():
    a = decide(healthy())
    assert a.reports["sellability"]["status"] == "SELLABLE" and a.reports["sellability"]["sell_simulated"] is True
    assert a.reports["liquidity"]["entry_impact_bps"] is not None and a.reports["liquidity"]["binding_cap"]
    frozen = decide(healthy(token=replace(healthy().token, freeze_authority="F" * 32)))
    assert frozen.reports["sellability"]["status"] == "NOT SELLABLE"
    assert "FREEZE_AUTHORITY" in frozen.reports["sellability"]["transfer_restrictions"]


def test_default_frozen_accounts_reject():
    t = replace(healthy().token, default_account_state="frozen", extensions=["defaultAccountState"])
    assert decide(healthy(token=t)).decision == FinalDecision.REJECT


def test_blacklist_rejects():
    a = decide(healthy(blacklisted_by="GLOBAL symbol exact 'SCAM'"))
    assert a.decision == FinalDecision.REJECT and "BLACKLISTED" in codes(a)


# --- holders / flow -------------------------------------------------------------

def test_soft_holder_concentration_halves_size_and_needs_approval_above_auto_ceiling():
    full = decide().plan.position_size.value
    h = replace(healthy().holders, top1_share=Decimal("0.20"))
    # Default ceiling is MODERATE: a HIGH warning needs a human.
    a = decide(healthy(holders=h))
    assert a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert a.plan.position_size.value <= full / 2 + Decimal("0.0000001")
    # With the ceiling raised to HIGH, the same warning just reduces size.
    b = decide(healthy(holders=h), SafetySettings(max_risk_level_for_auto="HIGH"))
    assert b.decision == FinalDecision.REDUCE_SIZE
    assert b.plan.position_size.value <= full / 2 + Decimal("0.0000001")


def test_critical_holder_concentration_rejects_and_is_never_averaged_away():
    h = replace(healthy().holders, top1_share=Decimal("0.55"))
    a = decide(healthy(holders=h))
    assert a.decision == FinalDecision.REJECT
    assert a.overall_risk == RiskLevel.CRITICAL
    assert a.category_risk["TOKEN"] == "LOW"


def test_aggregator_only_flow_needs_manual_approval():
    fl = replace(healthy().flow, wallet_level=False, unique_buyers=None, unique_sellers=None, top3_wallet_volume_share=None)
    a = decide(healthy(flow=fl))
    assert a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert a.data_status["flow"] == "DEGRADED"


def test_concentrated_volume_needs_approval():
    fl = replace(healthy().flow, top3_wallet_volume_share=Decimal("0.9"))
    assert decide(healthy(flow=fl)).decision == FinalDecision.REQUIRE_MANUAL_APPROVAL


# --- account / lifecycle ---------------------------------------------------------

def test_kill_switch_is_no_trade():
    acct = replace(healthy().account, kill_switch_engaged=True)
    assert decide(healthy(account=acct)).decision == FinalDecision.NO_TRADE


def test_daily_loss_unknown_fails_closed():
    acct = replace(healthy().account, daily_realized_pnl=None)
    assert "DAILY_PNL_UNAVAILABLE" in codes(decide(healthy(account=acct)))


def test_exposure_cap_leaves_no_room_is_no_trade():
    acct = replace(healthy().account, current_exposure=Decimal("3"))
    a = decide(healthy(account=acct))
    assert a.decision == FinalDecision.NO_TRADE and "SIZE_BELOW_MINIMUM" in codes(a)


def test_loss_cooldown_waits():
    acct = replace(healthy().account, last_loss_at=NOW - timedelta(seconds=30))
    assert decide(healthy(account=acct)).decision == FinalDecision.WAIT


def test_curve_complete_without_pool_waits():
    m = replace(healthy().market, curve_complete=True, migrated=False)
    a = decide(healthy(market=m))
    assert a.decision == FinalDecision.WAIT and "MIGRATION_PENDING" in codes(a)


# --- strategy / ML / modes / approval ---------------------------------------------

def test_unqualified_signal_waits_even_when_safe():
    a = decide(healthy(signal=StrategySignal("s", "1", False, 0.1, ["no acceleration"])))
    assert a.decision == FinalDecision.WAIT and not a.qualified


def test_high_ml_confidence_cannot_override_a_hard_block():
    t = replace(healthy().token, freeze_authority="X")
    a = decide(healthy(token=t, ml=MLInput("m", 3, 0.99)), SafetySettings(min_ml_confidence=0.5))
    assert a.decision == FinalDecision.REJECT


def test_low_ml_confidence_waits():
    a = decide(healthy(ml=MLInput("m", 3, 0.2)), SafetySettings(min_ml_confidence=0.5))
    assert a.decision == FinalDecision.WAIT and "ML_BELOW_MIN" in codes(a)


def test_manual_strategy_mode_requires_approval_then_executes_when_granted():
    a = decide(healthy(strategy_mode=StrategyMode.MANUAL))
    assert a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    b = decide(healthy(strategy_mode=StrategyMode.MANUAL, manual_approval_granted=True))
    assert b.decision == FinalDecision.EXECUTE


def test_manual_approval_never_bypasses_safety():
    t = replace(healthy().token, freeze_authority="X")
    a = decide(healthy(token=t, strategy_mode=StrategyMode.MANUAL, manual_approval_granted=True))
    assert a.decision == FinalDecision.REJECT
    m = replace(healthy().market, observation=Observation("rpc", NOW - timedelta(hours=1)))
    b = decide(healthy(market=m, manual_approval_granted=True))
    assert b.decision == FinalDecision.NO_TRADE


def test_strategy_off_is_no_trade():
    assert decide(healthy(strategy_mode=StrategyMode.OFF)).decision == FinalDecision.NO_TRADE


def test_live_without_env_permission_is_no_trade():
    a = decide(healthy(global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO, live_trading_permitted=False))
    assert a.decision == FinalDecision.NO_TRADE and "LIVE_NOT_PERMITTED" in codes(a)


def test_live_target_only_with_every_permission():
    not_ready = decide(healthy(global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO, live_trading_permitted=True,
                               live_ready=False, live_not_ready_reason="wallet not configured"))
    assert not_ready.decision == FinalDecision.NO_TRADE and "LIVE_NOT_READY" in codes(not_ready)
    a = decide(healthy(global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO, live_trading_permitted=True, live_ready=True))
    assert a.execution_target == ExecutionTarget.LIVE
    b = decide(healthy(global_mode=GlobalMode.PAPER, strategy_mode=StrategyMode.AUTO, live_trading_permitted=True))
    assert b.execution_target == ExecutionTarget.PAPER


def test_risk_above_auto_ceiling_needs_approval():
    m = replace(healthy().market, volatility=Decimal("0.12"))
    a = decide(healthy(market=m), SafetySettings(max_stop_pct=Decimal("0.30")))
    assert a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert "RISK_ABOVE_AUTO_CEILING" in codes(a)


def test_every_non_execute_decision_has_explicit_reasons():
    for inp in (healthy(holders=None), healthy(token=replace(healthy().token, freeze_authority="X")),
                healthy(signal=None)):
        a = decide(inp)
        assert a.reasons and all(r.strip() for r in a.reasons)
        assert a.reasons != ["all safety gates passed"]


def test_own_entry_counts_in_a_young_curves_real_reserve():
    """Our round trip on a bonding curve sells back into a curve that holds
    our own SOL. Capping the exit at the real reserve from BEFORE our buy
    (≈0 on a young curve) turned any round trip into a 100% loss."""
    young = ConstantProductModel(Decimal("30"), Decimal("1073000000"), Decimal("125"), real_quote_reserve=Decimal("0"))
    size = Decimal("0.3")
    buy = young.simulate_buy(size)
    assert young.simulate_sell(buy.amount_out).impact_bps == Decimal(10000)  # the pre-entry cap
    _, sell, loss_bps = young.round_trip(size)
    assert sell.amount_out > 0 and loss_bps < 500  # fees + the conservative double impact
    assert young.max_size_within(Decimal(300), Decimal(500), Decimal(1)) > 0
    from yonixalpha_core.safety.liquidity import side_costs
    entry, exit_ = side_costs(young, size)
    assert entry + exit_ < 1000


# --- fixed per-trade costs (live network/priority fees, unreclaimed rent) ----

def _live(**kw):
    return healthy(global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO, live_trading_permitted=True, live_ready=True, **kw)


def test_fixed_costs_are_counted_in_the_loss_at_the_stop_for_live_trades():
    base = decide(_live()).plan
    fixed = base.max_loss.value * Decimal("0.05")
    a = decide(_live(fixed_cost_quote=fixed, fixed_cost_detail={"total": str(fixed)}))
    assert a.execution_target == ExecutionTarget.LIVE, a.reasons
    p = a.plan
    loss = p.position_size.value * _loss_fraction(p.stop_distance_pct, p.entry_cost_bps, p.exit_cost_bps) + fixed
    assert loss <= p.max_loss.value * Decimal("1.000001")
    # The risk-derived size shrinks by the fixed costs (the final size here is bound by the impact cap).
    assert p.position_size.inputs["risk_size"] < base.position_size.inputs["risk_size"]
    assert p.position_size.value <= base.position_size.value and p.fixed_cost_quote == fixed
    assert p.position_size.inputs["risk_budget_after_fixed_costs"] == p.max_loss.value - fixed
    assert p.breakeven_price > base.breakeven_price  # the fixed costs must be earned back too
    assert p.to_dict()["fixed_cost_detail"] == {"total": str(fixed)}


def test_fixed_costs_above_the_risk_budget_refuse_the_live_trade():
    base = decide(_live()).plan
    a = decide(_live(fixed_cost_quote=base.max_loss.value * Decimal("1.01")))
    assert not a.executable and "FIXED_COSTS_EXCEED_RISK" in codes(a)


def test_a_trade_too_small_to_pay_its_fixed_costs_is_refused_never_enlarged():
    """Regression audit 2026-10-10: LIVE trades of 0.001-0.01 SOL paid 5-11%
    of their size in fixed fees and lost in every size band. A trade whose
    fixed costs exceed max_fixed_cost_pct of the safe size is refused."""
    base = decide(_live()).plan
    fixed = base.position_size.value * Decimal("0.03")  # 3% of the size, under the risk budget
    assert fixed < base.max_loss.value
    a = decide(_live(fixed_cost_quote=fixed))
    assert not a.executable and "FIXED_COSTS_TOO_HIGH" in codes(a)
    f = next(x for x in a.findings if x.code == "FIXED_COSTS_TOO_HIGH")
    assert f.action == FinalDecision.NO_TRADE and "never enlarged" in f.message
    looser = decide(_live(fixed_cost_quote=fixed), replace(SafetySettings(), max_fixed_cost_pct=Decimal("0.10")))
    assert looser.executable, looser.reasons
    assert looser.plan.position_size.value <= base.position_size.value  # nothing was made bigger
    # PAPER charged the LIVE fixed costs pays them in its result and is not refused (LIVE-only rule)
    paper = decide(healthy(fixed_cost_quote=fixed, paper_fixed_costs=True))
    assert "FIXED_COSTS_TOO_HIGH" not in codes(paper)


def test_max_fixed_cost_pct_is_bounded():
    from yonixalpha_core.safety.settings import clamp, validate

    clamped, notes = clamp(replace(SafetySettings(), max_fixed_cost_pct=Decimal("0.5")))
    assert clamped.max_fixed_cost_pct == Decimal("0.10") and notes
    assert "max_fixed_cost_pct must be positive" in validate(replace(SafetySettings(), max_fixed_cost_pct=Decimal(0)))


def test_fixed_live_costs_do_not_change_paper_sizing():
    paper = healthy(fixed_cost_quote=Decimal("100"))  # global mode PAPER: the wallet is not used
    a = decide(paper)
    assert a.executable and a.execution_target == ExecutionTarget.PAPER and a.plan.fixed_cost_quote is None
    assert a.plan.position_size.value == decide().plan.position_size.value


def test_fixed_trade_costs_follow_the_live_settings():
    from yonixalpha_core.live_trading import LiveExecutionSettings, fixed_trade_costs

    on, detail = fixed_trade_costs(LiveExecutionSettings())
    assert on == Decimal("0.000225") and "rent_reclaim_fee" in detail  # 2 x (0.000005 + 0.0001) + 0.000015
    off, detail = fixed_trade_costs(LiveExecutionSettings(auto_reclaim_rent=False))
    assert off == Decimal("0.00172384") and detail["token_account_rent_not_reclaimed"] == "0.00151384"


def test_fixed_costs_too_large_for_the_stop_are_stop_inside_costs():
    base = decide(_live()).plan
    a = decide(_live(fixed_cost_quote=base.max_loss.value * Decimal("0.4")))  # 13% of this size, stop 10%
    assert not a.executable and "STOP_INSIDE_COSTS" in codes(a)
    assert any("plus fixed costs" in f.message for f in a.findings)


def test_fixed_costs_shrink_a_risk_bound_live_size_on_a_small_wallet():
    small = AccountState(Decimal("0.08"), Decimal("0.03"), 0, Decimal("0"), Decimal("0"), None, Decimal("0"), False)
    # The sizing math below with the fixed-cost share allowed up to its 10% ceiling (by default such a
    # trade is refused as FIXED_COSTS_TOO_HIGH, asserted at the end).
    tiny = SafetySettings(min_position_size_quote=Decimal("0.001"), max_fixed_cost_pct=Decimal("0.10"))
    fixed = Decimal("0.000225")  # measured live round trip with the token account closed after the exit
    # A 10% stop: proportional costs 5.5% plus fixed 5.8% at this size leave no room before the stop.
    tight = decide(_live(account=small, fixed_cost_quote=fixed), tiny)
    assert not tight.executable and "STOP_INSIDE_COSTS" in codes(tight)
    # A 16% stop (as measured on the live trades): the trade fits, smaller.
    wide = replace(healthy().market, volatility=Decimal("0.08"))
    base = decide(_live(account=small, market=wide), tiny).plan
    assert base.binding_cap == "risk", base.binding_cap
    p = decide(_live(account=small, market=wide, fixed_cost_quote=fixed), tiny).plan
    assert p.position_size.value < base.position_size.value
    loss = p.position_size.value * _loss_fraction(p.stop_distance_pct, p.entry_cost_bps, p.exit_cost_bps) + fixed
    assert loss <= p.max_loss.value * Decimal("1.000001")
    # Default policy (regression audit 2026-10-10): fixed fees above 2% of this small size refuse the trade.
    default = decide(_live(account=small, market=wide, fixed_cost_quote=fixed),
                     SafetySettings(min_position_size_quote=Decimal("0.001")))
    assert not default.executable and "FIXED_COSTS_TOO_HIGH" in codes(default)


# --- intelligence regime and manipulation (solana.intel) ------------------------------

def _intel(**kw):
    base = {"stage": "FRESH", "regime": {"mayhem": False, "curve_math": {"valid": True, "reason": "ok"}},
            "manipulation": {"level": "NONE", "families": {}, "count": 0}}
    for k, v in kw.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


def test_mayhem_tokens_are_not_traded_by_default_because_curve_math_does_not_hold():
    a = decide(healthy(intel=_intel(regime={"mayhem": True, "curve_math": {"valid": False, "reason": "Mayhem"}})))
    assert not a.executable and "MAYHEM_OR_NONSTANDARD_CURVE" in codes(a)
    manual = decide(healthy(intel=_intel(regime={"mayhem": True, "curve_math": {"valid": False}})),
                    SafetySettings(mayhem_action="REQUIRE_MANUAL_APPROVAL"))
    assert manual.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    broken = decide(healthy(intel=_intel(regime={"curve_math": {"valid": False, "reason": "k not held"}})))
    assert "MAYHEM_OR_NONSTANDARD_CURVE" in codes(broken) and not broken.executable


def test_manipulation_acts_only_at_the_configured_levels():
    high = decide(healthy(intel=_intel(manipulation={"level": "HIGH", "count": 3, "families": {"a": "x", "b": "y", "c": "z"}})))
    assert not high.executable and "MANIPULATION_HIGH" in codes(high)
    med = decide(healthy(intel=_intel(manipulation={"level": "MEDIUM", "count": 2, "families": {"a": "x", "b": "y"}})))
    assert med.executable and "MANIPULATION_MEDIUM" in codes(med)  # WARN by default: reported, not blocking
    low = decide(healthy(intel=_intel(manipulation={"level": "LOW", "count": 1, "families": {"a": "x"}})))
    assert low.executable and not any(c.startswith("MANIPULATION_") for c in codes(low))


def test_post_migration_dump_waits_and_boost_and_instant_bond_are_reported():
    a = decide(healthy(intel=_intel(stage="MIGRATED", post_migration={"state": "DUMPING", "evidence": ["sellers dominate"]},
                                    regime={"instant_bond": True, "boost_window": True, "seconds_since_migration": 40})))
    assert a.decision == FinalDecision.WAIT and {"POST_MIGRATION_DUMPING", "INSTANT_BOND", "BOOST_WINDOW"} <= codes(a)
    ok = decide(healthy(intel=_intel(stage="MIGRATED", post_migration={"state": "RECOVERING"})))
    assert ok.executable  # RECOVERING is a description of the path, never a buy by itself — nor a block


def test_intel_settings_are_validated():
    from yonixalpha_core.safety.settings import clamp, validate
    assert any("mayhem_action" in e for e in validate(SafetySettings(mayhem_action="YOLO")))
    clamped, _ = clamp(SafetySettings(manipulation_high_families=1))
    assert clamped.manipulation_high_families == 2  # one indicator is never HIGH


def test_manufactured_pump_high_applies_its_configured_action_default_warn():
    mp = {"risk": "HIGH", "pattern_duration_seconds": 180, "evidence": ["log_price_r2: 0.97"], "detector_version": "mp-1"}
    warn = decide(healthy(intel=_intel(manufactured_pump=mp)))
    f = next(f for f in warn.findings if f.code == "MANUFACTURED_PUMP_PATTERN")
    assert f.action == FinalDecision.EXECUTE and "not a prediction" in f.message and warn.executable
    blocked = decide(healthy(intel=_intel(manufactured_pump=mp)), replace(SafetySettings(), manufactured_pump_action="NO_TRADE"))
    assert not blocked.executable and "MANUFACTURED_PUMP_PATTERN" in codes(blocked)
    low = decide(healthy(intel=_intel(manufactured_pump={**mp, "risk": "ELEVATED"})))
    assert "MANUFACTURED_PUMP_PATTERN" not in codes(low)


def test_paper_counts_the_same_fixed_costs_as_live_when_set():
    """Audit 2026-10-07 (paper vs live): with paper_execution
    charge_live_fixed_costs the pipeline passes the LIVE round trip's fixed
    costs for a PAPER target too; the plan then sizes exactly as LIVE does
    and refuses what LIVE refuses."""
    base = decide(_live()).plan
    fixed = base.max_loss.value * Decimal("0.05")
    live = decide(_live(fixed_cost_quote=fixed, fixed_cost_detail={"total": str(fixed)})).plan
    a = decide(healthy(fixed_cost_quote=fixed, fixed_cost_detail={"total": str(fixed)}, paper_fixed_costs=True))
    assert a.execution_target == ExecutionTarget.PAPER and a.plan.fixed_cost_quote == fixed
    assert a.plan.position_size.value == live.position_size.value
    assert a.plan.position_size.inputs["risk_budget_after_fixed_costs"] == a.plan.max_loss.value - fixed
    over = decide(healthy(fixed_cost_quote=base.max_loss.value * Decimal("1.01"), paper_fixed_costs=True))
    assert not over.executable and "FIXED_COSTS_EXCEED_RISK" in codes(over)
