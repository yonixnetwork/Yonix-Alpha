import asyncio
import signal
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import case, select

from yonixalpha_core import gate_events, manual_trade, x_narrative
from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent, TradingCandidate
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.venues.common import venue_health_snapshot
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.solana.market_data import DexScreenerClient, JupiterClient, RateBudget
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.runtime_watch import run_watcher
from yonixalpha_core.state_machine import CandidateState

from app import diagnostics
from app.evaluate import evaluate_candidate
from app.gate_eval import evaluate_with_gate, is_gate_candidate

log = get_logger("decision-engine.main")

EVAL_INTERVAL_SECONDS = 15  # legacy (non-gate) candidates: unchanged cadence
# Gate candidates: the loop wakes as soon as the funnel pushes a new
# candidate (gate_events.GATE_WAKE) and otherwise every GATE_LOOP_SECONDS;
# each candidate keeps its own pacing (30 s timer, or a meaningful event
# after event_min_interval_seconds), so this costs no extra RPC by itself.
GATE_LOOP_SECONDS = 5
SERVICE_NAME = "decision-engine"
EVALUABLE_STATES = [
    CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value, CandidateState.ANALYZING.value,
    CandidateState.WAITING_FOR_LIQUIDITY.value, CandidateState.WAITING_FOR_APPROVAL.value,
]
# Conservative client-side budgets. Neither provider's current free-tier
# limit was verifiable from the build environment; these stay well under
# the limits their public docs have historically stated.
JUPITER_REQUESTS_PER_MINUTE = 30
DEXSCREENER_REQUESTS_PER_MINUTE = 60


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"[{SERVICE_NAME}] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


async def _manual_loop(session_factory, redis, settings, stop_event: asyncio.Event, sources: Sources | None) -> None:
    """Manual BUY requests from the dashboard, evaluated as soon as they
    arrive through the same gate and execution code (gate_eval)."""
    while not stop_event.is_set():
        request_id = None
        try:
            request_id = await manual_trade.next_request(redis, timeout=2)
            if request_id is None:
                continue
            req = await manual_trade.get(redis, request_id)
            if req is None:
                continue
            if sources is None:
                await manual_trade.update(redis, request_id, "BLOCKED", stage="RPC_NOT_CONFIGURED",
                                          reason="no Solana RPC configured: the safety gate cannot evaluate the token")
                continue
            await manual_trade.update(redis, request_id, "EVALUATING")
            async with session_factory() as session:
                candidate = await session.get(TradingCandidate, UUID(req["candidate_id"]))
                if candidate is None or candidate.state not in EVALUABLE_STATES:
                    await manual_trade.update(redis, request_id, "BLOCKED", stage="CANDIDATE_CLOSED",
                                              reason=f"the token's candidate is {candidate.state if candidate else 'missing'}; "
                                                     "press BUY again")
                    continue
                operator = {"id": request_id}
                await evaluate_with_gate(session, redis, settings, sources, candidate, datetime.now(timezone.utc), operator)
            result = operator.get("result") or {"status": "BLOCKED", "reason": "not evaluated (unknown reason)"}
            await manual_trade.update(redis, request_id, result.pop("status"), **result)
            log.info("manual_buy.evaluated", request=request_id, mint=req.get("mint"), status=(await manual_trade.get(redis, request_id) or {}).get("status"))
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the queue
            log.error("manual_buy.failed", request=request_id, error=f"{type(exc).__name__}: {exc}"[:300])
            if request_id:
                await manual_trade.update(redis, request_id, "FAILED", stage="EVALUATION_FAILED",
                                          reason=f"{diagnostics.error_type(exc)}: {exc}"[:300])
            await _record_system_event(session_factory, "manual_buy_failed", "error",
                                       {"request": request_id, "error": f"{type(exc).__name__}: {exc}"[:300]})


