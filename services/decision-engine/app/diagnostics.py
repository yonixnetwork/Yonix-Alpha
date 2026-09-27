"""Structured diagnostics for a candidate whose evaluation raised.

A crash rolls the evaluation back, so without this nothing about it would be
stored: the candidate would be retried forever, never assessed, never timed
out, and hold a slot of the active-candidate budget. Every failure is
recorded instead (timeline event + system event, which also goes to
Telegram) with the exact place it happened and the simple input values of
that frame; after MAX_EVALUATION_FAILURES consecutive failures the candidate
is REJECTED with the error as its reason (explicit, never silent).

Only scalar locals are reported, and never any whose name looks like a
secret: frames can hold the application settings.
"""

import dataclasses
import decimal
import os
import re
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.state_machine import TERMINAL_STATES, CandidateState, apply_transition

MAX_EVALUATION_FAILURES = 5
FAILURE_COUNT_TTL = 6 * 3600
# Credential-like names only: "token" alone would hide every token_* market
# field, so only the credential kinds of token are matched.
_SECRETISH = re.compile(r"key|secret|password|passwd|private|credential|seed|mnemonic|auth|cookie|session"
                        r"|(access|refresh|bot|api|jwt|bearer|id)_?token", re.I)
_PROJECT = ("yonixalpha_core", f"{os.sep}app{os.sep}")


def error_type(exc: BaseException) -> str:
    """decimal.InvalidOperation carries the precise signal (DivisionUndefined
    is 0/0, DivisionByZero x/0) in its args; name it."""
    if isinstance(exc, decimal.InvalidOperation):
        signals = [getattr(a, "__name__", str(a)) for a in (exc.args[0] if exc.args and isinstance(exc.args[0], list) else [])]
        if "DivisionUndefined" in signals:
            return "decimal.DivisionUndefined (0 / 0)"
        return "decimal." + ("/".join(signals) or "InvalidOperation")
    if isinstance(exc, decimal.DivisionByZero):
        return "decimal.DivisionByZero (x / 0)"
    return type(exc).__name__


def _scalar(v: Any) -> Any:
    if isinstance(v, (bool, int, float, Decimal)) or v is None:
        return str(v) if isinstance(v, Decimal) else v
    if isinstance(v, str):
        return v if len(v) <= 80 else v[:77] + "..."
    return None


def _safe_locals(frame_locals: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, v in frame_locals.items():
        if name.startswith("__") or _SECRETISH.search(name):
            continue
        s = _scalar(v)
        if s is not None or v is None:
            out[name] = s
        elif dataclasses.is_dataclass(v) and not isinstance(v, type):
            fields = {f.name: _scalar(getattr(v, f.name)) for f in dataclasses.fields(v)
                      if not _SECRETISH.search(f.name)}
            out[name] = {k: x for k, x in fields.items() if x is not None} or type(v).__name__
    return out


def where(exc: BaseException) -> dict[str, Any]:
    """The innermost frame in this project's code (not a library's)."""
    tb = exc.__traceback__
    frames = []
    while tb is not None:
        frames.append(tb)
        tb = tb.tb_next
    ours = [f for f in frames if any(p in f.tb_frame.f_code.co_filename for p in _PROJECT)] or frames
    if not ours:
        return {}
    f = ours[-1]
    code = f.tb_frame.f_code
    summary = traceback.extract_tb(f, limit=1)[0]
    path = code.co_filename
    short = path[path.find("yonixalpha_core"):] if "yonixalpha_core" in path else path.rsplit(os.sep, 2)[-1]
    return {"file": short, "function": code.co_name, "line": f.tb_lineno, "code": (summary.line or "")[:200],
            "inputs": _safe_locals(dict(f.tb_frame.f_locals)),
            "stack": [f"{fr.tb_frame.f_code.co_filename.rsplit(os.sep, 1)[-1]}:{fr.tb_frame.f_code.co_name}:{fr.tb_lineno}"
                      for fr in ours[-4:]]}


async def record_failure(session_factory, redis: Redis | None, candidate_id, exc: BaseException,
                         now: datetime) -> dict[str, Any]:
    """Stores the failure and returns the detail (for the system event /
    Telegram). Rejects the candidate at MAX_EVALUATION_FAILURES."""
    attempt = 1
    if redis is not None:
        key = f"yx:evalfail:{candidate_id}"
        attempt = int(await redis.incr(key))
        await redis.expire(key, FAILURE_COUNT_TTL)
    detail: dict[str, Any] = {"candidate_id": str(candidate_id), "error_type": error_type(exc), "error": str(exc)[:300],
                              "where": where(exc), "attempt": attempt, "max_attempts": MAX_EVALUATION_FAILURES,
                              "at": now.isoformat()}
    async with session_factory() as session:
        cand = await session.get(TradingCandidate, candidate_id)
        if cand is not None:
            detail.update({"mint": (cand.detail or {}).get("mint"), "engine": cand.engine, "state": cand.state})
            await store.add_timeline_event(session, "candidate_evaluation_failed", now, detail, candidate_id=cand.id)
            if attempt >= MAX_EVALUATION_FAILURES and CandidateState(cand.state) not in TERMINAL_STATES:
                w = detail["where"]
                apply_transition(cand, CandidateState.REJECTED,
                                 reason=f"EVALUATION_FAILED x{attempt}: {detail['error_type']} at "
                                        f"{w.get('file')}:{w.get('function')}:{w.get('line')}"[:500])
                detail["decision"] = "REJECTED (evaluation kept failing; see the error above)"
            else:
                detail["decision"] = "retried on the next evaluation cycle"
        await session.commit()
    return detail


async def clear_failures(redis: Redis | None, candidate_id) -> None:
    if redis is not None:
        await redis.delete(f"yx:evalfail:{candidate_id}")
