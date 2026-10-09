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
  token migrated (curve engines) -> MIGRATED: handed to the migration engine

A LIVE target (global mode LIVE, strategy AUTO/MANUAL, all three
environment locks open, live readiness confirmed) creates a pending LIVE
position and BUY order for the paper-trading service's order worker; the
position only fills when the transaction confirms on chain.
"""

import time
from datetime import datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import deployer_intel, execution_analysis, live_smoke, live_trading, opportunities, paper_engine, paper_execution
from yonixalpha_core.ml.gate_features import FEATURE_VERSION
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.gate import CURVE_ENGINES, Assessment, assess
from yonixalpha_core.safety.models import FinalDecision, GlobalMode, StrategyMode
from yonixalpha_core.solana import pump_stream, pumpswap
from yonixalpha_core.solana.codec import db_safe
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
    session: AsyncSession, redis: Redis, settings: Settings, sources: Sources, candidate: TradingCandidate, now: datetime,
    operator: dict | None = None,
) -> Assessment | None:
    """`operator`: a manual BUY request (yonixalpha_core.manual_trade). The
    same evaluation runs with the operator's decision in place of the
    strategy signal; the outcome is written to operator["result"]."""
    engine = ENGINE_OF[candidate.engine]
    mint = candidate.detail["mint"]
    eval_started = time.time()

    # Per-candidate pacing: each evaluation costs several RPC/HTTP calls.
    # A manual request is evaluated when it is made.
    if operator is None and not await redis.set(f"yx:gate:pace:{candidate.id}", "1", nx=True, ex=REEVALUATE_EVERY_SECONDS):
        return None

    if engine in CURVE_ENGINES:
        stream_curve = await pump_stream.load_curve(redis, mint)
        if stream_curve is not None and stream_curve.pool:
            # Pre-migration rules stop applying the moment a pool exists: the
            # migration engine re-evaluates the token (its funnel picks up the
            # same migration event) with pool liquidity, routes and flow.
            apply_transition(candidate, CandidateState.MIGRATED,
                             reason=f"MIGRATION_DETECTED: PumpSwap pool {stream_curve.pool} — handed to solana_migration "
                                    "(MIGRATED_ANALYSIS with pool rules)")
            await store.add_timeline_event(session, "migration_detected", now, {"pool": stream_curve.pool, "from_engine": engine},
                                           candidate_id=candidate.id)
            await session.commit()
            if operator is not None:
                operator["result"] = {"status": "BLOCKED", "reason": "the token migrated while the request was queued; "
                                      "press BUY again — the route switches to the PumpSwap pool automatically"}
            return None

    if candidate.state == CandidateState.DISCOVERED.value:
        apply_transition(candidate, CandidateState.ANALYZING, reason=f"safety gate analysis started ({engine})")
        await store.add_timeline_event(session, "risk_analysis_started", now, {"engine": engine}, candidate_id=candidate.id)

    strategy_mode = await store.load_strategy_mode(session, engine)
    if strategy_mode == StrategyMode.OFF and operator is None:
        apply_transition(candidate, CandidateState.REJECTED, reason=f"strategy {engine} is OFF")
        await session.commit()
        return None

    approval = await pipeline.approval_granted(session, now, candidate_id=candidate.id)
    if operator is not None:
        # The operator's confirmed BUY is the approval: MANUAL semantics for
        # this one evaluation (approval-level warnings are accepted; every
        # stricter finding still blocks).
        strategy_mode, approval = StrategyMode.MANUAL, True
    live_intent = (await store.load_global_mode(session) == GlobalMode.LIVE and strategy_mode in (StrategyMode.AUTO, StrategyMode.MANUAL)
                   and store.live_trading_permitted(settings))
    controls, account, settings_meta = await pipeline.load_controls(session, redis, settings, engine, strategy_mode, mint, now,
                                                                    approval, live=live_intent,
                                                                    source="manual" if operator is not None else "sniper")
    live_ready, live_reason = await live_trading.live_readiness(redis, settings, now) if live_intent else (None, None)
    if engine == "solana_migration":
        inp, evidence = await assemble_migrated(sources, mint, now, controls)
    else:
        inp, evidence = await assemble_fresh(sources, mint, now, controls, engine=engine)
    inp.live_ready, inp.live_not_ready_reason = live_ready, live_reason
    inp.operator_request = operator is not None
    if inp.intel is not None:
        # Deployer history as of now (launches resolved before this decision only).
        try:
            async with session.begin_nested():  # a failed read must not touch the evaluation's own transaction
                inp.intel["deployer"] = await deployer_intel.features_asof(session, inp.creator, now, exclude_mint=mint)
        except Exception as exc:  # noqa: BLE001 - evidence; its failure is recorded, never guessed
            inp.intel["deployer"] = {"status": "UNAVAILABLE", "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                                     "feature_version": deployer_intel.FEATURE_VERSION}
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

    key = store.assessment_key(engine, mint, f"manual-{operator['id']}" if operator is not None
                               else str(int(now.timestamp()) // REEVALUATE_EVERY_SECONDS))
    row, created = await store.persist_assessment(session, a, candidate.id, key)
    if not created:
        await session.commit()
        return a
    await pipeline.after_decision(session, redis, settings, a, row, str(candidate.id))
    await pipeline.after_ml(redis, a, row, ml_info)

    codes = {f.code for f in a.findings if f.action == a.decision}
    pool = ((evidence.get("pool") or {}).get("address")) if lifecycle == "MIGRATED" else None
    requested_at = None
    if operator is not None and operator.get("requested_at"):
        try:
            requested_at = datetime.fromisoformat(operator["requested_at"]).timestamp()
        except ValueError:
            requested_at = None
    decision_ctx = execution_analysis.decision_context(inp, evidence, a, eval_started, time.time(), lifecycle, requested_at)
    meta = await pump_stream.load_meta(redis, mint) or {}
    decision_ctx["token_created_at"] = int(meta["created_at"]) if meta.get("created_at") else None
    decision_ctx["discovered_at"] = candidate.created_at.isoformat() if candidate.created_at else None
    provenance = {"source": "PUMPFUN", "lifecycle": lifecycle, "pool": pool, "strategy": a.strategy, "decision": decision_ctx,
                  "model_version": a.versions.get("ml_model"), "feature_version": FEATURE_VERSION,
                  "venue": {"pool": pool, "creator": evidence.get("creator") or None,
                            "real_liquidity_at_entry": str(inp.market.liquidity_quote) if inp.market else None,
                            **await _holder_snapshot(redis, mint, inp)}}
    if operator is None and not live_intent and inp.signal is not None and inp.signal.qualified:
        # LIVE_EXECUTION_SMOKE_TEST: only while an admin-armed run exists for
        # this category; the full gate runs again against the live wallet and
        # decides. Normal trading stays in its own mode either way.
        run = await live_smoke.armed_run(session, now)
        if run is not None and live_smoke.CATEGORY_ENGINE.get(run.category) == engine:
            position = await live_smoke.try_entry(session, redis, settings, run, candidate=candidate, engine=engine, inp=inp,
                                                  versions=vers, evidence=a.inputs_snapshot, lifecycle=lifecycle,
                                                  provenance=provenance, now=now)
            if position is not None:
                await session.commit()
                log.info("gate.smoke_test_entry", candidate_id=str(candidate.id), mint=mint, engine=engine,
                         run=str(run.id), route=position.execution_route)
                return a
    opened = live_opened = None
    if a.executable and a.execution_target.value == "LIVE":
        try:
            position = await live_trading.enter_live(session, redis, account, a, row.id, candidate, now, lifecycle,
                                                     inp.token.decimals if inp.token else None, provenance)
            pipeline.record_ml_sample(session, a, row.id, candidate.id, evidence.get("features") or {},
                                      *pipeline.ml_sample_args(inp.ml, ml_info))
            # The entry notification (Telegram) is sent after the order is
            # committed: the order worker only sees a committed order.
            live_opened = opened = position
            if operator is not None:
                operator["result"] = {"status": "SUBMITTING", "target": "LIVE", "position_id": str(position.id)}
        except ValueError as exc:
            await store.add_timeline_event(session, "live_entry_refused", now, {"reason": str(exc)},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason=f"live entry refused: {exc}")
            if operator is not None:
                operator["result"] = {"status": "BLOCKED", "stage": "LIVE_ENTRY_REFUSED", "reason": str(exc)}
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
            if operator is not None:
                operator["result"] = {"status": "FAILED", "stage": "SUBMISSION_FAILED",
                                      "reason": "paper entry failed (simulated network/confirmation failure)"}
            await session.commit()
            return a
        try:
            drift = await paper_execution.measured_live_drift(session)
            position = await paper_engine.open_position(
                session, account, a, row.id, candidate, inp.liquidity_model, inp.quote,
                inp.token.transfer_fee_bps if inp.token else None, now,
                venue={"type": adapter, "kind": "spot", "decimals": inp.token.decimals if inp.token else None,
                       **provenance["venue"]},
                max_slippage_bps=controls.settings.max_slippage_bps,
                entry_drift_pct=drift["buy_pct"],  # what a LIVE buy loses before it lands (measured)
            )
            position.execution_mode, position.source, position.lifecycle = "PAPER", "PUMPFUN", lifecycle
            position.execution_provider = live_trading.PAPER_PROVIDER
            position.execution_route = "pump-amm" if lifecycle == "MIGRATED" else "pump"
            position.pool, position.strategy = pool, a.strategy
            position.model_version, position.feature_version = provenance["model_version"], FEATURE_VERSION
            await pipeline.after_entry(session, redis, settings, a, position)
            opened = position
            if operator is not None:
                operator["result"] = {"status": "PAPER_POSITION_OPEN", "target": "PAPER", "position_id": str(position.id)}
        except paper_engine.FillError as exc:
            await store.add_timeline_event(session, "paper_entry_failed", now, {"reason": str(exc)},
                                           candidate_id=candidate.id, assessment_id=row.id)
            apply_transition(candidate, CandidateState.REJECTED, reason=f"paper entry failed: {exc}")
            if operator is not None:
                operator["result"] = {"status": "FAILED", "stage": "QUOTE_FAILED", "reason": f"paper entry failed: {exc}"}
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

    if operator is not None and "result" not in operator:
        blockers = [{"code": f.code, "category": f.category.value, "message": f.message}
                    for f in a.findings if f.action == a.decision]
        operator["result"] = {"status": "BLOCKED", "decision": a.decision.value, "stage": a.status_label,
                              "reason": "; ".join(a.reasons), "blockers": blockers,
                              # Why data was missing (e.g. "curve rpc: All RPC endpoints failed (env:primary: HTTP 429)").
                              "data_errors": [str(e)[:300] for e in (evidence.get("errors") or [])][:8]}
        if (candidate.detail or {}).get("manual_only") and candidate.state not in (CandidateState.REJECTED.value,):
            apply_transition(candidate, CandidateState.REJECTED, reason=f"manual BUY not executable: {a.status_label}")
    if operator is not None:
        operator["result"].update({"assessment_id": str(row.id), "decision": a.decision.value, "target": a.execution_target.value,
                                   "size": str(a.plan.position_size.value) if a.plan.position_size else None})
    if opened is not None or candidate.state == CandidateState.REJECTED.value:
        # The episode ended (entered, rejected or expired): record the state it
        # was decided on; what the token did afterwards is tracked from here.
        try:
            snap = opportunities.gate_snapshot(inp, evidence, a, decision_ctx)
            snap["market_cap_discovery_sol"] = await opportunities.discovery_market_cap(
                session, mint, Decimal(inp.token.supply_raw) if inp.token and inp.token.supply_raw else None)
            last = (candidate.state_history or [{}])[-1]
            decision = ("EXECUTE" if opened is not None else
                        "EXPIRED" if str(last.get("reason", "")).startswith("not executable within") else a.decision.value)
            await opportunities.record(
                session, key=f"gate:{candidate.id}", mint=mint, symbol=a.symbol, engine=engine, stage="GATE", decision=decision,
                traded=opened is not None, reasons=list(a.reasons), decided_at=now, snapshot=db_safe(snap),
                candidate_id=candidate.id, assessment_id=row.id, position_id=opened.id if opened is not None else None,
                execution_mode=opened.execution_mode if opened is not None else None)
        except Exception as exc:  # noqa: BLE001 - observation data never blocks a decision
            log.warning("gate.opportunity_record_failed", mint=mint, error=f"{type(exc).__name__}: {exc}")
    await session.commit()
    if live_opened is not None:
        await pipeline.after_entry(session, redis, settings, a, live_opened)
        await session.commit()
    log.info("gate.decision", candidate_id=str(candidate.id), mint=mint, engine=engine, decision=a.decision.value,
             status=a.status_label, target=a.execution_target.value,
             size=str(a.plan.position_size.value) if a.plan.position_size else None, errors=evidence.get("errors"))
    return a
