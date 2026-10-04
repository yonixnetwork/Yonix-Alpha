"""ML contribution governance (master §38-42): pure rules."""

from datetime import datetime, timedelta, timezone

from yonixalpha_core.ml import governance as g

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
NAME = "solana_candidate_momentum"
PASS = {"status": "PASS"}


def _check(current, stage, pct, *, champion=True, validation=PASS, now=NOW):
    return g.check_change(NAME, current, stage, pct, champion=champion, validation=validation, now=now)


def test_contribution_defaults_to_zero_and_counts_only_at_paper_contributor():
    assert g.Contribution().weight() == 0.0
    assert g.Contribution("SHADOW", 20).weight() == 0.0  # a stored percent outside PAPER means nothing
    assert g.Contribution("PAPER_CONTRIBUTOR", 10).weight() == 0.10
    assert g.Contribution("LIVE_CONTRIBUTOR", 25).weight() == 0.0
    parsed = g.parse({"models": {NAME: {"stage": "BOGUS", "percent": "90"}, "x": "junk"}})
    assert parsed[NAME].stage == "SHADOW" and parsed[NAME].percent == g.MAX_PCT and "x" not in parsed
    assert g.parse(None) == {}


def test_raising_needs_a_champion_a_pass_one_step_and_a_week():
    cur = g.Contribution()
    assert _check(cur, "PAPER_CONTRIBUTOR", 5) == []
    errs = _check(cur, "PAPER_CONTRIBUTOR", 5, champion=False, validation=None)
    assert any("champion" in e for e in errs) and any("none yet" in e for e in errs)
    assert any("(FAIL)" in e for e in _check(cur, "PAPER_CONTRIBUTOR", 5, validation={"status": "FAIL"}))
    assert any("(INSUFFICIENT_DATA)" in e for e in _check(cur, "PAPER_CONTRIBUTOR", 5,
                                                         validation={"status": "INSUFFICIENT_DATA"}))
    assert any("5 % at a time" in e for e in _check(cur, "PAPER_CONTRIBUTOR", 10))
    assert any("multiple of 5" in e for e in _check(cur, "PAPER_CONTRIBUTOR", 3))
    assert any("multiple of 5" in e for e in _check(cur, "PAPER_CONTRIBUTOR", 30))

    raised = g.apply_change(cur, "PAPER_CONTRIBUTOR", 5, "admin", NOW)
    assert raised.raised_at == NOW.isoformat() and raised.weight() == 0.05
    # however well the next days go, the next step waits a week
    assert any("wait 7 days" in e for e in _check(raised, "PAPER_CONTRIBUTOR", 10, now=NOW + timedelta(days=6)))
    assert _check(raised, "PAPER_CONTRIBUTOR", 10, now=NOW + timedelta(days=7)) == []


def test_lowering_is_always_allowed_and_keeps_the_raise_clock():
    cur = g.Contribution("PAPER_CONTRIBUTOR", 15, raised_at=NOW.isoformat())
    assert _check(cur, "PAPER_CONTRIBUTOR", 10, champion=False, validation=None) == []
    assert _check(cur, "SHADOW", 0, champion=False, validation=None) == []
    lowered = g.apply_change(cur, "SHADOW", 0, "admin", NOW + timedelta(days=1))
    assert lowered.weight() == 0.0 and lowered.raised_at == NOW.isoformat()
    # back up from 0 inside the week: refused (no flip-flopping around the 7-day rule)
    assert any("wait 7 days" in e for e in _check(lowered, "PAPER_CONTRIBUTOR", 5, now=NOW + timedelta(days=2)))


def test_live_is_locked_and_models_without_a_consumer_stay_shadow():
    assert g.LIVE_LOCKED in _check(g.Contribution(), "LIVE_CONTRIBUTOR", 0)
    assert any("only at PAPER_CONTRIBUTOR" in e for e in _check(g.Contribution(), "SHADOW", 5))
    errs = g.check_change("shadow_evm_p_upside_50", g.Contribution(), "PAPER_CONTRIBUTOR", 5, champion=True,
                          validation=PASS, now=NOW)
    assert "no decision consumer" in errs[0]
    assert g.check_change("shadow_evm_p_upside_50", g.Contribution(), "OBSERVATION_ONLY", 0, champion=False,
                          validation=None, now=NOW) == []
    assert "safety-gate model" in g.check_change("gate_solana_fresh", g.Contribution(), "SHADOW", 0, champion=True,
                                                 validation=PASS, now=NOW)[0]
    assert "stage must be one of" in _check(g.Contribution(), "PRODUCTION", 0)[0]


def test_health_reads_drift_then_the_frozen_set_verdict():
    assert g.health(validation=None) == ("NOT_VALIDATED", "no frozen validation report yet")
    assert g.health(validation=PASS)[0] == "OK"
    assert g.health(validation={"status": "FAIL", "reason": "AUC 0.5 < 0.55"}) == ("DEGRADED", "AUC 0.5 < 0.55")
    assert g.health(validation={"status": "INSUFFICIENT_DATA"})[0] == "NOT_VALIDATED"
    assert g.health(validation=PASS, drift={"status": "MODEL_DRIFT_DETECTED"})[0] == "DRIFT"
