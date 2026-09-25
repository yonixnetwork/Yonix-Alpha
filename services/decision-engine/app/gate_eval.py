"""Safety-gate evaluation for candidates from the pump.fun stream funnel
(detail.source == "pump_stream"). Legacy candidates keep the Phase 5 path in
app/evaluate.py.

Per evaluation: load the operator's controls from the DB, assemble live
inputs, run the gate, persist the full assessment (every decision,
including rejections and waits), and act on it:

  EXECUTE / REDUCE_SIZE -> open the paper position from the gate's own plan
  REQUIRE_MANUAL_APPROVAL -> wait for an operator approval; an approval only
                             lets the NEXT full re-evaluation pass, it never
                             skips any check
  WAIT / NO_TRADE        -> keep observing until the engine's time limit
  REJECT                 -> reject the candidate now

Live execution of Solana trades is not implemented; a LIVE target (which
needs all three environment locks open) is refused and recorded.
"""

from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import kill_switch, paper_engine
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import MLFeatureSnapshot, RiskAssessment, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import Assessment, assess
from yonixalpha_core.safety.models import FinalDecision, StrategyMode
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh, assemble_migrated
from yonixalpha_core.state_machine import CandidateState, apply_transition

log = get_logger("decision-engine.gate")

ENGINE_OF = {"discovery": "solana_fresh", "migration": "solana_migration"}
MAX_OBSERVATION_SECONDS = {"solana_fresh": 30 * 60, "solana_migration": 60 * 60}
REEVALUATE_EVERY_SECONDS = 30
APPROVAL_VALID_SECONDS = 10 * 60


def is_gate_candidate(candidate: TradingCandidate) -> bool:
    return (candidate.detail or {}).get("source") == "pump_stream" and candidate.engine in ENGINE_OF


async def _approval_granted(session: AsyncSession, candidate_id, now: datetime) -> bool:
    row = (
        await session.execute(
            select(RiskAssessment.approved_at)
            .where(RiskAssessment.candidate_id == candidate_id, RiskAssessment.approval_state == "APPROVED")
            .order_by(RiskAssessment.approved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None and now - row <= timedelta(seconds=APPROVAL_VALID_SECONDS)


def _observing_since(candidate: TradingCandidate) -> datetime | None:
    for entry in candidate.state_history or []:
        if entry.get("state") == CandidateState.OBSERVING.value:
            return datetime.fromisoformat(entry["at"])
    return None


async def evaluate_with_gate(
    session: AsyncSession, redis: Redis, settings: Settings, sources: Sources, candidate: TradingCandidate, now: datetime
) -> Assessment | None:
    engine = ENGINE_OF[candidate.engine]
    mint = candidate.detail["mint"]

    # Per-candidate pacing: each evaluation costs several RPC/HTTP calls.
    if not await redis.set(f"yx:gate:pace:{candidate.id}", "1", nx=True, ex=REEVALUATE_EVERY_SECONDS):
        return None

    if candidate.state == CandidateState.DISCOVERED.value:
        apply_transition(candidate, CandidateState.OBSERVING, reason=f"safety gate evaluation ({engine})")

    strategy_mode = await store.load_strategy_mode(session, engine)
    if strategy_mode == StrategyMode.OFF:
        apply_transition(candidate, CandidateState.REJECTED, reason=f"strategy {engine} is OFF")
        await session.commit()
        return None

    safety, settings_meta = await store.load_settings(session, engine)
    account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[engine])
    controls = Controls(
        settings=safety,
        account=await store.account_state(session, account, mint, now, await kill_switch.is_engaged(redis)),
        blacklist=await store.load_blacklist(session),
        custom_rules=await store.load_custom_rules(session),
        global_mode=await store.load_global_mode(session),
        strategy_mode=strategy_mode,
        live_trading_permitted=store.live_trading_permitted(settings),
        manual_approval_granted=await _approval_granted(session, candidate.id, now),
    )
    assemble = assemble_fresh if engine == "solana_fresh" else assemble_migrated
    inp, evidence = await assemble(sources, mint, now, controls)
    a = assess(inp, safety, versions={"settings": settings_meta, "strategy": inp.signal.name + " v" + inp.signal.version
                                      if inp.signal else None})
    a.inputs_snapshot = evidence

    key = store.assessment_key(engine, mint, str(int(now.timestamp()) // REEVALUATE_EVERY_SECONDS))
    row, created = await store.persist_assessment(session, a, candidate.id, key)
    if not created:
        await session.commit()
        return a

    if a.executable and a.execution_target.value == "LIVE":
        await store.add_timeline_event(session, "live_refused", now,
                                       {"reason": "live Solana execution is not implemented"},
                                       candidate_id=candidate.id, assessment_id=row.id)
        apply_transition(candidate, CandidateState.REJECTED, reason="LIVE target but live Solana execution is not implemented")
    elif a.executable:
        session.add(MLFeatureSnapshot(candidate_id=candidate.id, symbol=a.symbol[:64],
                                      features=evidence.get("features") or {}))
        try:
            await paper_engine.open_position(
                session, account, a, row.id, candidate, inp.liquidity_model, inp.quote,
                inp.token.transfer_fee_bps if inp.token else None, now,
                venue={"type": "pump_curve" if inp.liquidity_model is not None else "jupiter",
                       "decimals": inp.token.decimals if inp.token else None},
            )
        except paper_engine.FillError as exc:
            await store.add_timeline_event(session, "paper_entry_refused", now, {"reason": str(exc)},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason=f"paper entry refused: {exc}")
    elif a.decision == FinalDecision.REJECT:
        apply_transition(candidate, CandidateState.REJECTED, reason="; ".join(a.reasons)[:500])
    else:
        started = _observing_since(candidate) or now
        if (now - started).total_seconds() >= MAX_OBSERVATION_SECONDS[engine]:
            apply_transition(candidate, CandidateState.REJECTED,
                             reason=f"not executable within {MAX_OBSERVATION_SECONDS[engine]}s; last: {a.status_label}")

    await session.commit()
    log.info("gate.decision", candidate_id=str(candidate.id), mint=mint, engine=engine, decision=a.decision.value,
             status=a.status_label, target=a.execution_target.value,
             size=str(a.plan.position_size.value) if a.plan.position_size else None, errors=evidence.get("errors"))
    return a

