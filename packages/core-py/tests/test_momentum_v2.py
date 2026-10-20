"""Momentum v2 checks, the momentum config validation and the strategy
mode semantics (no database)."""

from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.strategies import momentum_v2 as mv2
from yonixalpha_core.strategies.catalog import CATALOG, validate_config
from yonixalpha_core.strategy_modes import execution_view

GOOD = {"return_1m": 0.05, "return_5m": 0.2, "volume_acceleration": 2.0, "buyer_acceleration": 1.8,
        "seller_acceleration": 1.1, "new_wallet_acceleration": 1.5, "trades_1m": 30, "flow_state": "EXPANSION",
        "sell_share": 0.3, "breadth_score": 0.7, "top3_buy_share": 0.3, "creator_sold": False,
        "manufactured_pump_risk": "LOW", "liquidity_sol": 20.0}


def test_every_check_off_is_exactly_v1():
    for v1 in (True, False):
        ev = mv2.evaluate(GOOD, v1, {})
        assert ev.qualified is v1 and ev.version == "1" and ev.enabled == []
        assert len(ev.checks) == len(mv2.CHECKS)  # every check still runs and is recorded
    assert mv2.evaluate(GOOD, True, {}).state == mv2.QUALIFIED
    assert mv2.evaluate(GOOD, False, {}).state == mv2.WATCH


def test_exhaustion_rejects_a_late_entry_and_versions_the_decision():
    late = {**GOOD, "return_5m": 0.9}
    ev = mv2.evaluate(late, True, {"mv2_exhaustion": True})
    assert ev.state == mv2.REJECTED and not ev.qualified and ev.version == "2:exh"
    assert "too late" in ev.reasons[0]
    # the same features with the check off: v1 decides (recorded as a FAIL, not enforced)
    off = mv2.evaluate(late, True, {})
    assert off.qualified and next(c for c in off.checks if c.name == "exhaustion").status == mv2.FAIL


def test_decay_and_concentration_make_it_wait_not_reject():
    fading = {**GOOD, "buyer_acceleration": 0.7}
    ev = mv2.evaluate(fading, True, {"mv2_decay": True})
    assert ev.state == mv2.WAIT and not ev.qualified and "fewer buyers" in ev.reasons[0]
    concentrated = {**GOOD, "top3_buy_share": 0.85}
    ev = mv2.evaluate(concentrated, True, {"mv2_independent_buyers": True})
    assert ev.state == mv2.WAIT and "concentrated" in ev.reasons[0]


def test_anti_dump_rejects_creator_sell_distribution_and_seller_surge():
    for bad in ({"creator_sold": True}, {"flow_state": "DISTRIBUTION"}, {"seller_acceleration": 3.0},
                {"manufactured_pump_risk": "HIGH"}):
        ev = mv2.evaluate({**GOOD, **bad}, True, {"mv2_anti_dump": True})
        assert ev.state == mv2.REJECTED, bad


def test_an_enabled_check_with_missing_input_blocks_the_entry():
    missing = {**GOOD, "return_5m": None}
    ev = mv2.evaluate(missing, True, {"mv2_exhaustion": True})
    assert not ev.qualified and ev.state == mv2.WAIT and "required input" in ev.reasons[0]
    # qualified wallets are not measured for these tokens: enabling it can only block
    assert not mv2.evaluate(GOOD, True, {"mv2_qualified_wallets": True}).qualified


def test_no_features_is_stale_and_score_is_explanation_only():
    assert mv2.evaluate({}, True, {"mv2_exhaustion": True}).state == mv2.STALE
    ev = mv2.evaluate(GOOD, True, {})
    assert 0 <= ev.score <= 1 and ev.qualified  # the score never decides


def test_features_come_from_the_recorded_intelligence():
    intel = {"momentum": {"return_1m": "0.04", "return_5m": "0.25", "buyer_acceleration": 2},
             "flow_state": {"state": "EXPANSION", "sell_share": 0.2},
             "buyer_breadth": {"score": 0.6, "unique_buyers": 12, "components": {"diversity": 0.7}},
             "now": {"price": 2.0, "high": 2.5}}
    f = mv2.features_from_intel(intel, {"curve_progress": "0.5", "creator_sold": False})
    assert f["return_5m"] == 0.25 and f["top3_buy_share"] == 0.3 and f["distance_from_high"] == -0.2
    assert f["curve_progress"] == 0.5 and f["creator_sold"] is False and f["seller_acceleration"] is None
    assert mv2.entry_type(f) == mv2.CONTINUATION
    assert mv2.entry_type({**f, "return_1m": -0.02}) == mv2.PULLBACK


def test_momentum_config_is_validated_and_defaults_are_off():
    e = CATALOG["solana_momentum"]
    assert all(e.defaults[f"mv2_{c}"] is False for c in mv2.CHECKS) and e.defaults["monitor_when_off"] is True
    clean, errors = validate_config("solana_momentum", {"mv2_exhaustion": True, "mv2_max_return_5m": "0.3"})
    assert errors == [] and clean == {"mv2_exhaustion": True, "mv2_max_return_5m": "0.3"}
    for bad in ({"mv2_exhaustion": "yes"}, {"mv2_max_return_5m": "50"}, {"mv2_min_breadth": "1.5"}, {"mv2_unknown": True}):
        assert validate_config("solana_momentum", bad)[1], bad
    assert validate_config("solana_fresh", {"mv2_exhaustion": True})[1]  # momentum-only settings


def test_auto_runs_on_paper_unless_live_is_authorized():
    v = execution_view(StrategyMode.AUTO, GlobalMode.PAPER, True)
    assert v["effective_execution"] == "PAPER" and "live execution unavailable" in v["reason"]
    v = execution_view(StrategyMode.AUTO, GlobalMode.LIVE, False)
    assert v["effective_execution"] == "PAPER" and "locks are closed" in v["reason"]
    assert execution_view(StrategyMode.AUTO, GlobalMode.LIVE, True)["effective_execution"] == "LIVE"
    assert execution_view(StrategyMode.PAPER, GlobalMode.LIVE, True)["effective_execution"] == "PAPER"
    off = execution_view(StrategyMode.OFF, GlobalMode.LIVE, True)
    assert off["effective_execution"] == "NONE" and "never sells" in off["reason"]
