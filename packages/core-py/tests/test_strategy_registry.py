"""Strategy portfolio and routing (regression recovery, 2026-10-10): each
category strategy fires on its own signal, not on another strategy's; the
router picks one strategy per token from its category, or NO_TRADE; smart
wallets never select; the registry covers every strategy; the demotion rule
needs statistical evidence (never one or two losses) and never promotes.
Deterministic synthetic pump.fun curves; no network, no database."""

from datetime import timedelta

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import strategy_registry as reg
from yonixalpha_core.testing.curve_sim import T0, Curve

CREATED = int(T0.timestamp())


def evaluate(c: Curve, at: float, wallets=None):
    f = ei.early_features(c.trades, T0 + timedelta(seconds=at), created_ts=CREATED, creator="creator",
                          complete_history=True, fee_bps=125)
    return ei.evaluate(f, ei.EntryConfig(), wallets)


def dec(res, name):
    return res["strategies"][name]["decision"]


def broadening_without_acceleration() -> Curve:
    """More and more independent buyers while SOL inflow per 10 s falls."""
    c = Curve()
    for i in range(8):
        c.buy(1 + i * 2.5, f"a{i}", 0.35)
    for i in range(6):
        c.buy(21 + i * 1.6, f"b{i}", 0.2)
    for i in range(7):
        c.buy(31 + i * 1.3, f"c{i}", 0.15)
    return c


def range_breakout() -> Curve:
    """A five-minute-old token that pulled back, ranged, then broke its high."""
    c = Curve()
    for i in range(10):
        c.buy(1 + i * 25, f"a{i}", 0.3)
    for i in range(3):
        c.sell(260 + i * 3, f"a{i}", 1.0)
    for i in range(3):
        c.buy(280 + i * 10, f"m{i}", 0.1)
    for i in range(6):
        c.buy(312 + i * 1.3, f"n{i}", 0.35)
    return c


def older_run(sol: float = 0.3) -> Curve:
    c = Curve()
    for i in range(30):
        c.buy(1 + i * 20, f"e{i}", sol)
    return c


def shallow_retest() -> Curve:
    c = older_run()
    for i in range(5):
        c.sell(610 + i * 4, f"e{i}", 1.0)
    for i in range(6):
        c.buy(650 + i * 2, f"r{i}", 0.15)
    return c


def deep_drop_then_turn() -> Curve:
    c = older_run(0.4)
    for i in range(18):
        c.sell(600 + i * 3, f"e{i}", 1.0)
    for i in range(3):
        c.sell(660 + i * 4, f"e{20 + i}", 0.3)
    for i in range(6):
        c.buy(672 + i * 1.3, f"r{i}", 0.25)
    return c


# --- distinct signals -----------------------------------------------------------------------------

def test_demand_confirmation_fires_on_breadth_where_acceleration_waits():
    res = evaluate(broadening_without_acceleration(), 40)
    assert dec(res, ei.EARLY_DEMAND_CONFIRMATION) == ei.CANDIDATE
    assert dec(res, ei.EARLY_ACCELERATION) == ei.WAIT
    assert "decelerating" in res["strategies"][ei.EARLY_ACCELERATION]["reasons"][0]
    assert res["route"]["selected"] == ei.EARLY_DEMAND_CONFIRMATION


def test_breakout_needs_a_prior_range_and_is_distinct_from_the_other_fresh_strategies():
    res = evaluate(range_breakout(), 320)
    assert dec(res, ei.SELECTIVE_EARLY_BREAKOUT) == ei.CANDIDATE
    assert dec(res, ei.EARLY_ACCELERATION) == ei.NO_TRADE and dec(res, ei.EARLY_DEMAND_CONFIRMATION) == ei.NO_TRADE
    # a straight run-up has no range: no breakout
    straight = evaluate(broadening_without_acceleration(), 40)
    assert dec(straight, ei.SELECTIVE_EARLY_BREAKOUT) == ei.WAIT
    assert "no range yet" in straight["strategies"][ei.SELECTIVE_EARLY_BREAKOUT]["reasons"][0]


