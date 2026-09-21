from types import SimpleNamespace

import pytest

from yonixalpha_core.state_machine import (
    CandidateState,
    InvalidStateTransitionError,
    TERMINAL_STATES,
    apply_transition,
)


def _candidate(state: CandidateState) -> SimpleNamespace:
    return SimpleNamespace(state=state.value, state_history=[], state_updated_at=None)


def test_valid_transition_updates_state_and_history():
    candidate = _candidate(CandidateState.DISCOVERED)
    apply_transition(candidate, CandidateState.OBSERVING, reason="passed initial filters")

    assert candidate.state == CandidateState.OBSERVING.value
    assert candidate.state_updated_at is not None
    assert len(candidate.state_history) == 1
    assert candidate.state_history[0]["state"] == CandidateState.OBSERVING.value
    assert candidate.state_history[0]["reason"] == "passed initial filters"
    assert "at" in candidate.state_history[0]


def test_history_accumulates_across_multiple_transitions():
    candidate = _candidate(CandidateState.DISCOVERED)
    apply_transition(candidate, CandidateState.OBSERVING)
    apply_transition(candidate, CandidateState.QUALIFIED)
    apply_transition(candidate, CandidateState.REJECTED, reason="risk engine declined")

    assert [h["state"] for h in candidate.state_history] == [
        CandidateState.OBSERVING.value,
        CandidateState.QUALIFIED.value,
        CandidateState.REJECTED.value,
    ]
    assert candidate.state == CandidateState.REJECTED.value


def test_invalid_transition_raises_and_leaves_candidate_unchanged():
    candidate = _candidate(CandidateState.DISCOVERED)
    with pytest.raises(InvalidStateTransitionError):
        apply_transition(candidate, CandidateState.ENTERED)  # can't skip the chain

    assert candidate.state == CandidateState.DISCOVERED.value
    assert candidate.state_history == []


def test_full_happy_path_reaches_closed():
    candidate = _candidate(CandidateState.DISCOVERED)
    chain = [
        CandidateState.OBSERVING,
        CandidateState.QUALIFIED,
        CandidateState.ENTRY_PENDING,
        CandidateState.ENTERED,
        CandidateState.MANAGING,
        CandidateState.EXIT_SIGNAL,
        CandidateState.EXITING,
        CandidateState.CLOSED,
    ]
    for next_state in chain:
        apply_transition(candidate, next_state)

    assert candidate.state == CandidateState.CLOSED.value


def test_rejected_unreachable_after_entered():
    candidate = _candidate(CandidateState.ENTERED)
    with pytest.raises(InvalidStateTransitionError):
        apply_transition(candidate, CandidateState.REJECTED)


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_have_no_outgoing_transitions(terminal_state):
    candidate = _candidate(terminal_state)
    for target in CandidateState:
        if target == terminal_state:
            continue
        with pytest.raises(InvalidStateTransitionError):
            apply_transition(candidate, target)
