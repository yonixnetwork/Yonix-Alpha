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

A LIVE target (global mode LIVE, strategy AUTO/MANUAL, all three
environment locks open, live readiness confirmed) creates a pending LIVE
position and BUY order for the paper-trading service's order worker; the
position only fills when the transaction confirms on chain.
"""

from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import live_trading, paper_engine, paper_execution
from yonixalpha_core.ml.gate_features import FEATURE_VERSION
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.gate import Assessment, assess
from yonixalpha_core.safety.models import FinalDecision, GlobalMode, StrategyMode
from yonixalpha_core.solana import pump_stream, pumpswap
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


async def _holder_snapshot(redis, mint: str, inp) -> dict:
    """What exit intelligence compares holder changes against: shares at
    entry, supply, and the curve/pool accounts that are not holders."""
    h, t = inp.holders, inp.token
    if h is None or t is None:
        return {}
    meta = await pump_stream.load_meta(redis, mint) or {}
    excluded = [a for a in (meta.get("bonding_curve"), pumpswap.canonical_pool(mint)) if a]
    return {"holders_at_entry": {"top1_share": str(h.top1_share), "top10_share": str(h.top10_share),
                                 "creator_share": str(h.creator_share) if h.creator_share is not None else None},
            "supply_raw": str(t.supply_raw), "holder_excluded": excluded}


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
    live_intent = (await store.load_global_mode(session) == GlobalMode.LIVE and strategy_mode in (StrategyMode.AUTO, StrategyMode.MANUAL)
                   and store.live_trading_permitted(settings))
    controls, account, settings_meta = await pipeline.load_controls(session, redis, settings, engine, strategy_mode, mint, now,
                                                                    approval, live=live_intent)
    live_ready, live_reason = await live_trading.live_readiness(redis, settings, now) if live_intent else (None, None)
    if engine == "solana_migration":
        inp, evidence = await assemble_migrated(sources, mint, now, controls)
    else:
        inp, evidence = await assemble_fresh(sources, mint, now, controls, engine=engine)
    inp.live_ready, inp.live_not_ready_reason = live_ready, live_reason
    # Target evidence: resistance comes from the assembler; strategy history from closed trades.
    if inp.targets is not None:
        inp.targets.historical_mfe, inp.targets.samples = await pipeline.historical_excursion(session, engine)
    # Operator exit plan (dashboard strategy config); unset parts are automatic.
    inp.overrides = pipeline.manual_overrides(await store.load_strategy_config(session, engine),
                                              inp.market.price if inp.market else None, inp.side)
    lifecycle = "MIGRATED" if engine == "solana_migration" else "FRESH"
    if inp.liquidity_model is not None:
        adapter = "pumpswap_pool" if lifecycle == "MIGRATED" else "pump_curve"
    else:
        adapter = "jupiter" if inp.quote is not None else None
    inp.ml, ml_info = await pipeline.champion_prediction(session, redis, engine, evidence.get("features") or {})
    vers = await pipeline.versions(session, settings_meta, inp.signal, adapter)
    vers["ml_model"] = f"{ml_info['model']} v{ml_info['version']}" if inp.ml else None
    a = assess(inp, controls.settings, versions=vers)
    ml_info["influenced"] = pipeline.ml_influenced(a)
    a.inputs_snapshot = {**evidence, "ml": ml_info}

    key = store.assessment_key(engine, mint, str(int(now.timestamp()) // REEVALUATE_EVERY_SECONDS))
    row, created = await store.persist_assessment(session, a, candidate.id, key)
    if not created:
        await session.commit()
        return a
    await pipeline.after_decision(session, redis, settings, a, row, str(candidate.id))
    await pipeline.after_ml(redis, a, row, ml_info)

    codes = {f.code for f in a.findings if f.action == a.decision}
    pool = ((evidence.get("pool") or {}).get("address")) if lifecycle == "MIGRATED" else None
    provenance = {"source": "PUMPFUN", "lifecycle": lifecycle, "pool": pool, "strategy": a.strategy,
                  "model_version": a.versions.get("ml_model"), "feature_version": FEATURE_VERSION,
                  "venue": {"pool": pool, "creator": evidence.get("creator") or None,
                            "real_liquidity_at_entry": str(inp.market.liquidity_quote) if inp.market else None,
                            **await _holder_snapshot(redis, mint, inp)}}
    if a.executable and a.execution_target.value == "LIVE":
        try:
            position = await live_trading.enter_live(session, redis, account, a, row.id, candidate, now, lifecycle,
                                                     inp.token.decimals if inp.token else None, provenance)
            pipeline.record_ml_sample(session, a, row.id, candidate.id, evidence.get("features") or {},
                                      *pipeline.ml_sample_args(inp.ml, ml_info))
            await pipeline.after_entry(session, redis, settings, a, position)
        except ValueError as exc:
            await store.add_timeline_event(session, "live_entry_refused", now, {"reason": str(exc)},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason=f"live entry refused: {exc}")
    elif a.executable:
        pipeline.record_ml_sample(session, a, row.id, candidate.id, evidence.get("features") or {}, *pipeline.ml_sample_args(inp.ml, ml_info))
        await store.add_timeline_event(session, "risk_calculated", now,
                                       {"size": str(a.plan.position_size.value), "stop": str(a.plan.stop_loss.value),
                                        "max_loss": str(a.plan.max_loss.value)}, candidate_id=candidate.id, assessment_id=row.id)
        rates = await paper_execution.effective_rates(session)
        if paper_execution.simulated_failure(f"entry:{row.id}", rates["entry_pct"]):
            # Same outcome as a live buy that never lands: no position.
            await store.add_timeline_event(session, "paper_entry_failed", now,
                                           {"simulated": True, "reason": "simulated network/confirmation failure",
                                            "failure_pct": str(rates["entry_pct"]), "source": rates["entry_source"]},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason="paper entry failed (simulated execution failure)")
            await session.commit()
            return a
        try:
            position = await paper_engine.open_position(
                session, account, a, row.id, candidate, inp.liquidity_model, inp.quote,
                inp.token.transfer_fee_bps if inp.token else None, now,
                venue={"type": adapter, "kind": "spot", "decimals": inp.token.decimals if inp.token else None,
                       **provenance["venue"]},
                max_slippage_bps=controls.settings.max_slippage_bps,
            )
            position.execution_mode, position.source, position.lifecycle = "PAPER", "PUMPFUN", lifecycle
            position.execution_provider = live_trading.PAPER_PROVIDER
            position.execution_route = "pump-amm" if lifecycle == "MIGRATED" else "pump"
            position.pool, position.strategy = pool, a.strategy
            position.model_version, position.feature_version = provenance["model_version"], FEATURE_VERSION
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
