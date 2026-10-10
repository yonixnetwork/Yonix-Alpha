import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core import system_profile, update_monitor
from yonixalpha_core.ml import frozen, steps

from app.ablation import run_ablation
from app.entry_ml import run_entry_cycle
from app.evm_ml import run_evm_cycle
from app.gate_ml import run_cycle
from app.shadow_ml import run_shadow_cycle
from app.train import train_and_maybe_register
from app.validation import run_validation

log = get_logger("ml.main")

# Training is infrequent by nature (a labeled dataset changes on the order
# of closed positions, not seconds) — an hourly check is plenty, and cheap
# to run even while it's doing nothing but recording
# training_skipped_insufficient_samples, which per docs/ML.md is the
# expected outcome for a long time.
TRAIN_INTERVAL_SECONDS = 3600
EVM_INTERVAL_SECONDS = 1800  # each pass drains the EVM sample / wallet-label backlog (bounded per builder)
SERVICE_NAME = "ml"
# GitHub / PyPI update monitor (master §64-66): notifies, never deploys.
# UPDATE_MONITOR=0 switches it off.
UPDATE_MONITOR = os.getenv("UPDATE_MONITOR", "1") != "0"


async def _record_system_event(session_factory, event_type: str, severity: str, detail: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(SystemEvent(service=SERVICE_NAME, event_type=event_type, severity=severity, detail=detail))
        await session.commit()
    if severity in ("error", "critical"):
        await send_telegram_alert(
            get_settings(), f"[{SERVICE_NAME}] {severity.upper()}: {event_type}" + (f"\n{detail}" if detail else "")
        )


TRAINING_STEPS = ("solana_training", "gate_models", "solana_shadow", "ablation", "frozen_validation", "entry_timing")


async def _decide(session_factory, redis, settings, anchor_step: str, normal_s: int) -> tuple[bool, str, dict]:
    """Whether the background training steps may run now
    (yonixalpha_core.operating_mode.training_decision): NORMAL mode on their
    usual interval; LOW_RESOURCE once per ML_TRAINING_INTERVAL_LOW_RESOURCE_H
    and never while the host is CRITICAL; EMERGENCY never. The last run is
    read from the step record, so a restart does not start a run early.
    Inference (the decision engine) is never affected."""
    from yonixalpha_core import operating_mode, resources

    async with session_factory() as session:
        st = await operating_mode.load(session, settings)
    lvl, why = resources.level(resources.sample(), settings)
    rec = await steps.read_one(redis, anchor_step) or {}
    last = datetime.fromisoformat(rec["started_at"]) if rec.get("started_at") else None
    ok, reason = operating_mode.training_decision(st["resource_mode"], lvl, last, datetime.now(timezone.utc), settings,
                                                  timedelta(seconds=normal_s))
    if not ok and lvl == resources.CRITICAL and why:
        reason += f" ({'; '.join(why)})"
    return ok, reason, st


RETRY_SECONDS = 900  # skipped for resources (CRITICAL) or an unreadable state: checked again after this


async def _next_check_s(redis, anchor_step: str, interval_s: int, now: datetime | None = None) -> float:
    """Seconds until a skipped training run is due again: when it waits for its
    interval, until the interval ends (at least a minute); otherwise (resource
    pressure) RETRY_SECONDS; never more than the interval. 2026-10-08: it slept
    a full hour after every skip, so a restart just before the hour was up
    pushed training back nearly two hours (last run 21:44, checked 22:27, next
    check 23:27)."""
    now = now or datetime.now(timezone.utc)
    rec = await steps.read_one(redis, anchor_step) or {}
    try:
        due = (datetime.fromisoformat(rec["started_at"]) + timedelta(seconds=interval_s) - now).total_seconds()
    except (KeyError, TypeError, ValueError):
        return float(min(RETRY_SECONDS, interval_s))
    return float(min(interval_s, max(60.0, due) if due > 0 else RETRY_SECONDS))


async def _training_loop(session_factory, redis, settings, stop_event: asyncio.Event) -> None:
    """Solana training, gate models, shadow models, the ablation and the
    frozen-set validation: hourly in NORMAL mode, on the low-resource
    schedule otherwise (_decide); a skipped run is recorded with its reason.
    Each step is timed in Redis (yonixalpha_core.ml.steps) for ML Review."""
    while not stop_event.is_set():
        try:
            ok, reason, _ = await _decide(session_factory, redis, settings, "solana_training", TRAIN_INTERVAL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - no reading: background work waits
            ok, reason = False, f"SKIPPED - mode / resources unreadable: {type(exc).__name__}"
        if not ok:
            for name in TRAINING_STEPS:
                await steps.skipped(redis, name, reason)
            wait = await _next_check_s(redis, "solana_training", TRAIN_INTERVAL_SECONDS)
            log.info("training_loop.skipped", reason=reason, next_check_s=round(wait))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            async def train():
                async with session_factory() as session:
                    return await train_and_maybe_register(session)
            outcome = await steps.timed(redis, "solana_training", train, log)
            log.info("training_loop.completed", status=outcome.status, available_samples=outcome.available_samples)
            await _record_system_event(
                session_factory,
                f"training_{outcome.status}",
                "info",
                {"available_samples": outcome.available_samples, "metrics": outcome.metrics},
            )
        except Exception as exc:  # noqa: BLE001
            log.error("training_loop.failed", error=str(exc))
            await _record_system_event(session_factory, "training_loop_failed", "error", {"error": str(exc)})
        # Safety-gate models: quality check, challenger training, drift.
        # Challengers are only ever registered; promotion is an operator action.
        try:
            cycle = await steps.timed(redis, "gate_models", lambda: run_cycle(session_factory, redis, settings), log)
            log.info("gate_ml.completed", result=cycle)
            await _record_system_event(session_factory, "gate_ml_cycle", "info", cycle)
        except Exception as exc:  # noqa: BLE001
            log.error("gate_ml.failed", error=str(exc))
            await _record_system_event(session_factory, "gate_ml_cycle_failed", "error", {"error": str(exc)})

        # Multi-target SHADOW models on the opportunity ledger: review only,
        # never loaded by the decision engine (app.shadow_ml).
        try:
            shadow = await steps.timed(redis, "solana_shadow", lambda: run_shadow_cycle(session_factory), log)
            log.info("shadow_ml.completed", result=shadow)
            await _record_system_event(session_factory, "shadow_ml_cycle", "info", shadow)
        except Exception as exc:  # noqa: BLE001
            log.error("shadow_ml.failed", error=str(exc))
            await _record_system_event(session_factory, "shadow_ml_cycle_failed", "error", {"error": str(exc)})

        # Do the scanner-intelligence features help out of sample? (every 6 h;
        # results stored for the ML Review page, never used for decisions)
        try:
            abl = await steps.timed(redis, "ablation", lambda: run_ablation(session_factory, redis), log)
            log.info("ablation.completed", status=abl.get("status"), samples=abl.get("samples"))
            if abl.get("status") == "EVALUATED":
                await _record_system_event(session_factory, "feature_ablation", "info",
                                           {"samples": abl.get("samples"), "split": abl.get("split")})
        except Exception as exc:  # noqa: BLE001
            log.error("ablation.failed", error=str(exc))
            await _record_system_event(session_factory, "feature_ablation_failed", "error", {"error": str(exc)})

        # Frozen validation sets (master §38): freeze the due windows, score each
        # model on the sets it never saw. A PASS is what an operator needs before
        # raising a contribution (ml.governance); nothing is raised from here.
        try:
            fams = frozen.FAMILIES if system_profile.evm_ml_enabled(get_settings()) else frozen.SOLANA_FAMILIES
            val = await steps.timed(redis, "frozen_validation", lambda: run_validation(session_factory, families=fams), log)
            log.info("validation.completed", frozen=val.get("frozen"), evaluated=val.get("evaluated"))
            if val.get("frozen") or val.get("evaluated"):
                await _record_system_event(session_factory, "frozen_validation", "info",
                                           {k: val.get(k) for k in ("frozen", "evaluated", "skipped_seen", "budget_spent")})
        except Exception as exc:  # noqa: BLE001
            log.error("validation.failed", error=str(exc))
            await _record_system_event(session_factory, "frozen_validation_failed", "error", {"error": str(exc)[:500]})

        # Entry-timing model on the early-entry signals (entry_ml): SHADOW only,
        # scored on the frozen test period; never activated.
        try:
            async def entry():
                async with session_factory() as session:
                    return await run_entry_cycle(session)
            ent = await steps.timed(redis, "entry_timing", entry, log)
            log.info("entry_ml.completed", result=ent)
        except Exception as exc:  # noqa: BLE001
            log.error("entry_ml.failed", error=str(exc))
            await _record_system_event(session_factory, "entry_ml_failed", "error", {"error": str(exc)[:500]})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TRAIN_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _evm_loop(session_factory, redis, stop_event: asyncio.Event) -> None:
    """EVM opportunities and wallet behaviour (M12): samples, labels and
    SHADOW models; review only, never read by an entry or exit. Its own
    loop, so a slow Solana step can never hold it up."""
    while not stop_event.is_set():
        # System profile (yonixalpha_core.system_profile): SOLANA_ONLY trains
        # no BSC / Robinhood model; memecoin ML on Solana is not affected.
        off = system_profile.disabled_reason(get_settings(), "evm_ml")
        try:
            ok, reason, st = (False, f"SKIPPED - {off}", {}) if off else \
                await _decide(session_factory, redis, get_settings(), "evm_wallet_ml", EVM_INTERVAL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            ok, reason, st = False, f"SKIPPED - mode / resources unreadable: {type(exc).__name__}", {}
        if not ok:
            await steps.skipped(redis, "evm_wallet_ml", reason)
            log.info("evm_ml.skipped", reason=reason)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=EVM_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        wallet = st.get("copy_trading_effective") != "SUSPENDED"  # wallet analytics belong to copy trading
        try:
            evm = await steps.timed(redis, "evm_wallet_ml", lambda: run_evm_cycle(session_factory, wallet=wallet), log)
            log.info("evm_ml.completed", result=evm)
            await _record_system_event(session_factory, "evm_ml_cycle", "info", evm)
        except Exception as exc:  # noqa: BLE001
            log.error("evm_ml.failed", error=str(exc))
            await _record_system_event(session_factory, "evm_ml_cycle_failed", "error", {"error": str(exc)[:500]})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EVM_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    stopping: list = []

    def _stop() -> None:  # a deploy stops the container: running steps are STOPPED, not "interrupted"
        stop_event.set()
        stopping.append(loop.create_task(steps.mark_stopped(redis)))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)

    await _record_system_event(session_factory, "service_started", "info")
    log.info("ml.started")
    interrupted = await steps.mark_interrupted(redis)
    if interrupted:  # the previous process died mid-step: alerted, never silent
        log.error("ml.steps_interrupted", steps=interrupted)
        await _record_system_event(session_factory, "ml_step_interrupted", "error", {"steps": interrupted})

    try:
        tasks = [_training_loop(session_factory, redis, settings, stop_event), _evm_loop(session_factory, redis, stop_event),
                 heartbeat_loop(settings, "ml", stop_event)]
        if UPDATE_MONITOR:
            tasks.append(update_monitor.run(session_factory, redis, settings, stop_event))
        await asyncio.gather(*tasks)
    finally:
        await _record_system_event(session_factory, "service_stopped", "info")
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
