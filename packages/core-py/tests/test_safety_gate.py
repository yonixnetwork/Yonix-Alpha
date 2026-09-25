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


def test_insufficient_liquidity_on_established_token_is_no_trade():
    m = replace(healthy().market, liquidity_quote=Decimal("5"), age_seconds=86400)
    a = decide(healthy(market=m))
    assert a.decision == FinalDecision.NO_TRADE and "INSUFFICIENT_LIQUIDITY" in codes(a)


def test_insufficient_liquidity_on_young_token_waits():
    m = replace(healthy().market, liquidity_quote=Decimal("5"), age_seconds=60)
    a = decide(healthy(market=m))
    assert a.decision == FinalDecision.WAIT
    assert a.status_label == "WAITING_FOR_LIQUIDITY"


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


def test_excessive_transfer_fee_rejects():
    t = replace(healthy().token, transfer_fee_bps=900, extensions=["transferFeeConfig"])
    assert decide(healthy(token=t)).decision == FinalDecision.REJECT


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
    a = decide(healthy(global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO, live_trading_permitted=True))
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