async def _evaluation_loop(session_factory, redis, settings, stop_event: asyncio.Event, sources: Sources | None = None) -> None:
    loop = asyncio.get_running_loop()
    last_legacy = 0.0
    while not stop_event.is_set():
        legacy_due = loop.time() - last_legacy >= EVAL_INTERVAL_SECONDS
        if legacy_due:
            last_legacy = loop.time()
        try:
            async with session_factory() as session:
                # Newly discovered candidates first, newest first: a fresh
                # token's first evaluation never waits behind older ones.
                result = await session.execute(
                    select(TradingCandidate.id).where(TradingCandidate.state.in_(EVALUABLE_STATES))
                    .order_by(case((TradingCandidate.state == CandidateState.DISCOVERED.value, 0), else_=1),
                              TradingCandidate.created_at.desc()))
                candidate_ids = result.scalars().all()

            evaluated = 0
            for candidate_id in candidate_ids:
                # Each candidate gets its own session/transaction, so one
                # candidate's failure (a bad row, a transient DB error)
                # can't abort evaluation of the rest of the batch.
                try:
                    async with session_factory() as session:
                        candidate = await session.get(TradingCandidate, candidate_id)
                        if candidate is None or candidate.state not in EVALUABLE_STATES:
                            continue  # state changed since the query above
                        if (candidate.detail or {}).get("manual_only"):
                            continue  # evaluated only by its manual BUY request (_manual_loop)
                        if is_gate_candidate(candidate):
                            if sources is None:
                                continue
                            await evaluate_with_gate(session, redis, settings, sources, candidate, datetime.now(timezone.utc))
                        else:
                            if not legacy_due:
                                continue
                            await evaluate_candidate(session, redis, settings, candidate, datetime.now(timezone.utc))
                    await diagnostics.clear_failures(redis, candidate_id)
                    evaluated += 1
                except Exception as exc:  # noqa: BLE001
                    try:
                        detail = await diagnostics.record_failure(session_factory, redis, candidate_id, exc,
                                                                  datetime.now(timezone.utc))
                    except Exception as rec_exc:  # noqa: BLE001 - the alert below must still go out
                        detail = {"candidate_id": str(candidate_id), "error_type": diagnostics.error_type(exc),
                                  "error": str(exc)[:300], "diagnostics_failed": str(rec_exc)[:200]}
                    log.error("evaluate.candidate_failed", **{k: str(v) for k, v in detail.items()})
                    await _record_system_event(session_factory, "candidate_evaluation_failed", "error", detail)

            if evaluated:
                log.info("evaluation_loop.completed", count=evaluated)
        except Exception as exc:  # noqa: BLE001
            log.error("evaluation_loop.failed", error=str(exc))
            await _record_system_event(session_factory, "evaluation_loop_failed", "error", {"error": str(exc)})

        woken = await gate_events.wait_for_wake(redis, stop_event, GATE_LOOP_SECONDS)
        if woken:
            log.info("evaluation_loop.woken", candidates=len(woken))


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await _record_system_event(
        session_factory,
        "service_started",
        "info",
        {"trading_enabled": settings.TRADING_ENABLED, "live_trading_enabled": settings.LIVE_TRADING_ENABLED},
    )
    log.info(
        "decision-engine.started",
        trading_enabled=settings.TRADING_ENABLED,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
    )

    http_client = httpx.AsyncClient()
    sources = None
    if settings.SOLANA_RPC_URL:
        sources = Sources(
            redis=redis,
            rpc=RpcManager.create(client=http_client, primary_url=settings.SOLANA_RPC_URL, backup_url=settings.SOLANA_RPC_BACKUP_URL,
                                  extra_backup_urls=[settings.SOLANA_RPC_BACKUP_URL_2, settings.SOLANA_RPC_BACKUP_URL_3]),
            jupiter=JupiterClient(http_client, settings.JUPITER_API_KEY, RateBudget(JUPITER_REQUESTS_PER_MINUTE)),
            dexscreener=DexScreenerClient(http_client, RateBudget(DEXSCREENER_REQUESTS_PER_MINUTE)),
        )
    else:
        log.warning("decision-engine.gate_disabled", reason="SOLANA_RPC_URL not set; pump.fun candidates are not evaluated")

    try:
        await asyncio.gather(
            _evaluation_loop(session_factory, redis, settings, stop_event, sources),
            _manual_loop(session_factory, redis, settings, stop_event, sources),
            heartbeat_loop(settings, "decision-engine", stop_event, lambda: {"venues": venue_health_snapshot()}),
            run_watcher("decision-engine", settings, session_factory, stop_event, rpc=sources.rpc if sources else None,
                        redis=redis),
            # X narrative (SHADOW, off unless X_NARRATIVE_ENABLED + dashboard switch + bearer token): looks up queued
            # candidates in the background; evaluation never waits for it.
            x_narrative.run_forever(session_factory, redis, settings, stop_event),
        )
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
