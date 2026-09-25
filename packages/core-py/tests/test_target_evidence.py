"""Automatic take-profits refined by resistance and strategy history."""

from decimal import Decimal

from yonixalpha_core.safety.models import FinalDecision, ManualOverrides, TargetContext
from yonixalpha_core.safety.planning import MIN_HISTORY_SAMPLES

from tests.test_safety_gate import decide, healthy


def tps(a):
    return [tp.price.value for tp in a.plan.take_profits]


BASE = decide()
ENTRY = BASE.plan.entry_price
T1, T2, T3 = tps(BASE)


def test_baseline_targets_are_r_multiples():
    assert BASE.decision == FinalDecision.EXECUTE and T1 < T2 < T3


def test_resistance_between_tp1_and_tp2_pulls_tp2_below_it():
    level = (T1 + T2) / 2
    a = decide(healthy(targets=TargetContext(resistance=level, resistance_source="test trades")))
    got = tps(a)
    assert got[0] == T1 and got[2] == T3 and got[1] == level * Decimal("0.995")
    tp2 = a.plan.take_profits[1].price
    assert "resistance" in tp2.method and tp2.inputs["resistance"] == level and tp2.provenance.value == "AUTO"


def test_resistance_outside_tp1_tp2_changes_nothing():
    for level in (T1 * Decimal("0.99"), T2 * Decimal("1.2"), None):
        assert tps(decide(healthy(targets=TargetContext(resistance=level)))) == [T1, T2, T3]


def test_history_caps_the_last_target_only_with_enough_trades():
    mfe = ((T2 + T3) / 2) / ENTRY - 1
    enough = decide(healthy(targets=TargetContext(historical_mfe=mfe, samples=MIN_HISTORY_SAMPLES)))
    assert tps(enough)[:2] == [T1, T2] and tps(enough)[2] == ENTRY * (1 + mfe)
    assert enough.plan.take_profits[2].price.inputs["closed_trades"] == MIN_HISTORY_SAMPLES
    few = decide(healthy(targets=TargetContext(historical_mfe=mfe, samples=MIN_HISTORY_SAMPLES - 1)))
    assert tps(few) == [T1, T2, T3]
    # A history cap below TP2 would break the target order: not applied.
    low = decide(healthy(targets=TargetContext(historical_mfe=(T1 / ENTRY - 1), samples=100)))
    assert tps(low) == [T1, T2, T3]


def test_operator_targets_are_never_adjusted():
    manual = [T1, T2 * Decimal("1.1"), T3 * Decimal("1.3")]
    a = decide(healthy(overrides=ManualOverrides(take_profits=manual),
                       targets=TargetContext(resistance=(T1 + T2) / 2, historical_mfe=Decimal("0.01"), samples=100)))
    assert tps(a) == manual
