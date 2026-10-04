import asyncio
import os
import signal

from yonixalpha_core.config import get_settings
from yonixalpha_core.events import heartbeat_loop
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import SystemEvent
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.logging import configure_logging, get_logger
from yonixalpha_core.notify import send_telegram_alert
from yonixalpha_core import update_monitor
from yonixalpha_core.ml import steps

from app.ablation import run_ablation
from app.evm_ml import run_evm_cycle
from app.gate_ml import run_cycle
from app.shadow_ml import run_shadow_cycle
from app.train import train_and_maybe_register

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


async def _training_loop(session_factory, redis, settings, stop_event: asyncio.Event) -> None:
    """Solana training, gate models, shadow models and the ablation, hourly.
    Each step is timed in Redis (yonixalpha_core.ml.steps) for ML Review."""
    while not stop_event.is_set():
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

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TRAIN_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _evm_loop(session_factory, redis, stop_event: asyncio.Event) -> None:
    """EVM opportunities and wallet behaviour (M12): samples, labels and
    SHADOW models; review only, never read by an entry or exit. Its own
    loop, so a slow Solana step can never hold it up."""
    while not stop_event.is_set():
        try:
            evm = await steps.timed(redis, "evm_wallet_ml", lambda: run_evm_cycle(session_factory), log)
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
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await _record_system_event(session_factory, "service_started", "info")
    log.info("ml.started")

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
