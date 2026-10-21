"""Momentum pipeline verdicts: a broken scanner is never reported as "no
opportunity", and each A-H case is told apart from the recorded numbers."""

from yonixalpha_core.momentum_pipeline import classify

OK_STREAM = {"stream_event": "CONNECTED", "pumpportal_event": "CONNECTED"}
FUNNEL = {"passes": 100, "momentum_active": 50, "momentum_no_curve_address": 5, "momentum_curve_done": 2,
          "momentum_too_young": 20, "momentum_considered": 23, "momentum_prefilter_failed": 20, "momentum_budget_full": 0,
          "momentum_recently_promoted": 0, "momentum_promoted": 3, "momentum_watch_recorded": 0}


def run(**kw):
    base = dict(mode="PAPER", stream_states=OK_STREAM, funnel=FUNNEL, candidates={"evaluated": 3},
                decisions={"total": 3, "executable": 0}, blockers=[], positions={}, watch=0, refused=0, failed_orders=0)
    base.update(kw)
    return [f["code"] for f in classify(**base)], classify(**base)


def test_a_disconnected_stream_is_a_system_failure_not_no_opportunity():
    codes, out = run(stream_states={"stream_event": "DISCONNECTED", "pumpportal_event": "DATA GAP"})
    assert codes[0] == "A_NO_EVENTS" and out[0]["kind"] == "SYSTEM"
    codes, out = run(stream_states={"stream_event": "STALE", "pumpportal_event": "CONNECTED"})
    assert codes[0] == "A_NO_EVENTS" and out[0]["kind"] == "WARNING"


def test_b_funnel_not_running():
    assert "B_NOT_PROCESSED" in run(funnel={**FUNNEL, "passes": 0})[0]


def test_c_nothing_passes_the_prefilter():
    codes, out = run(funnel={**FUNNEL, "momentum_prefilter_failed": 23, "momentum_promoted": 0}, candidates={},
                     decisions={"total": 0, "executable": 0})
    assert codes == ["C_FILTERED"] and out[0]["kind"] == "NO_OPPORTUNITY"
    codes, _ = run(funnel={**FUNNEL, "momentum_considered": 0, "momentum_prefilter_failed": 0, "momentum_promoted": 0},
                   candidates={}, decisions={"total": 0, "executable": 0})
    assert codes == ["C_FILTERED"]


def test_d_budget_full_or_not_evaluated():
    codes, out = run(funnel={**FUNNEL, "momentum_promoted": 0, "momentum_budget_full": 3}, candidates={},
                     decisions={"total": 0, "executable": 0})
    assert codes == ["D_NOT_EVALUATED"] and out[0]["kind"] == "SYSTEM" and "slot budget" in out[0]["message"]
    codes, _ = run(decisions={"total": 0, "executable": 0})
    assert codes == ["D_NOT_EVALUATED"]


def test_e_and_h_signal_versus_mode_blockers():
    codes, out = run(blockers=[{"code": "SIGNAL_NOT_QUALIFIED", "category": "STRATEGY", "n": 3}])
    assert codes == ["H_NO_SIGNAL"] and out[0]["kind"] == "NO_OPPORTUNITY"
    codes, _ = run(blockers=[{"code": "STRATEGY_OFF", "category": "STRATEGY", "n": 3}])
    assert codes == ["E_BLOCKED"]
    codes, _ = run(blockers=[{"code": "DAILY_LOSS_LIMIT", "category": "ACCOUNT", "n": 2},
                             {"code": "SIGNAL_NOT_QUALIFIED", "category": "STRATEGY", "n": 1}])
    assert codes == ["E_BLOCKED"]
    codes, _ = run(blockers=[{"code": "TOP_HOLDER_SHARE", "category": "HOLDER", "n": 3}])
    assert codes == ["C_FILTERED"]


def test_f_executable_without_position_and_failed_orders():
    assert run(decisions={"total": 3, "executable": 2})[0] == ["F_EXECUTION_FAILED"]
    codes, _ = run(decisions={"total": 3, "executable": 1}, positions={"PAPER": 1}, refused=1)
    assert codes == ["F_EXECUTION_FAILED", "TRADING"]


def test_g_off_records_watch_and_trading_is_reported():
    codes, out = run(mode="OFF", candidates={}, decisions={"total": 0, "executable": 0}, watch=7)
    assert codes[0] == "G_OBSERVE_ONLY" and "7 WATCH" in out[0]["message"] and out[0]["kind"] == "BY_DESIGN"
    assert run(decisions={"total": 3, "executable": 1}, positions={"PAPER": 1})[0] == ["TRADING"]


def test_no_counters_and_nothing_recorded_is_no_evidence_not_no_opportunity():
    codes, out = run(funnel=None, candidates={}, decisions={"total": 0, "executable": 0})
    assert codes == ["NO_EVIDENCE"] and "not evidence that there was no opportunity" in out[0]["message"]
