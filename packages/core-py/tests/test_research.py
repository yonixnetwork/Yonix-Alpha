"""Research pipeline rules (master §67)."""

from yonixalpha_core import research as r

NOTE = "paper-tested for 7 days on BSC, 40 entries"


def test_forward_one_stage_at_a_time_with_the_evidence():
    assert r.check_move("RESEARCH", "REVIEW", "read the official contracts and docs") == []
    assert "one stage at a time" in r.check_move("RESEARCH", "PAPER", NOTE)[0]
    assert "needs" in r.check_move("REVIEW", "PAPER", "ok")[0]
    assert r.check_move("VALIDATION", "CONTROLLED_RELEASE", NOTE) == []
    assert "already" in r.check_move("PAPER", "PAPER", NOTE)[0]
    assert "stage must be one of" in r.check_move("PAPER", "LIVE", NOTE)[0]


def test_reject_back_and_reopen():
    assert "reason" in r.check_move("PAPER", "REJECTED", "")[0]
    assert r.check_move("PAPER", "REJECTED", "sell simulation reverted on 3 of 10 tokens") == []
    assert "reopened to RESEARCH" in r.check_move("REJECTED", "PAPER", NOTE)[0]
    assert r.check_move("REJECTED", "RESEARCH", None) == []
    assert r.check_move("CONTROLLED_RELEASE", "PAPER", "rolled back: entries lost money") == []
    assert "reason" in r.check_move("CONTROLLED_RELEASE", "PAPER", "")[0]
