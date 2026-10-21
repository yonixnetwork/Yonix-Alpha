"""Why is momentum not trading? Every stage a momentum token passes, counted
over a window, and a verdict that separates "no opportunity" from "the
system is broken". Read-only, bounded queries; used by the dashboard
(GET /api/strategies/solana_momentum/pipeline) and the server tool
(python -m yonixalpha_core.tools.momentum_pipeline).

Stages (what is recorded for each):
  1 STREAM      pump.fun stream and PumpPortal messages     scanner_health
  2 SCANNED     tokens trading in the last 2 min, per funnel pass (Redis,
                hourly buckets): no curve address / curve done / too young
                / considered
  3 PREFILTER   considered -> failed the acceleration prefilter / passed
  4 CANDIDATE   passed -> candidate created / slot budget full / promoted
                within the last hour already / WATCH row (strategy OFF)
  5 EVALUATED   momentum gate decisions (risk_assessments, solana_momentum)
  6 BLOCKED     why non-executable decisions were refused, grouped as
                strategy signal / mode-risk-limits / token safety
  7 EXECUTED    paper or live positions opened for momentum
  8 FAILED      live entries refused after the gate said yes, failed orders

Verdict codes (the brief's A-H):
  A_NO_EVENTS        the stream is disconnected / stale
  B_NOT_PROCESSED    events arrive but the funnel did not run
  C_FILTERED         tokens seen, none passed the prefilter / token safety
  D_NOT_EVALUATED    candidates created but no gate decision followed
  E_BLOCKED          signals qualified but mode / risk / limits blocked them
  F_EXECUTION_FAILED executable decisions without a position, or failed orders
  G_OBSERVE_ONLY     strategy OFF: WATCH rows recorded, no trade by design
  H_NO_SIGNAL        evaluated, but the momentum signal never qualified
  TRADING            positions were opened in the window
  NO_EVIDENCE        nothing recorded in the window (e.g. counters not yet
                     deployed) - never reported as "no opportunity"
Several can apply; `verdict` is the first in pipeline order, `findings`
lists every one with its numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import scanner_health
from yonixalpha_core.safety import store
from yonixalpha_core.solana import pump_stream

ENGINE = "solana_momentum"
FUNNEL = f"{pump_stream.PREFIX}:funnel"
COUNTERS = ("passes", "momentum_active", "momentum_no_curve_address", "momentum_curve_done", "momentum_too_young",
            "momentum_considered", "momentum_prefilter_failed", "momentum_budget_full", "momentum_recently_promoted",
            "momentum_promoted", "momentum_watch_recorded")
MODE_CODES = ("STRATEGY_OFF", "MANUAL_MODE", "AUTO_NO_APPROVAL", "KILL_SWITCH", "CHAIN_DISABLED", "NEW_ENTRIES_DISABLED",
              "LIVE_NOT_READY", "LIVE_NOT_PERMITTED")
SIGNAL_CODES = ("SIGNAL_NOT_QUALIFIED",)
STREAM_STAGES = ("stream_event", "pumpportal_event")

CANDIDATES_SQL = """
SELECT state, count(*) FROM trading_candidates WHERE engine = 'momentum' AND created_at >= :since GROUP BY 1
"""
DECISIONS_SQL = """
SELECT count(*), count(*) FILTER (WHERE executable),
       count(*) FILTER (WHERE executable AND execution_target = 'PAPER'),
       count(*) FILTER (WHERE executable AND execution_target = 'LIVE'),
       count(DISTINCT asset_id), max(evaluated_at)
FROM risk_assessments WHERE engine = :engine AND evaluated_at >= :since
"""
BLOCKERS_SQL = """
SELECT f->>'code' AS code, coalesce(f->>'category', '?') AS category, count(*) AS n
FROM (SELECT assessment FROM risk_assessments
      WHERE engine = :engine AND evaluated_at >= :since AND NOT executable
      ORDER BY evaluated_at DESC LIMIT 5000) r,
     jsonb_array_elements(r.assessment -> 'findings') f
