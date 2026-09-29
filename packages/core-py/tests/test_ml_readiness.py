"""ML readiness lifecycle: every state, and ML contributes only in
PRODUCTION_CONTRIBUTOR (and then at most as a WAIT)."""

from yonixalpha_core.ml.readiness import classify


def c(**kw):
    base = dict(clean_labeled=0, challenger=None, champion=None, drift_flag=False, min_ml_confidence=None)
    return classify(**{**base, **kw})


def test_states_in_order():
    assert c(clean_labeled=12)["state"] == "INSUFFICIENT_DATA"
    assert c(clean_labeled=60)["state"] == "LEARNING"
    weak = {"version": 2, "metrics": {"promotable": False, "challenger": {"auc": 0.52, "auc_lower_bound": 0.44}}}
    r = c(clean_labeled=60, challenger=weak)
    assert r["state"] == "VALIDATING" and "0.520" in r["reason"]
    good = {"version": 3, "metrics": {"promotable": True}}
    assert c(clean_labeled=60, challenger=good)["state"] == "SHADOW"  # waits for the operator


def test_champion_needs_forward_validation_and_a_threshold_to_contribute():
    fresh = {"version": 1, "metrics": {"drift": {"status": "ok", "recent_accuracy": None}}}
    assert c(champion=fresh)["state"] == "SHADOW"
    ok = {"version": 1, "metrics": {"drift": {"status": "ok", "recent_accuracy": 0.64, "accuracy_drop": 0.02}}}
    paper = c(champion=ok)
    assert paper["state"] == "PAPER_VALIDATED" and paper["contributing"] is False
    prod = c(champion=ok, min_ml_confidence=0.6)
    assert prod["state"] == "PRODUCTION_CONTRIBUTOR" and prod["contributing"] is True
    assert "never approves" in prod["effect"]


def test_drift_or_degradation_stops_contribution():
    ok = {"version": 1, "metrics": {"drift": {"status": "ok", "recent_accuracy": 0.64, "accuracy_drop": 0.02}}}
    assert c(champion=ok, min_ml_confidence=0.6, drift_flag=True)["contributing"] is False
    worse = {"version": 1, "metrics": {"drift": {"status": "ok", "recent_accuracy": 0.4, "accuracy_drop": 0.25}}}
    r = c(champion=worse, min_ml_confidence=0.6)
    assert r["state"] == "SHADOW" and r["contributing"] is False
