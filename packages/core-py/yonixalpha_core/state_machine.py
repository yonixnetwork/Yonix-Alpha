from datetime import datetime, timezone
from enum import StrEnum


class CandidateState(StrEnum):
    """Per spec section 10. A candidate is any token/opportunity an engine
    is tracking, independent of which engine (discovery/migration/momentum)
    produced it. REJECTED is reachable any time before capital is
    committed (ENTERED); once a position is actually open, the only honest
    terminal state is CLOSED, reached via the exit chain — a real position
    doesn't get "rejected" out of existence.
    """

    DISCOVERED = "discovered"
    OBSERVING = "observing"
    # Gate-era lifecycle (spec §9). ANALYZING replaces OBSERVING for
    # candidates evaluated by the safety gate; the two WAITING states make
    # "why hasn't it traded yet" visible without reading the findings.
    ANALYZING = "analyzing"
    WAITING_FOR_LIQUIDITY = "waiting_for_liquidity"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    QUALIFIED = "qualified"
    ENTRY_PENDING = "entry_pending"
    ENTERED = "entered"
    MANAGING = "managing"
    EXIT_SIGNAL = "exit_signal"
    EXITING = "exiting"
    CLOSED = "closed"
    REJECTED = "rejected"
    # A fresh/momentum (bonding-curve) candidate whose token migrated before
    # entry: handed to the migration engine, which re-evaluates it with pool
    # rules (MIGRATION_DETECTED -> MIGRATED_ANALYSIS). Not a rejection.
    MIGRATED = "migrated"


TERMINAL_STATES = {CandidateState.CLOSED, CandidateState.REJECTED, CandidateState.MIGRATED}

VALID_TRANSITIONS: dict[CandidateState, set[CandidateState]] = {
    CandidateState.DISCOVERED: {CandidateState.OBSERVING, CandidateState.ANALYZING, CandidateState.REJECTED,
                                CandidateState.MIGRATED},
    CandidateState.ANALYZING: {
        CandidateState.WAITING_FOR_LIQUIDITY, CandidateState.WAITING_FOR_APPROVAL, CandidateState.QUALIFIED, CandidateState.REJECTED,
        CandidateState.MIGRATED,
    },
    CandidateState.WAITING_FOR_LIQUIDITY: {
        CandidateState.ANALYZING, CandidateState.WAITING_FOR_APPROVAL, CandidateState.QUALIFIED, CandidateState.REJECTED,
        CandidateState.MIGRATED,
    },
    CandidateState.WAITING_FOR_APPROVAL: {
        CandidateState.ANALYZING, CandidateState.WAITING_FOR_LIQUIDITY, CandidateState.QUALIFIED, CandidateState.REJECTED,
        CandidateState.MIGRATED,
    },
    CandidateState.OBSERVING: {CandidateState.QUALIFIED, CandidateState.REJECTED, CandidateState.MIGRATED},
    CandidateState.QUALIFIED: {CandidateState.ENTRY_PENDING, CandidateState.REJECTED},
    # ANALYZING: a buy that failed without filling goes back for a full,
    # fresh re-evaluation (bounded; see live_trading.MAX_ENTRY_ATTEMPTS).
    CandidateState.ENTRY_PENDING: {CandidateState.ENTERED, CandidateState.REJECTED, CandidateState.ANALYZING},
    CandidateState.ENTERED: {CandidateState.MANAGING},
    CandidateState.MANAGING: {CandidateState.EXIT_SIGNAL},
    CandidateState.EXIT_SIGNAL: {CandidateState.EXITING},
    CandidateState.EXITING: {CandidateState.CLOSED},
    CandidateState.CLOSED: set(),
    CandidateState.REJECTED: set(),
    CandidateState.MIGRATED: set(),
}


class InvalidStateTransitionError(Exception):
    def __init__(self, from_state: CandidateState, to_state: CandidateState):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Cannot transition from {from_state.value} to {to_state.value}")


def apply_transition(candidate, new_state: CandidateState, reason: str | None = None) -> None:
    """Mutates `candidate` (a TradingCandidate ORM instance) in place: sets
    `.state`, appends to `.state_history` (so the full path is recoverable
    from Postgres after a restart, per spec section 10 — never just the
    current state), and stamps `.state_updated_at`. Caller still owns
    session.commit(); this function does no I/O.

    Raises InvalidStateTransitionError rather than silently allowing an
    illegal jump (e.g. DISCOVERED -> ENTERED) — a bug in caller code should
    fail loudly here, not corrupt the candidate's history.
    """
    current = CandidateState(candidate.state)
    if new_state not in VALID_TRANSITIONS.get(current, set()):
        raise InvalidStateTransitionError(current, new_state)

    now = datetime.now(timezone.utc)
    history = list(candidate.state_history or [])
    history.append({"state": new_state.value, "at": now.isoformat(), "reason": reason})

    candidate.state = new_state.value
    candidate.state_history = history
    candidate.state_updated_at = now
