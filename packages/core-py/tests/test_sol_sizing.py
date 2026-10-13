"""SOL-denominated sizing and loss limits (2026-10-13).

FIXED_SOL: the configured SOL amount is the size, account equity never
changes it, every cap / the planned loss / the soft-warning reduction refuses
the trade under policy REJECT and reduces it (reported) under REDUCE.
MAX_LOSS_PER_TRADE_SOL is independent of the size. Daily limits: the account
limit counts every strategy, a strategy's own limit only its own losses."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from yonixalpha_core.live_trading import LAMPORTS
from yonixalpha_core.safety import ConstantProductModel, SafetySettings, assess
from yonixalpha_core.safety.gate import account_blockers, token_category
from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    FinalDecision,
    HolderInfo,
    ManualOverrides,
    MarketInfo,
    Observation,
    StrategySignal,
    TokenProgramInfo,
    TradeFlow,
)
from yonixalpha_core.safety.planning import _loss_fraction
from yonixalpha_core.safety.settings import sizing_errors

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
FRESH = Observation("test", NOW - timedelta(seconds=5))
MODEL = ConstantProductModel(Decimal("60"), Decimal("536500000"), Decimal("125"), real_quote_reserve=Decimal("30"))


def account(equity="10", available=None, **kw) -> AccountState:
    e = Decimal(equity)
    return AccountState(e, Decimal(available) if available is not None else e, 0, Decimal("0"), Decimal("0"), None,
                        Decimal("0"), False, **kw)


def healthy(**changes) -> AssessmentInput:
    base = AssessmentInput(
        engine="solana_fresh", strategy_name="fresh_launch_flow", asset_id="MINT111", symbol="TEST", now=NOW,
        market=MarketInfo(FRESH, price=MODEL.marginal_price, volatility=Decimal("0.05"),
                          liquidity_quote=Decimal("30"), age_seconds=600, curve_complete=False, migrated=False),
        token=TokenProgramInfo(FRESH, "Tokenkeg", None, None, 6, 10**15),
        holders=HolderInfo(FRESH, Decimal("0.05"), Decimal("0.25"), Decimal("0.03"), 20, 1),
        flow=TradeFlow(FRESH, 300, 40, 30, 10, 25, 8, Decimal("12"), Decimal("4"), Decimal("0.30"), False),
        account=account(), liquidity_model=MODEL,
        signal=StrategySignal("fresh_launch_flow", "1", True, 0.7, ["acceleration"]),
    )
    return replace(base, **changes)


def fixed(size="0.05", max_loss="0.02", policy="REJECT", **kw) -> SafetySettings:
    return SafetySettings(trade_size_mode="FIXED_SOL", fixed_trade_size_sol=Decimal(size),
                          max_loss_per_trade_sol=Decimal(max_loss), fixed_size_policy=policy, **kw)


def codes(a):
    return {f.code for f in a.findings}


def test_fixed_sol_size_is_used_exactly_and_never_scaled_by_equity():
    for equity in ("1", "10", "100"):
        a = assess(healthy(account=account(equity)), fixed())
        assert a.decision == FinalDecision.EXECUTE, a.reasons
        assert a.plan.position_size.value == Decimal("0.05")
        assert a.plan.max_loss.value == Decimal("0.02") and "FIXED_SOL" in a.plan.max_loss.method
        p = a.plan
        loss = p.position_size.value * _loss_fraction(p.stop_distance_pct, p.entry_cost_bps, p.exit_cost_bps)
        assert p.planned_loss_at_stop == loss and loss <= Decimal("0.02")
        assert p.to_dict()["planned_loss_at_stop"] == str(loss)


def test_risk_pct_mode_is_unchanged_and_capped_by_the_absolute_loss_limit():
    base = assess(healthy(), SafetySettings())
    assert base.plan.max_loss.value == Decimal("0.1")  # 10 SOL x 1%
    capped = assess(healthy(), SafetySettings(max_loss_per_trade_sol=Decimal("0.004")))
    assert capped.plan.max_loss.value == Decimal("0.004") and "max_loss_per_trade_sol" in capped.plan.max_loss.method
    assert capped.plan.position_size.value < base.plan.position_size.value


def test_loss_above_the_per_trade_limit_is_refused_or_reduced_by_policy():
    a = assess(healthy(), fixed(size="0.2", max_loss="0.002"))
    assert a.decision == FinalDecision.NO_TRADE and "TRADE_SIZE_EXCEEDS_RISK" in codes(a)
    assert a.plan.position_size is None
    r = assess(healthy(), fixed(size="0.2", max_loss="0.002", policy="REDUCE"))
    assert r.executable and "FIXED_SIZE_REDUCED" in codes(r), r.reasons
    p = r.plan
    assert p.position_size.value < Decimal("0.2") and p.planned_loss_at_stop <= Decimal("0.002") * Decimal("1.000001")
    assert "reduced" in p.position_size.method


def test_insufficient_balance_is_a_precise_refusal_never_a_smaller_trade():
    a = assess(healthy(account=account("10", available="0.03")), fixed(size="0.05"))
    assert a.decision == FinalDecision.NO_TRADE and "INSUFFICIENT_BALANCE" in codes(a)
    blocked = account_blockers(account("10", available="0.03"), fixed(size="0.05"), NOW)
    assert [b.code for b in blocked] == ["INSUFFICIENT_BALANCE"]  # known before any token data


def test_caps_refuse_the_fixed_size_under_reject_and_reduce_under_reduce():
    small_pool = replace(healthy().market, liquidity_quote=Decimal("3"))  # pool_fraction 1% -> 0.03 SOL
    a = assess(healthy(market=small_pool), fixed(size="0.05"))
    assert "FIXED_SIZE_ABOVE_POOL_FRACTION" in codes(a) and a.decision == FinalDecision.NO_TRADE
    r = assess(healthy(market=small_pool), fixed(size="0.05", policy="REDUCE"))
    assert r.plan.position_size.value == Decimal("0.03") and "FIXED_SIZE_REDUCED" in codes(r)


def test_soft_warning_reduction_needs_policy_reduce():
    volatile = replace(healthy().market, volatility=Decimal("0.10"))  # HIGH_VOLATILITY -> REDUCE_SIZE
    a = assess(healthy(market=volatile), fixed(size="0.05", max_loss="0.05"))
    assert "FIXED_SIZE_REDUCTION_REQUIRED" in codes(a) and not a.executable
    r = assess(healthy(market=volatile), fixed(size="0.05", max_loss="0.05", policy="REDUCE"))
    assert r.plan.position_size.value == Decimal("0.025"), r.reasons


def test_a_conflicting_manual_size_is_refused_not_silently_chosen():
    a = assess(healthy(overrides=ManualOverrides(position_size_quote=Decimal("0.07"))), fixed())
    assert "SIZE_SETTINGS_CONFLICT" in codes(a) and a.decision == FinalDecision.NO_TRADE
    same = assess(healthy(overrides=ManualOverrides(position_size_quote=Decimal("0.05"))), fixed())
    assert same.executable and same.plan.position_size.value == Decimal("0.05")


def test_sizing_settings_are_validated():
    assert sizing_errors(fixed()) == []
    assert sizing_errors(SafetySettings(trade_size_mode="FIXED_SOL"))  # no size, no loss limit
    assert sizing_errors(fixed(size="2"))  # above max_position_size_quote 1
    assert sizing_errors(fixed(size="0.001"))  # below min_position_size_quote 0.01
    assert sizing_errors(fixed(size="0.0500000001"))  # finer than a lamport
    assert sizing_errors(SafetySettings(max_loss_per_trade_sol=Decimal("-1")))
    assert sizing_errors(SafetySettings(trade_size_mode="PCT"))


def test_sol_to_lamports_is_exact():
    size = assess(healthy(), fixed(size="0.123456789", max_loss="0.05")).plan.position_size.value
    assert size == Decimal("0.123456789") and size * LAMPORTS == Decimal(123456789)
    # the LIVE buy limit (live_trading.enter_live): Decimal throughout, rounded down to whole lamports
    max_in = int((size * (1 + Decimal("5") / 100) * LAMPORTS).to_integral_value(ROUND_DOWN))
    assert max_in == 129629628


def test_account_daily_limit_counts_every_strategy_and_the_strategy_limit_its_own():
    s = SafetySettings(max_daily_loss_quote=Decimal("0.02"))  # e.g. the migration engine's own limit
    # account lost 0.06 (another strategy), this strategy nothing; account limit 0.5 not reached
    a = replace(account(), daily_realized_pnl=Decimal("-0.06"), strategy_daily_realized_pnl=Decimal("0"),
                account_daily_loss_limit=Decimal("0.5"))
    assert account_blockers(a, s, NOW) == []
    own = replace(a, strategy_daily_realized_pnl=Decimal("-0.02"))
    assert [b.code for b in account_blockers(own, s, NOW)] == ["STRATEGY_DAILY_LOSS_LIMIT"]
    acct = replace(a, daily_realized_pnl=Decimal("-0.5"))
    assert [b.code for b in account_blockers(acct, s, NOW)] == ["DAILY_LOSS_LIMIT"]
    # without the account limit (older callers): the engine value applies to the account, as before
    legacy = replace(a, account_daily_loss_limit=None, strategy_daily_realized_pnl=None)
    assert [b.code for b in account_blockers(legacy, s, NOW)] == ["DAILY_LOSS_LIMIT"]


def test_one_open_position_per_token_across_strategies():
    held = replace(account(), token_exposure=Decimal("0.03"))
    a = assess(healthy(account=held), SafetySettings())
    assert "TOKEN_ALREADY_HELD" in codes(a) and a.decision == FinalDecision.NO_TRADE


def test_token_category_is_the_tokens_state_not_the_strategys_name():
    s = SafetySettings()
    assert token_category(healthy(), s) == "FRESH"
    near = replace(healthy().market, curve_progress=Decimal("0.8"))
    assert token_category(healthy(market=near), s) == "NEAR_MIGRATION"
    migrated = replace(healthy().market, migrated=True)
    assert token_category(healthy(market=migrated), s) == "MIGRATED"  # a fresh-engine trade on a migrated token
    assert token_category(healthy(engine="solana_migration"), s) == "MIGRATED"
    assert token_category(healthy(engine="solana_momentum"), s) == "MOMENTUM"
    assert assess(healthy(), s).plan.to_dict()["token_category"] == "FRESH"


def test_a_reduced_fixed_size_is_re_checked_against_the_stop_with_its_fixed_fees():
    # fixed fees of 0.004 SOL: 4% of 0.1 SOL is fine, but on the size reduced to the
    # 0.003 SOL loss limit they exceed the stop distance -> refused, never traded at a loss
    inp = healthy(fixed_cost_quote=Decimal("0.004"), paper_fixed_costs=True)
    r = assess(inp, fixed(size="0.1", max_loss="0.0045", policy="REDUCE"))
    msg = next(f.message for f in r.findings if f.code == "STOP_INSIDE_COSTS")
    assert not r.executable and msg.startswith("after reduced to"), r.reasons
