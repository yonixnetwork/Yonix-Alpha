"""Stop-gap position sizing (2026-10-20, memecoin refocus Phase 5).

Paper evidence (Oct 15-17): curve-token stops filled at -44.7% and -53.9%;
a 0.397 SOL fresh position sized for a ~0.02 SOL loss at a ~5% stop lost
0.178 SOL. The planner now sizes Solana positions as if the loss at the stop
were at least sizing_stop_gap_curve_pct (bonding curve) or
sizing_stop_gap_pool_pct (migrated pool). The stop level is unchanged and
sizes only ever shrink."""

from dataclasses import replace
from decimal import Decimal

from yonixalpha_core.safety import SafetySettings, assess
from yonixalpha_core.safety.gate import sizing_gap_pct
from yonixalpha_core.safety.models import FinalDecision
from yonixalpha_core.safety.planning import _loss_fraction
from yonixalpha_core.safety.settings import validate

from tests.test_sol_sizing import account, fixed, healthy

OFF = {"sizing_stop_gap_curve_pct": Decimal(0), "sizing_stop_gap_pool_pct": Decimal(0)}


def _migrated(**kw):
    return healthy(engine="solana_migration", market=replace(healthy().market, curve_complete=True, migrated=True), **kw)


def test_curve_token_is_sized_for_the_gap_and_the_stop_is_unchanged():
    old = assess(healthy(), SafetySettings(**OFF)).plan
    new = assess(healthy(), SafetySettings()).plan
    assert new.stop_loss.value == old.stop_loss.value and new.stop_distance_pct == old.stop_distance_pct
    assert new.sizing_loss_pct == Decimal("0.50") and old.sizing_loss_pct == old.stop_distance_pct
    assert new.position_size.value < old.position_size.value
    # the loss if the stop fills 50% below entry stays within the same budget
    assert new.planned_loss_at_gap <= new.max_loss.value * Decimal("1.000001")
    assert new.planned_loss_at_stop < new.planned_loss_at_gap
    assert "stop-gap assumption 50%" in new.position_size.method
    d = new.to_dict()
    assert d["sizing_loss_pct"] == "0.50" and d["planned_loss_at_gap"] is not None


def test_the_oct_15_fresh_loss_would_have_stayed_inside_its_budget():
    """MEIZHU: 0.397 SOL position, trailing stop filled at -44.7% (-0.178 SOL).
    With the gap assumption the size is budget / ~0.5, so the same fill
    would lose about the planned budget, not 9x it."""
    s = SafetySettings(max_loss_per_trade_sol=Decimal("0.02"))
    p = assess(healthy(), s).plan
    loss_at_the_real_fill = p.position_size.value * _loss_fraction(Decimal("0.447"), p.entry_cost_bps, p.exit_cost_bps)
    assert loss_at_the_real_fill <= Decimal("0.02")


def test_migrated_pools_use_the_pool_gap():
    s = SafetySettings()
    assert sizing_gap_pct(_migrated(), s) == Decimal("0.25")
    assert sizing_gap_pct(healthy(), s) == Decimal("0.50")
    assert sizing_gap_pct(healthy(engine="solana_momentum"), s) == Decimal("0.50")
    # The plan is built even though this fixture is refused later (no SOL/USD for the liquidity check).
    p = assess(_migrated(), s).plan
    assert p.position_size is not None and p.sizing_loss_pct == max(Decimal("0.25"), p.stop_distance_pct)
    assert p.planned_loss_at_gap <= p.max_loss.value * Decimal("1.000001")


def test_other_engines_are_not_affected():
    assert sizing_gap_pct(healthy(engine="binance_futures"), SafetySettings()) == Decimal(0)


def test_a_stop_wider_than_the_gap_keeps_the_stop():
    s = SafetySettings(sizing_stop_gap_curve_pct=Decimal("0.06"))
    p = assess(healthy(market=replace(healthy().market, volatility=Decimal("0.05"))), s).plan
    assert p.sizing_loss_pct == max(Decimal("0.06"), p.stop_distance_pct)


def test_off_is_the_previous_sizing():
    a = assess(healthy(), SafetySettings(**OFF)).plan
    loss = a.position_size.value * _loss_fraction(a.stop_distance_pct, a.entry_cost_bps, a.exit_cost_bps)
    assert a.planned_loss_at_stop == loss and a.planned_loss_at_gap == loss


def test_fixed_sol_refuses_or_reduces_a_size_whose_gap_loss_exceeds_the_limit():
    gap = {"sizing_stop_gap_curve_pct": Decimal("0.5")}
    a = assess(healthy(account=account("10")), fixed(size="0.05", max_loss="0.02", **gap))
    assert a.decision == FinalDecision.NO_TRADE and "sized for a stop gap of 50%" in " ".join(a.reasons)
    r = assess(healthy(account=account("10")), fixed(size="0.05", max_loss="0.02", policy="REDUCE", **gap))
    assert r.executable, r.reasons
    assert r.plan.position_size.value < Decimal("0.05") and r.plan.planned_loss_at_gap <= Decimal("0.02") * Decimal("1.000001")
    ok = assess(healthy(account=account("10")), fixed(size="0.03", max_loss="0.02", **gap))
    assert ok.executable and ok.plan.position_size.value == Decimal("0.03"), ok.reasons


def test_the_gap_settings_are_validated():
    assert any("sizing_stop_gap_curve_pct" in e for e in validate(SafetySettings(sizing_stop_gap_curve_pct=Decimal("1"))))
    assert any("sizing_stop_gap_pool_pct" in e for e in validate(SafetySettings(sizing_stop_gap_pool_pct=Decimal("-0.1"))))
    assert not [e for e in validate(SafetySettings()) if "sizing_stop_gap" in e]
