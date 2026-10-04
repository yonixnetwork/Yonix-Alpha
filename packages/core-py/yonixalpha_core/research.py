"""Research pipeline (master §67).

New launchpads, DEX routes, RPC capabilities, wallet intelligence, safety
methods, execution methods and GitHub implementations are evaluated
continuously (the update monitor, the launchpad activity monitor and the
research records), but a research result never changes a trading rule by
itself. Each item moves through

    RESEARCH -> REVIEW -> PAPER -> VALIDATION -> CONTROLLED_RELEASE

one stage at a time, by an operator, with the evidence each stage needs
(REQUIREMENTS), every move audited and kept in the item's history. It can be
REJECTED at any stage (with the reason) and reopened to RESEARCH, or moved
back to any earlier stage.

CONTROLLED_RELEASE records the operator's decision and its limits. It
changes no setting: the release itself is made with the existing controls
(launchpad status, strategy and copy switches, risk settings), each audited
on its own. Nothing in this module trades or enables anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

STAGES = ("RESEARCH", "REVIEW", "PAPER", "VALIDATION", "CONTROLLED_RELEASE")
REJECTED = "REJECTED"
KINDS = ("launchpad", "dex_route", "rpc_capability", "wallet_intelligence", "safety_method", "execution_method",
         "github_implementation")
MIN_NOTE = 10
REQUIREMENTS = {
    "REVIEW": "what was researched and from which source",
    "PAPER": "the review outcome: what is paper tested, and how it stays apart from live trading",
    "VALIDATION": "the paper evidence: what ran on paper, for how long, and the result",
    "CONTROLLED_RELEASE": "the validation result and the release limits: scope, size, and how to roll back",
}


def check_move(current: str, target: str, note: str | None) -> list[str]:
    """Reasons a move is refused (empty: allowed)."""
    note = (note or "").strip()
    if target == REJECTED:
        if current == REJECTED:
            return ["already REJECTED"]
        return [] if len(note) >= MIN_NOTE else [f"a rejection needs the reason (at least {MIN_NOTE} characters)"]
    if target not in STAGES:
        return [f"stage must be one of {', '.join(STAGES)} or {REJECTED}"]
    if current == REJECTED:
        return [] if target == "RESEARCH" else ["a rejected item can only be reopened to RESEARCH"]
    if current not in STAGES:
        return [f"unknown current stage {current}"]
    i, j = STAGES.index(current), STAGES.index(target)
    if j == i:
        return [f"already {current}"]
    if j < i:  # back to an earlier stage: allowed, with the reason
        return [] if len(note) >= MIN_NOTE else [f"moving back needs the reason (at least {MIN_NOTE} characters)"]
    if j > i + 1:
        return [f"one stage at a time: {current} -> {STAGES[i + 1]} first"]
    if len(note) < MIN_NOTE:
        return [f"{target} needs {REQUIREMENTS[target]} (at least {MIN_NOTE} characters)"]
    return []


def entry(stage: str, by: str, at: datetime, note: str | None) -> dict[str, Any]:
    return {"stage": stage, "by": by, "at": at.isoformat(), "note": (note or "").strip() or None}
