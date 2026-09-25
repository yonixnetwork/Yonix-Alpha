from decimal import Decimal

import pytest

from yonixalpha_core.safety import ConstantProductModel, SafetySettings, clamp, settings_from_dict, settings_to_dict, validate
from yonixalpha_core.safety.models import FinalDecision
from yonixalpha_core.safety.rules import (
    BlacklistRule,
    CustomRule,
    evaluate_custom_rules,
    match_blacklist,
    validate_blacklist_rule,
    validate_custom_rule,
)


# --- settings -------------------------------------------------------------------

def test_clamp_enforces_hard_limits_and_reports_each_change():
    s, notes = clamp(SafetySettings(risk_per_trade_pct=Decimal("0.5"), max_data_age_seconds=9999))
    assert s.risk_per_trade_pct == Decimal("0.02")
    assert s.max_data_age_seconds == 300
    assert len(notes) == 2


def test_clamp_leaves_valid_settings_alone():
    s, notes = clamp(SafetySettings())
    assert notes == [] and s == SafetySettings()


def test_settings_round_trip_through_json():
    original = SafetySettings(max_entry_impact_bps=Decimal("250"), min_ml_confidence=0.6)
    assert settings_from_dict(settings_to_dict(original)) == original


def test_settings_from_dict_ignores_unknown_keys_and_keeps_defaults():
    s = settings_from_dict({"nonsense": 1, "max_open_positions": "5"})
    assert s.max_open_positions == 5 and s.risk_per_trade_pct == SafetySettings().risk_per_trade_pct


def test_validate_catches_inconsistent_settings():
    errors = validate(SafetySettings(min_stop_pct=Decimal("0.4"), max_stop_pct=Decimal("0.3"),
                                     tp_exit_fractions=(Decimal("0.9"), Decimal("0.9"), Decimal("0.9"))))
    assert any("min_stop_pct" in e for e in errors)
    assert any("sum above 1" in e for e in errors)
    assert validate(SafetySettings()) == []


# --- blacklist ------------------------------------------------------------------

RULES = [
    BlacklistRule("1", "GLOBAL", "symbol", "SCAM", "exact"),
    BlacklistRule("2", "solana_fresh", "name", "*elon*", "pattern"),
    BlacklistRule("3", "GLOBAL", "mint", "DisabledMint", "exact", enabled=False),
]


def test_blacklist_exact_match_is_case_insensitive():
    assert match_blacklist(RULES, "solana_momentum", "x", "scam", "m")


def test_blacklist_pattern_respects_scope():
    assert match_blacklist(RULES, "solana_fresh", "Baby ELON Coin", "BEC", "m")
    assert match_blacklist(RULES, "solana_momentum", "Baby ELON Coin", "BEC", "m") is None


def test_disabled_rule_never_matches():
    assert match_blacklist(RULES, "solana_fresh", "n", "s", "DisabledMint") is None


def test_wildcard_only_pattern_is_rejected():
    assert validate_blacklist_rule("GLOBAL", "symbol", "**", "pattern")
    assert validate_blacklist_rule("GLOBAL", "symbol", "PEPE*", "pattern") == []


@pytest.mark.parametrize("args", [("NOPE", "symbol", "x", "exact"), ("GLOBAL", "ticker", "x", "exact"), ("GLOBAL", "symbol", "", "exact")])
def test_invalid_blacklist_rules_are_refused(args):
    assert validate_blacklist_rule(*args)


# --- custom rules -----------------------------------------------------------------

def test_custom_rule_fires_on_condition():
    rules = [CustomRule("1", "tiny liquidity", "liquidity_quote", "<", "50", "WAIT", "GLOBAL")]
    assert evaluate_custom_rules(rules, "solana_fresh", {"liquidity_quote": 20}) == [("tiny liquidity", FinalDecision.WAIT)]
    assert evaluate_custom_rules(rules, "solana_fresh", {"liquidity_quote": 80}) == []


def test_custom_rule_with_missing_field_does_not_fire():
    rules = [CustomRule("1", "r", "unique_buyers", "<", "5", "REJECT", "GLOBAL")]
    assert evaluate_custom_rules(rules, "x", {}) == []


def test_custom_rule_validation():
    fields = {"liquidity_quote"}
    assert validate_custom_rule("liquidity_quote", "<", "10", "REJECT", fields) == []
    assert validate_custom_rule("liquidity_quote", "~=", "ten", "EXPLODE", fields)


# --- liquidity model ---------------------------------------------------------------

def test_constant_product_buy_matches_closed_form_without_fee():
    m = ConstantProductModel(Decimal("100"), Decimal("1000"), Decimal("0"))
    buy = m.simulate_buy(Decimal("10"))
    assert buy.amount_out == Decimal("1000") * Decimal("10") / Decimal("110")
    assert buy.impact_bps == Decimal("1000")  # impact = dS / S for a CPMM


def test_round_trip_without_fee_reflects_both_impacts():
    m = ConstantProductModel(Decimal("100"), Decimal("1000"), Decimal("0"))
    _, _, loss = m.round_trip(Decimal("10"))
    # Exiting against unchanged reserves costs 2x/(S+2x) = 20/120.
    assert abs(loss - Decimal("20") / Decimal("120") * 10000) < Decimal("0.0001")


def test_fee_is_charged_on_both_legs():
    m = ConstantProductModel(Decimal("1000000"), Decimal("1000000"), Decimal("100"))
    _, _, loss = m.round_trip(Decimal("1"))
    assert Decimal("199") < loss < Decimal("201")


def test_sell_cannot_return_more_than_real_reserves():
    m = ConstantProductModel(Decimal("60"), Decimal("500"), Decimal("0"), real_quote_reserve=Decimal("1"))
    assert m.simulate_sell(Decimal("400")).amount_out == Decimal("1")


def test_max_size_within_respects_both_limits():
    m = ConstantProductModel(Decimal("60"), Decimal("500000"), Decimal("100"))
    size = m.max_size_within(Decimal("100"), Decimal("100"), Decimal("50"))
    buy = m.simulate_buy(size)
    assert buy.impact_bps <= 100 and m.simulate_sell(buy.amount_out).impact_bps <= 100
    assert size > Decimal("0.5")


def test_invalid_reserves_are_refused():
    with pytest.raises(ValueError):
        ConstantProductModel(Decimal("0"), Decimal("1"), Decimal("0"))
