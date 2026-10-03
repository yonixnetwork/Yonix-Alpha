"""Decision states and the safety hierarchy (master §76-77) for entries
that are decided by a list of blockers (BSC / Robinhood automatic, manual
and copy entries; the Solana gate has its own FinalDecision with the same
states and is not changed here).

§76 order (a layer above decides before any layer below):

    DATA SAFETY > TOKEN SAFETY > LIQUIDITY SAFETY > EXECUTION SAFETY > RISK
    > STRATEGY > ML > EXECUTION

Every blocker code belongs to one layer and implies one §77 state:

    EXECUTE          nothing blocks
    REDUCE_SIZE      nothing blocks, the size was reduced (coordination
                     REDUCE_SIZE, or the risk plan sized it down)
    WAIT             not yet: the trade signal or the liquidity may still
                     come while the token is observed
    MANUAL_APPROVAL  an operator must approve (launch coordination)
    REJECT           the token itself fails (safety verdict, strategy)
    NO_TRADE         cannot trade now: data or provider unavailable,
                     limits, controls, execution not quotable

The decision is the state of the highest blocking layer (within a layer the
most restrictive state). A lower layer can never turn a higher layer's
block into a trade: ML has no blocker codes and contributes 0 %, and a copy
target's buy is only the trigger of the evaluation, never evidence that
passes a check ("A target wallet buying a token is NOT permission").
Unknown codes are treated as RISK / NO_TRADE: never as a pass.
"""

from __future__ import annotations

from typing import Any

EXECUTE, REDUCE_SIZE, WAIT, MANUAL_APPROVAL, REJECT, NO_TRADE = (
    "EXECUTE", "REDUCE_SIZE", "WAIT", "MANUAL_APPROVAL", "REJECT", "NO_TRADE")
STATES = (EXECUTE, REDUCE_SIZE, WAIT, MANUAL_APPROVAL, REJECT, NO_TRADE)
LAYERS = ("DATA_SAFETY", "TOKEN_SAFETY", "LIQUIDITY_SAFETY", "EXECUTION_SAFETY", "RISK", "STRATEGY", "ML", "EXECUTION")
# within one layer: the more restrictive state wins
_SEVERITY = {REJECT: 4, NO_TRADE: 3, MANUAL_APPROVAL: 2, WAIT: 1}

CODES: dict[str, tuple[str, str]] = {
    # data safety: the data a check needs is missing, stale or unverified -> NO_TRADE (§70)
    "SAFETY_STALE": ("DATA_SAFETY", NO_TRADE),
    "COORDINATION_NOT_CHECKED": ("DATA_SAFETY", NO_TRADE),
    "LAUNCHPAD_NOT_VERIFIED": ("DATA_SAFETY", NO_TRADE),
    "GAS_PRICE_UNAVAILABLE": ("DATA_SAFETY", NO_TRADE),
    # token safety
    "SAFETY_NOT_PASSED": ("TOKEN_SAFETY", REJECT),
    "LAUNCH_COORDINATION": ("TOKEN_SAFETY", NO_TRADE),
    "COORDINATION_MANUAL_APPROVAL": ("TOKEN_SAFETY", MANUAL_APPROVAL),
    # liquidity safety
    "LIQUIDITY_TOO_LOW": ("LIQUIDITY_SAFETY", WAIT),
    # execution safety
    "NO_EXECUTABLE_ROUND_TRIP": ("EXECUTION_SAFETY", NO_TRADE),
    "NO_EXECUTABLE_QUOTE": ("EXECUTION_SAFETY", NO_TRADE),
    "INSUFFICIENT_GAS": ("EXECUTION_SAFETY", NO_TRADE),
    "ROUND_TRIP_EXCEEDS_STOP_BUDGET": ("EXECUTION_SAFETY", REJECT),
    "CHASE_GUARD": ("EXECUTION_SAFETY", NO_TRADE),
    # risk and operator controls
    "KILL_SWITCH": ("RISK", NO_TRADE),
    "TRADING_CONTROL_OFF": ("RISK", NO_TRADE),
    "PAPER_ENTRIES_OFF": ("RISK", NO_TRADE),
    "CATEGORY_DISABLED": ("RISK", NO_TRADE),
    "MAX_OPEN_POSITIONS": ("RISK", NO_TRADE),
    "MAX_EXPOSURE": ("RISK", NO_TRADE),
    "DAILY_LOSS_LIMIT": ("RISK", NO_TRADE),
    "ALREADY_TRADED": ("RISK", NO_TRADE),
    "POSITION_ALREADY_OPEN": ("RISK", NO_TRADE),
    "DUPLICATE_POSITION": ("RISK", NO_TRADE),
    "RISK_SETTINGS_INVALID": ("RISK", NO_TRADE),
    "PLAN_INCOMPLETE": ("RISK", NO_TRADE),
    # strategy
    "TOO_FEW_BUYS": ("STRATEGY", WAIT),
    "TOO_FEW_BUYERS": ("STRATEGY", WAIT),
    "SELLING_PRESSURE": ("STRATEGY", WAIT),
    "CATEGORY_NOT_TRADED": ("STRATEGY", REJECT),
}
DEFAULT = ("RISK", NO_TRADE)

ML_EVIDENCE = {"contribution_pct": 0, "status": "SHADOW",
               "note": "ML is not consulted for entries: shadow models are review data and cannot override safety (§76)"}


def classify(code: str) -> tuple[str, str]:
    return CODES.get(code, DEFAULT)


def resolve(blockers: list[dict[str, Any]], size_reduced: bool = False) -> dict[str, Any]:
    """{decision, layer, reason, blockers (each with its layer and state)}."""
    tagged = [{**b, "layer": classify(b["code"])[0], "state": classify(b["code"])[1]} for b in blockers]
    if not tagged:
        return {"decision": REDUCE_SIZE if size_reduced else EXECUTE, "layer": "EXECUTION",
                "reason": "size reduced; every check passed" if size_reduced else "every check passed",
                "blockers": []}
    top = min(LAYERS.index(b["layer"]) for b in tagged)
    first = [b for b in tagged if LAYERS.index(b["layer"]) == top]
    decider = max(first, key=lambda b: _SEVERITY[b["state"]])
    tagged.sort(key=lambda b: (LAYERS.index(b["layer"]), -_SEVERITY[b["state"]]))
    return {"decision": decider["state"], "layer": decider["layer"],
            "reason": f"{decider['code']}: {decider.get('message', '')}".rstrip(": "), "blockers": tagged}


def provider_status(rpc) -> dict[str, Any]:
    """Endpoint states of the chain's RPC (URLs redacted by health())."""
    try:
        h = rpc.health()
    except Exception as exc:  # noqa: BLE001 - evidence only; never blocks or passes anything
        return {"available": False, "error": type(exc).__name__}
    eps = h.get("endpoints") or []
    return {"available": any(e.get("state") in ("OK", "UNCHECKED") for e in eps),
            "endpoints": [{"url": e.get("url"), "state": e.get("state"), "last_error": (e.get("last_error") or "")[:120]}
                          for e in eps]}
