"""Safety-gate evaluation for candidates from the pump.fun stream funnel
(detail.source == "pump_stream"). Legacy candidates keep the Phase 5 path in
app/evaluate.py.

Per evaluation: load the operator's controls from the DB, assemble live
inputs, run the gate, persist the full assessment (every decision,
including rejections and waits), and act on it:

  EXECUTE / REDUCE_SIZE -> open the paper position from the gate's own plan
  REQUIRE_MANUAL_APPROVAL -> WAITING_FOR_APPROVAL; an approval only lets
                             the NEXT full re-evaluation pass, it never
                             skips any check
  WAIT (liquidity)        -> WAITING_FOR_LIQUIDITY
  WAIT / NO_TRADE         -> ANALYZING until the engine's time limit
  REJECT                  -> REJECTED now

Live execution of Solana trades is not implemented; a LIVE target (which
needs all three environment locks open) is refused and recorded.
"""

from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import paper_engine
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.gate import Assessment, assess
from yonixalpha_core.safety.models import FinalDecision, StrategyMode
from yonixalpha_core.solana.assembler import Sources, assemble_fresh, assemble_migrated
from yonixalpha_core.state_machine import CandidateState, apply_transition

log = get_logger("decision-engine.gate")

ENGINE_OF = {"discovery": "solana_fresh", "migration": "solana_migration", "momentum": "solana_momentum"}
MAX_OBSERVATION_SECONDS = {"solana_fresh": 30 * 60, "solana_migration": 60 * 60, "solana_momentum": 20 * 60}
REEVALUATE_EVERY_SECONDS = 30
GATE_STATES = [
    CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value, CandidateState.ANALYZING.value,
    CandidateState.WAITING_FOR_LIQUIDITY.value, CandidateState.WAITING_FOR_APPROVAL.value,
]


def is_gate_candidate(candidate: TradingCandidate) -> bool:
    return (candidate.detail or {}).get("source") == "pump_stream" and candidate.engine in ENGINE_OF


def _analysis_started(candidate: TradingCandidate) -> datetime | None:
    for entry in candidate.state_history or []:
        if entry.get("state") in (CandidateState.ANALYZING.value, CandidateState.OBSERVING.value):
            return datetime.fromisoformat(entry["at"])
    return None


def _move(candidate: TradingCandidate, target: CandidateState, reason: str) -> None:
    if candidate.state != target.value:
        apply_transition(candidate, target, reason=reason)


async def evaluate_with_gate(
    session: AsyncSession, redis: Redis, settings: Settings, sources: Sources, candidate: TradingCandidate, now: datetime
) -> Assessment | None:
    engine = ENGINE_OF[candidate.engine]
    mint = candidate.detail["mint"]

    # Per-candidate pacing: each evaluation costs several RPC/HTTP calls.
    if not await redis.set(f"yx:gate:pace:{candidate.id}", "1", nx=True, ex=REEVALUATE_EVERY_SECONDS):
        return None

    if candidate.state == CandidateState.DISCOVERED.value:
        apply_transition(candidate, CandidateState.ANALYZING, reason=f"safety gate analysis started ({engine})")
        await store.add_timeline_event(session, "risk_analysis_started", now, {"engine": engine}, candidate_id=candidate.id)

    strategy_mode = await store.load_strategy_mode(session, engine)
    if strategy_mode == StrategyMode.OFF:
        apply_transition(candidate, CandidateState.REJECTED, reason=f"strategy {engine} is OFF")
        await session.commit()
        return None

    approval = await pipeline.approval_granted(session, now, candidate_id=candidate.id)
    controls, account, settings_meta = await pipeline.load_controls(session, redis, settings, engine, strategy_mode, mint, now, approval)
    if engine == "solana_migration":
        inp, evidence = await assemble_migrated(sources, mint, now, controls)
    else:
        inp, evidence = await assemble_fresh(sources, mint, now, controls, engine=engine)
    adapter = "pump_curve" if inp.liquidity_model is not None else ("jupiter" if inp.quote is not None else None)
    a = assess(inp, controls.settings, versions=await pipeline.versions(session, settings_meta, inp.signal, adapter))
    a.inputs_snapshot = evidence

    key = store.assessment_key(engine, mint, str(int(now.timestamp()) // REEVALUATE_EVERY_SECONDS))
    row, created = await store.persist_assessment(session, a, candidate.id, key)
    if not created:
        await session.commit()
        return a
    await pipeline.after_decision(session, redis, settings, a, row, str(candidate.id))

    codes = {f.code for f in a.findings if f.action == a.decision}
    if a.executable and a.execution_target.value == "LIVE":
        await store.add_timeline_event(session, "live_refused", now,
                                       {"reason": "live Solana execution is not implemented"},
                                       candidate_id=candidate.id, assessment_id=row.id)
        apply_transition(candidate, CandidateState.REJECTED, reason="LIVE target but live Solana execution is not implemented")
    elif a.executable:
        pipeline.record_ml_sample(session, a, row.id, candidate.id, evidence.get("features") or {})
        await store.add_timeline_event(session, "risk_calculated", now,
                                       {"size": str(a.plan.position_size.value), "stop": str(a.plan.stop_loss.value),
                                        "max_loss": str(a.plan.max_loss.value)}, candidate_id=candidate.id, assessment_id=row.id)
        try:
            position = await paper_engine.open_position(
                session, account, a, row.id, candidate, inp.liquidity_model, inp.quote,
                inp.token.transfer_fee_bps if inp.token else None, now,
                venue={"type": adapter, "kind": "spot", "decimals": inp.token.decimals if inp.token else None,
                       "real_liquidity_at_entry": str(inp.market.liquidity_quote) if inp.market else None,
                       "creator": (evidence.get("creator") or None)},
                max_slippage_bps=controls.settings.max_slippage_bps,
            )
            await pipeline.after_entry(session, redis, settings, a, position)
        except paper_engine.FillError as exc:
            await store.add_timeline_event(session, "paper_entry_failed", now, {"reason": str(exc)},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason=f"paper entry failed: {exc}")
    elif a.decision == FinalDecision.REJECT:
        apply_transition(candidate, CandidateState.REJECTED, reason="; ".join(a.reasons)[:500])
    else:
        if a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL:
            _move(candidate, CandidateState.WAITING_FOR_APPROVAL, "operator approval required")
        elif "WAITING_FOR_LIQUIDITY" in codes or "MIGRATION_PENDING" in codes:
            _move(candidate, CandidateState.WAITING_FOR_LIQUIDITY, a.status_label)
        else:
            _move(candidate, CandidateState.ANALYZING, a.status_label)
        started = _analysis_started(candidate) or now
        if (now - started).total_seconds() >= MAX_OBSERVATION_SECONDS[engine]:
            apply_transition(candidate, CandidateState.REJECTED,
                             reason=f"not executable within {MAX_OBSERVATION_SECONDS[engine]}s; last: {a.status_label}")

    await session.commit()
    log.info("gate.decision", candidate_id=str(candidate.id), mint=mint, engine=engine, decision=a.decision.value,
             status=a.status_label, target=a.execution_target.value,
             size=str(a.plan.position_size.value) if a.plan.position_size else None, errors=evidence.get("errors"))
    return a
