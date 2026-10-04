"""Solana observations in the master §15 / §17 names."""

from datetime import datetime, timezone

from yonixalpha_core.solana import observation_states as os_

T = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def test_funnel_and_candidate_states_map_onto_the_master_names():
    m = os_.master_state
    assert m("OBSERVING") == ("OBSERVING", None) and m(None) == ("OBSERVING", None)
    assert m("CONTINUE_MONITORING") == ("ANALYZING", None)
    assert m("NO_TRADE") == ("EXPIRED", "EXPIRED_NO_ENTRY")
    assert m("REJECT") == ("REJECTED", "DETERIORATION_WHILE_OBSERVED")
    assert m("MIGRATION_DETECTED") == ("EXPIRED", "MIGRATED")
    assert m("PROMOTE") == ("QUALIFIED", None)
    assert m("PROMOTE", "analyzing") == ("WAITING_FOR_ENTRY", None)
    assert m("PROMOTE", "waiting_for_liquidity") == ("WAITING_FOR_ENTRY", None)
    assert m("PROMOTE", "entry_pending") == ("ENTRY_PENDING", None)
    assert m("PROMOTE", "managing") == ("ENTERED", None) and m("PROMOTE", "closed") == ("ENTERED", None)
    assert m("PROMOTE", "rejected") == ("REJECTED", "SAFETY_FAILURE")
    assert m("PROMOTE", "migrated") == ("EXPIRED", "MIGRATED")


def test_the_section_17_fields():
    f = os_.fields({"created_at": T.isoformat(), "window_seconds": 120}, "NO_TRADE", T)
    assert f["observation_started_at"] == T.isoformat() and f["observation_deadline"] == "2026-10-05T12:02:00+00:00"
    assert f["observation_reason"].startswith("launch observed") and f["expiry_reason"] == "EXPIRED_NO_ENTRY"
    assert os_.fields(None, "OBSERVING", None)["observation_deadline"] is None  # unknown, never invented