def test_retest_and_recovery_are_separate_signals():
    rt = evaluate(shallow_retest(), 662)
    assert rt["category"] == ei.MOMENTUM
    assert dec(rt, ei.BREAKOUT_RETEST) == ei.CANDIDATE
    assert dec(rt, ei.MOMENTUM_RECOVERY) != ei.CANDIDATE and dec(rt, ei.MOMENTUM_CONTINUATION) != ei.CANDIDATE
    rc = evaluate(deep_drop_then_turn(), 680)
    assert dec(rc, ei.MOMENTUM_RECOVERY) == ei.CANDIDATE
    assert dec(rc, ei.BREAKOUT_RETEST) == ei.NO_TRADE  # 33% below the high: support lost, not a retest
    assert rc["route"]["selected"] == ei.MOMENTUM_RECOVERY


def test_recovery_never_buys_a_token_still_falling_with_sellers_in_control():
    c = older_run(0.4)
    for i in range(18):
        c.sell(600 + i * 3, f"e{i}", 1.0)
    for i in range(4):
        c.sell(660 + i * 2, f"e{20 + i}", 1.0)
    c.buy(668, "late", 0.05)
    res = evaluate(c, 670)
    assert dec(res, ei.MOMENTUM_RECOVERY) == ei.NO_TRADE
    assert any("falling" in r for r in res["strategies"][ei.MOMENTUM_RECOVERY]["reasons"])


# --- routing ----------------------------------------------------------------------------------------

def test_router_selects_one_strategy_from_the_token_category_only():
    res = evaluate(range_breakout(), 320)
    # MOMENTUM_CONTINUATION also qualifies, but a 320 s token is FRESH: only FRESH strategies are routed
    assert dec(res, ei.MOMENTUM_CONTINUATION) == ei.CANDIDATE
    r = res["route"]
    assert r["category"] == ei.FRESH and r["selected"] == ei.SELECTIVE_EARLY_BREAKOUT and r["decision"] == ei.CANDIDATE
    assert set(r["rejected"]) == {ei.EARLY_ACCELERATION, ei.EARLY_DEMAND_CONFIRMATION}
    assert all(reason for _, reason in r["rejected"].values())


def test_highest_score_wins_when_several_qualify():
    d = {ei.EARLY_ACCELERATION: ei.StrategyDecision(ei.EARLY_ACCELERATION, ei.CANDIDATE, 0.6, None, None),
         ei.EARLY_DEMAND_CONFIRMATION: ei.StrategyDecision(ei.EARLY_DEMAND_CONFIRMATION, ei.CANDIDATE, 0.9, None, None),
         ei.SELECTIVE_EARLY_BREAKOUT: ei.StrategyDecision(ei.SELECTIVE_EARLY_BREAKOUT, ei.WAIT, 0.99, None, None, ["x"])}
    r = ei.route(d, ei.FRESH)
    assert r["selected"] == ei.EARLY_DEMAND_CONFIRMATION
    assert r["rejected"][ei.EARLY_ACCELERATION][0] == ei.CANDIDATE and r["rejected"][ei.SELECTIVE_EARLY_BREAKOUT] == (ei.WAIT, "x")


def test_no_qualifying_strategy_is_no_trade_and_unknown_age_waits():
    res = evaluate(older_run(), 605)  # a steady old run: no retest, no drop, no continuation structure
    assert res["route"]["decision"] == ei.NO_TRADE and res["route"]["selected"] is None
    assert set(res["route"]["rejected"]) == set(ei.CATEGORY_STRATEGIES[ei.MOMENTUM])
    none = ei.route({ei.BREAKOUT_RETEST: ei.StrategyDecision(ei.BREAKOUT_RETEST, ei.WAIT, 0.5, None, None, ["no retest"])},
                    ei.MOMENTUM)
    assert none["decision"] == ei.NO_TRADE and none["selected"] is None
    unknown = ei.route({}, None)
    assert unknown["decision"] == ei.WAIT and "age unknown" in unknown["reason"]
    assert ei.route({}, ei.MIGRATION)["decision"] == ei.NO_TRADE  # migrated tokens: measured by the pool variants only


