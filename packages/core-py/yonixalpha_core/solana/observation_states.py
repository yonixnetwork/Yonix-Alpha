"""Solana observations in the master §15 state names (master §14-17).

Solana keeps its own observation funnel (solana.observation: OBSERVING,
CONTINUE_MONITORING, PROMOTE, REJECT, NO_TRADE, MIGRATION_DETECTED) and, after
a promotion, the candidate's lifecycle (state_machine.CandidateState). This
maps both onto the same names the BSC / Robinhood observation uses, with the
§17 fields, so all three chains read alike. A view only: no Solana decision
or state changes.

  funnel OBSERVING            -> OBSERVING
  funnel CONTINUE_MONITORING  -> ANALYZING (signal not met yet, still watched)
  funnel NO_TRADE             -> EXPIRED, EXPIRED_NO_ENTRY
  funnel REJECT               -> REJECTED (deterioration seen while observed)
  funnel MIGRATION_DETECTED   -> EXPIRED, MIGRATED (the migration engine
                                 observes it again as MIGRATED)
  funnel PROMOTE              -> QUALIFIED, then from the candidate:
      analyzing / waiting_*   -> WAITING_FOR_ENTRY (the gate holds it)
      entry_pending           -> ENTRY_PENDING
      entered and later       -> ENTERED
      rejected                -> REJECTED (SAFETY_FAILURE: the gate refused it)
      migrated                -> EXPIRED, MIGRATED
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

WAITING = {"analyzing", "waiting_for_liquidity", "waiting_for_approval", "qualified", "observing", "discovered"}
ENTERED_STATES = {"entered", "managing", "exit_signal", "exiting", "closed"}


def master_state(outcome: str | None, candidate_state: str | None = None) -> tuple[str, str | None]:
    """(§15 state, §17 expiry / rejection reason)."""
    if outcome in (None, "OBSERVING"):
        return "OBSERVING", None
    if outcome == "CONTINUE_MONITORING":
        return "ANALYZING", None
    if outcome == "NO_TRADE":
        return "EXPIRED", "EXPIRED_NO_ENTRY"
    if outcome == "REJECT":
        return "REJECTED", "DETERIORATION_WHILE_OBSERVED"
    if outcome == "MIGRATION_DETECTED":
        return "EXPIRED", "MIGRATED"
    if outcome == "PROMOTE":
        c = (candidate_state or "").lower()
        if not c:
            return "QUALIFIED", None
        if c == "entry_pending":
            return "ENTRY_PENDING", None
        if c in ENTERED_STATES:
            return "ENTERED", None
        if c == "rejected":
            return "REJECTED", "SAFETY_FAILURE"
        if c == "migrated":
            return "EXPIRED", "MIGRATED"
        if c in WAITING:
            return "WAITING_FOR_ENTRY", None
        return "QUALIFIED", None
    return "OBSERVING", None


def fields(report: dict[str, Any] | None, outcome: str | None, decided_at: datetime | None,
           candidate_state: str | None = None) -> dict[str, Any]:
    """The §17 fields of one Solana observation."""
    r = report or {}
    started = r.get("created_at")
    deadline = None
    if started and r.get("window_seconds"):
        deadline = (datetime.fromisoformat(started) + timedelta(seconds=int(r["window_seconds"]))).isoformat()
    state, reason = master_state(outcome, candidate_state)
    return {"state": state, "observation_started_at": started, "observation_deadline": deadline,
            "observation_reason": "launch observed (pump.fun stream)", "expiry_reason": reason,
            "decided_at": decided_at.isoformat() if decided_at else None, "funnel_outcome": outcome,
            "candidate_state": candidate_state}
