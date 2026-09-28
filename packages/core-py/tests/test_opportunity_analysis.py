"""Ledger v2 analysis on curve-exact trades: executable vs theoretical
returns, the non-hindsight counterfactual, exit classification, recovery
and labels."""

from datetime import timedelta

from yonixalpha_core import opportunity_analysis as oa

from tests.test_launch_features import T0, Curve

FEE = 125


def run(steps, curve=None):
    """steps: (seconds after T0, SOL, buy) on one curve."""
    c = curve or Curve()
    return [c.trade(f"w{i}", T0 + timedelta(seconds=s), sol, buy) for i, (s, sol, buy) in enumerate(steps)]


def base_of(trades, at=T0):
    return oa._p(oa._last(trades, at))


def test_executable_return_counts_fees_impact_and_fixed_costs():
    trades = run([(0, 1.0, True), (100, 0.01, True)])  # the price barely moves
    rt = oa.round_trip(trades, T0 + timedelta(seconds=2), T0 + timedelta(seconds=200), fee_bps=FEE, mayhem=False)
    assert abs(rt["theoretical_return_pct"]) < 0.5
    assert -4 < rt["executable_return_pct"] < -2.5  # two 1.25% fees + impact + fixed costs
    assert rt["entry_impact_pct"] > 0
    assert "fee rate unknown" in oa.round_trip(trades, T0, T0 + timedelta(seconds=200), fee_bps=None, mayhem=False)["unknown"]
    assert "curve math" in oa.round_trip(trades, T0, T0 + timedelta(seconds=200), fee_bps=FEE, mayhem=True)["unknown"]
    assert "migrated" in oa.round_trip(trades, T0, T0 + timedelta(seconds=200), fee_bps=FEE, mayhem=False,
                                       migrated_at=T0 + timedelta(seconds=50))["unknown"]


def test_counterfactual_missed_win_needs_a_profit_after_costs():
    trades = run([(0, 1.0, True)] + [(10 + i * 5, 1.0, True) for i in range(12)])  # steady buying: +30% reached
    cf = oa.counterfactual(trades, T0, base_of(trades), T0 + timedelta(seconds=3600), fee_bps=FEE, mayhem=False,
                           migrated_at=None, reasons=["LOW_LIQUIDITY"])
    assert cf["rule_exit"] == "TP" and cf["classification"] == "MISSED_WIN" and cf["rejecting_rule"] == "LOW_LIQUIDITY"
    assert cf["rule_exit_executable"]["executable_return_pct"] >= oa.MIN_EXECUTABLE_WIN_PCT
    heavy = oa.counterfactual(trades, T0, base_of(trades), T0 + timedelta(seconds=3600), fee_bps=1000, mayhem=False,
                              migrated_at=None, reasons=[])
    assert heavy["classification"] == "UNEXECUTABLE"  # +30% on the chart, not after 10% fees on each leg


def test_counterfactual_is_decided_by_what_the_stream_hit_first():
    c = Curve()
    trades = run([(0, 10.0, True), (10, 6.0, False)], c)  # falls ~ -28% first ...
    trades += run([(60 + i * 5, 2.0, True) for i in range(20)], c)  # ... then pumps far above the decision price
    cf = oa.counterfactual(trades, T0, base_of(trades), T0 + timedelta(seconds=3600), fee_bps=FEE, mayhem=False,
                           migrated_at=None, reasons=[])
    assert cf["rule_exit"] == "STOP" and cf["peak_pct"] > 30
    assert cf["classification"] == "REJECTION_JUSTIFIED_DRAWDOWN" and cf["drawdown_before_peak_pct"] <= -25
    assert "better_later_entry" in cf
    flat = run([(0, 1.0, True), (30, 0.05, True)])
    assert oa.counterfactual(flat, T0, base_of(flat), T0 + timedelta(seconds=3600), fee_bps=FEE, mayhem=False,
                             migrated_at=None, reasons=[])["classification"] == "CORRECT_REJECTION"
    assert oa.counterfactual([], T0, None, T0, fee_bps=FEE, mayhem=False, migrated_at=None, reasons=[])["classification"] == "UNKNOWN"


def test_exit_classification():
    c = Curve()
    trades = run([(0, 2.0, True)], c) + run([(300 + i * 10, 1.5, True) for i in range(10)], c)  # pumps after the exit
    early = oa.exit_analysis(trades, T0 + timedelta(seconds=60), T0 + timedelta(seconds=4000), pnl_pct=5, mfe_pct=8,
                             exit_reason="take_profit")
    assert early["classification"] == "POSSIBLY_EARLY" and early["horizons"]["X+15m"]["change_pct"] > 30
    c = Curve()
    trades = run([(0, 4.0, True)], c) + run([(120 + i * 10, 0.6, False) for i in range(6)], c)
    risk = oa.exit_analysis(trades, T0 + timedelta(seconds=60), T0 + timedelta(seconds=4000), pnl_pct=-10, mfe_pct=2,
                            exit_reason="stop_loss")
    assert risk["classification"] == "RISK_CORRECT"
    late = oa.exit_analysis(trades, T0 + timedelta(seconds=60), T0 + timedelta(seconds=4000), pnl_pct=-5, mfe_pct=60,
                            exit_reason="trailing")
    assert late["classification"] == "POSSIBLY_LATE"
    assert oa.exit_analysis(trades, None, T0, pnl_pct=None, mfe_pct=None, exit_reason=None)["classification"] == "UNKNOWN"


def test_recovery_and_labels_come_only_from_after_the_decision():
    c = Curve()
    trades = run([(0, 10.0, True), (20, 6.0, False)], c) + run([(40 + i * 5, 1.0, True) for i in range(10)], c)
    base = base_of(trades)
    end = T0 + timedelta(seconds=3600)
    rec = oa.recovery(trades, T0, base, end)
    assert rec["mae_pct"] < -20 and rec["recovered"] is True and rec["time_to_recovery_seconds"] > 0
    assert rec["flow_after_trough_60s"]["buys"] >= 1
    lab = oa.labels(trades, T0, base, end, migrated_at=None, executable_primary=None, rec=rec)
    assert lab["recovery"] is True and lab["migrate_60m"] is False and lab["available_at"] == end.isoformat()
    assert lab["manipulation"] is None  # no ground truth: never invented
    # Trades before the decision never enter the labels.
    earlier = run([(-100, 20.0, True)], Curve())
    assert oa.labels(earlier + trades, T0, base, end, migrated_at=None, executable_primary=None, rec=rec)["peak_pct"] == lab["peak_pct"]


def test_path_point_reports_flow_between_horizons():
    trades = run([(0, 1.0, True), (3, 0.5, False), (8, 0.7, True)])
    p = oa.path_point(trades, T0, base_of(trades), T0 + timedelta(seconds=5), T0 + timedelta(seconds=10), 10**15)
    assert p["buys"] == 1 and p["sells"] == 0 and p["change_pct"] > 0 and p["market_cap_sol"] > 0
    assert oa.path_point([], T0, None, T0, T0 + timedelta(seconds=5), None)["unknown"]


def test_recovery_is_undefined_without_a_drawdown():
    trades = run([(0, 1.0, True)] + [(10 + i * 5, 1.0, True) for i in range(5)])
    rec = oa.recovery(trades, T0, base_of(trades), T0 + timedelta(seconds=3600))
    assert rec["recovered"] is None and rec["time_to_recovery_seconds"] is None