def test_smart_wallets_confirm_only_the_routed_candidate_and_never_select():
    proven = {"status": "MEASURED", "proven_entries": [{"wallet": "w1aaaaaa", "seconds_ago": 5}], "proven_exits": [],
              "coordinated": False}
    res = evaluate(broadening_without_acceleration(), 40, proven)
    assert res["strategies"][ei.SMART_WALLET_CONFIRMATION]["decision"] == ei.CANDIDATE
    assert res["route"]["selected"] == ei.EARLY_DEMAND_CONFIRMATION  # unchanged by the wallets
    alone = evaluate(older_run(), 605, proven)  # proven wallets, but nothing routed
    assert alone["route"]["decision"] == ei.NO_TRADE
    sw = alone["strategies"][ei.SMART_WALLET_CONFIRMATION]
    assert sw["decision"] == ei.WAIT and "never a trigger alone" in sw["reasons"][0]


# --- registry --------------------------------------------------------------------------------------

def test_registry_covers_every_strategy_once_with_a_version():
    names = [s.recorded_as for s in reg.SPECS]
    assert len(names) == len(set(names)) and len({s.code for s in reg.SPECS}) == len(reg.SPECS)
    assert set(ei.STRATEGIES) <= set(names)
    assert {reg.BY_CODE[c].recorded_as for c in ("M1", "M2", "M3")} <= set(ei.MIGRATED_VARIANTS)
    for cat, members in ei.CATEGORY_STRATEGIES.items():
        assert all(reg.BY_NAME[m].category == cat for m in members)
    assert reg.BY_NAME[ei.SMART_WALLET_CONFIRMATION].trades_alone is False
    assert reg.version_of(ei.BREAKOUT_RETEST) == f"P2.v1/{ei.FEATURE_VERSION}"


# --- demotion ---------------------------------------------------------------------------------------

def test_one_or_two_losses_never_demote():
    assert reg.deterioration([-40.0, -35.0])["deteriorated"] is False
    assert reg.deterioration([-5.0] * 29)["deteriorated"] is False  # below the minimum sample


def test_a_reliably_losing_paper_strategy_is_demoted_and_a_noisy_one_is_not():
    losing = [-4.0, -6.0, -3.0, -5.0, 2.0] * 8
    d = reg.deterioration(losing)
    assert d["deteriorated"] is True and d["upper_95_pct"] < 0
    noisy = [-30.0, 25.0, -20.0, 22.0, -1.0] * 8  # mean slightly negative, wide spread: not reliable
    assert reg.deterioration(noisy)["deteriorated"] is False
    modes = {ei.EARLY_ACCELERATION: "PAPER", ei.BREAKOUT_RETEST: "SHADOW", ei.MOMENTUM_RECOVERY: "PAPER"}
    out = reg.demotions({ei.EARLY_ACCELERATION: losing, ei.BREAKOUT_RETEST: losing, ei.MOMENTUM_RECOVERY: noisy}, modes)
    assert set(out) == {ei.EARLY_ACCELERATION}  # SHADOW strategies are never touched; nothing is ever promoted


def test_only_the_newest_window_counts():
    old_losses = [-10.0] * 100
    recent = [3.0, 1.0, -1.0, 4.0] * 15  # the newest 60
    assert reg.deterioration(old_losses + recent)["deteriorated"] is False


def test_strategy_settings_are_range_checked():
    assert ei.validate_config({}) == []
    assert ei.validate_config({"rt_min_pullback_pct": 30.0})  # above rt_max_pullback_pct 20
    assert ei.validate_config({"rc_min_drawdown_pct": 80.0})
    assert ei.validate_config({"dc_min_age_seconds": 400})
    assert ei.validate_config({"fresh_max_age_seconds": 10})
    assert ei.validate_config({"dc_max_top_buyer_share": 1.5})