WHERE f->>'action' IN ('NO_TRADE', 'REJECT', 'WAIT')
GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25
"""
POSITIONS_SQL = """
SELECT coalesce(execution_mode, 'PAPER'), count(*), max(created_at) FROM paper_positions
WHERE engine = :engine AND created_at >= :since GROUP BY 1
"""
WATCH_SQL = """
SELECT decision, count(*), max(decided_at) FROM opportunity_outcomes
WHERE engine = :engine AND stage = 'WATCH' AND decided_at >= :since GROUP BY 1
"""
REFUSED_SQL = """
SELECT count(*) FROM trade_timeline_events e JOIN paper_positions p ON p.id = e.position_id
WHERE e.occurred_at >= :since AND e.event_type = 'live_entry_refused' AND p.engine = :engine
"""
FAILED_ORDERS_SQL = """
SELECT count(*) FROM execution_orders o JOIN paper_positions p ON p.id = o.position_id
WHERE o.created_at >= :since AND o.status IN ('FAILED', 'EXPIRED') AND p.engine = :engine
"""
LAST_DECISION_SQL = "SELECT max(evaluated_at) FROM risk_assessments WHERE engine = :engine"


def _hour_keys(now: datetime, hours: int) -> list[str]:
    start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [f"{FUNNEL}:h:{(start - timedelta(hours=i)):%Y%m%d%H}" for i in range(max(1, hours))]


async def funnel_counts(redis: Any, now: datetime, hours: int) -> dict[str, int] | None:
    """Summed hourly funnel counters; None when no bucket exists (counters
    not deployed yet, or the funnel never ran in the window)."""
    if redis is None:
        return None
    total = {k: 0 for k in COUNTERS}
    found = False
    for key in _hour_keys(now, hours):
        raw = await redis.hgetall(key)
        if not raw:
            continue
        found = True
        for k, v in raw.items():
            k = k.decode() if isinstance(k, bytes) else k
            if k in total:
                total[k] += int(v)
    return total if found else None


def classify(*, mode: str, stream_states: dict[str, str], funnel: dict[str, int] | None, candidates: dict[str, int],
             decisions: dict[str, Any], blockers: list[dict[str, Any]], positions: dict[str, int], watch: int,
             refused: int, failed_orders: int) -> list[dict[str, Any]]:
    """Every A-H finding that the numbers support, in pipeline order."""
    out: list[dict[str, Any]] = []

    def add(code: str, kind: str, message: str) -> None:
        out.append({"code": code, "kind": kind, "message": message})

    bad_stream = [k for k, v in stream_states.items() if v in (scanner_health.DISCONNECTED, scanner_health.DATA_GAP)]
    if bad_stream and len(bad_stream) == len(stream_states):
        add("A_NO_EVENTS", "SYSTEM", "no token events: " + ", ".join(f"{k} {stream_states[k]}" for k in bad_stream))
    elif bad_stream or any(v == scanner_health.STALE for v in stream_states.values()):
        add("A_NO_EVENTS", "WARNING", "a feed is degraded: " + ", ".join(f"{k} {v}" for k, v in stream_states.items()))
    if funnel is not None and funnel.get("passes", 0) == 0:
        add("B_NOT_PROCESSED", "SYSTEM", "the discovery funnel did not run in the window")
    if mode == "OFF":
        add("G_OBSERVE_ONLY", "BY_DESIGN", f"momentum is OFF: no trade by design; {watch} WATCH observation(s) recorded")
    if funnel is not None:
        considered, failed = funnel.get("momentum_considered", 0), funnel.get("momentum_prefilter_failed", 0)
        passed = considered - failed
        if funnel.get("momentum_active", 0) and not considered:
            add("C_FILTERED", "NO_OPPORTUNITY", f"{funnel['momentum_active']} active token(s) scanned, none old enough or "
                f"with a known curve (too young {funnel.get('momentum_too_young', 0)}, no curve address "
                f"{funnel.get('momentum_no_curve_address', 0)}, curve done {funnel.get('momentum_curve_done', 0)})")
        elif considered and passed <= 0:
            add("C_FILTERED", "NO_OPPORTUNITY", f"{considered} token check(s), none passed the acceleration prefilter")
        if mode != "OFF" and passed > 0 and not funnel.get("momentum_promoted", 0):
            if funnel.get("momentum_budget_full", 0):
                add("D_NOT_EVALUATED", "SYSTEM", f"{funnel['momentum_budget_full']} prefilter pass(es) dropped: the shared "
                    "candidate slot budget was full (fresh / migration candidates held every slot)")
            elif funnel.get("momentum_recently_promoted", 0):
                add("D_NOT_EVALUATED", "NO_OPPORTUNITY", "prefilter passes were tokens already promoted within the last hour")
    n_cand = sum(candidates.values())
    if mode != "OFF" and n_cand and not decisions.get("total"):
        add("D_NOT_EVALUATED", "SYSTEM", f"{n_cand} momentum candidate(s) created but no gate decision followed "
            "(is the decision engine running?)")
    total, execu = decisions.get("total", 0), decisions.get("executable", 0)
    if total and not execu:
        by_kind = {"signal": 0, "mode": 0, "safety": 0}
        for b in blockers:
            k = "signal" if b["code"] in SIGNAL_CODES else "mode" if (b["code"] in MODE_CODES or b["category"] == "ACCOUNT") \
                else "safety"
            by_kind[k] += b["n"]
        top = ", ".join(f"{b['code']} {b['n']}" for b in blockers[:5])
        if by_kind["signal"] and by_kind["signal"] >= max(by_kind["mode"], by_kind["safety"]):
            add("H_NO_SIGNAL", "NO_OPPORTUNITY", f"{total} decision(s), none qualified: the momentum signal did not "
                f"confirm (top reasons: {top})")
        if by_kind["mode"]:
            add("E_BLOCKED", "CONFIGURATION", f"mode / risk / limit findings blocked decisions (top reasons: {top})")
        if by_kind["safety"] and by_kind["safety"] > by_kind["signal"]:
            add("C_FILTERED", "NO_OPPORTUNITY", f"token safety checks refused the candidates (top reasons: {top})")
    opened = sum(positions.values())
    if execu and not opened:
        add("F_EXECUTION_FAILED", "SYSTEM", f"{execu} executable decision(s) but no position was opened")
    if refused or failed_orders:
        add("F_EXECUTION_FAILED", "SYSTEM", f"{refused} live entr(y/ies) refused at order time, {failed_orders} failed order(s)")
    if opened:
        add("TRADING", "OK", f"{opened} position(s) opened: " + ", ".join(f"{k} {v}" for k, v in positions.items()))
    if not out:
        add("NO_EVIDENCE", "UNKNOWN", "nothing recorded for momentum in the window; the stage counters may not be "
            "deployed yet. This is not evidence that there was no opportunity.")
    return out


async def report(session: AsyncSession, redis: Any, now: datetime | None = None, hours: int = 24) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    p = {"since": since, "engine": ENGINE}
    mode = (await store.load_strategy_mode(session, ENGINE)).value
    gm = (await store.load_global_mode(session)).value
    health = await scanner_health.report(session, redis, now) if redis is not None else {"stages": [], "overall": "DATA GAP"}
    stream = {s["stage"]: s["state"] for s in health.get("stages", []) if s["stage"] in STREAM_STAGES}
    funnel = await funnel_counts(redis, now, hours)
    cands = {str(k): int(v) for k, v in (await session.execute(text(CANDIDATES_SQL), {"since": since})).all()}
    d = (await session.execute(text(DECISIONS_SQL), p)).one()
    decisions = {"total": int(d[0]), "executable": int(d[1]), "executable_paper": int(d[2]), "executable_live": int(d[3]),
                 "tokens": int(d[4]), "last_at": d[5].isoformat() if d[5] else None}
    blockers = [{"code": c, "category": cat, "n": int(n)} for c, cat, n in (await session.execute(text(BLOCKERS_SQL), p)).all()]
    positions = {str(m): int(n) for m, n, _ in (await session.execute(text(POSITIONS_SQL), p)).all()}
    watch_rows = (await session.execute(text(WATCH_SQL), p)).all()
    watch = {str(k): int(n) for k, n, _ in watch_rows}
    refused = int((await session.execute(text(REFUSED_SQL), p)).scalar_one())
    failed = int((await session.execute(text(FAILED_ORDERS_SQL), p)).scalar_one())
    last_decision = (await session.execute(text(LAST_DECISION_SQL), {"engine": ENGINE})).scalar()
    findings = classify(mode=mode, stream_states=stream, funnel=funnel, candidates=cands, decisions=decisions,
                        blockers=blockers, positions=positions, watch=sum(watch.values()), refused=refused,
                        failed_orders=failed)
    stages = [
        {"stage": "STREAM", "value": health.get("overall"), "detail": stream},
        {"stage": "SCANNED", "value": None if funnel is None else funnel["momentum_active"], "detail": None if funnel is None else
         {k: funnel[k] for k in ("momentum_too_young", "momentum_no_curve_address", "momentum_curve_done")}},
        {"stage": "PREFILTER", "value": None if funnel is None else funnel["momentum_considered"], "detail": None if funnel is None
         else {"failed": funnel["momentum_prefilter_failed"],
               "passed": funnel["momentum_considered"] - funnel["momentum_prefilter_failed"]}},
        {"stage": "CANDIDATE", "value": sum(cands.values()), "detail": {
            "by_state": cands, "watch_recorded": sum(watch.values()),
            **({} if funnel is None else {"budget_full": funnel["momentum_budget_full"],
                                          "recently_promoted": funnel["momentum_recently_promoted"]})}},
        {"stage": "EVALUATED", "value": decisions["total"], "detail": {"tokens": decisions["tokens"],
                                                                     "last_at": decisions["last_at"]}},
        {"stage": "QUALIFIED", "value": decisions["executable"], "detail": {"paper": decisions["executable_paper"],
                                                                          "live": decisions["executable_live"]}},
        {"stage": "EXECUTED", "value": sum(positions.values()), "detail": positions},
        {"stage": "FAILED", "value": refused + failed, "detail": {"live_refused": refused, "failed_orders": failed}},
    ]
    return {"at": now.isoformat(), "hours": hours, "mode": mode, "global_mode": gm, "verdict": findings[0]["code"],
            "findings": findings, "stages": stages, "blockers": blockers, "watch": watch,
            "funnel_counters": "recorded" if funnel is not None else "not recorded in this window",
            "last_decision_ever": last_decision.isoformat() if last_decision else None}
