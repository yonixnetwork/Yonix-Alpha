"""The LIVE order worker and wallet reconciliation.

Runs inside paper-trading (the service that owns positions). Every second it
executes PENDING LIVE orders (build → guard → sign → simulate → send →
confirm → apply the actual fill); every RECONCILE_SECONDS, and once at
start-up before any order is touched, it reconciles the database with the
wallet. It publishes its readiness to Redis; decision-engine refuses to
create LIVE orders unless that readiness is fresh and "ready".

With any lock closed the worker does nothing but report "disabled" and
cancel LIVE orders that should not exist.
"""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select

from yonixalpha_core import live_trading
from yonixalpha_core.db.models import ExecutionOrder
from yonixalpha_core.live_trading import LIVE_ACCOUNT, READY_KEY
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.store import live_trading_permitted
from yonixalpha_core.solana.live_exec import ExecOutcome, SolanaLiveExecutor
from yonixalpha_core.solana.pumpportal import PumpPortalClient
from yonixalpha_core.solana.wallet import WalletError, load_wallet

log = get_logger("paper-trading.live")

POLL_SECONDS = 1.0
RECONCILE_SECONDS = 30
READY_TTL_SECONDS = 60


async def _publish(redis, status: str, reason: str | None = None, **extra) -> None:
    await redis.set(READY_KEY, json.dumps({"status": status, "reason": reason, "at": datetime.now(timezone.utc).isoformat(),
                                           **{k: str(v) for k, v in extra.items()}}), ex=READY_TTL_SECONDS)


async def cancel_live_orders(session_factory, redis, app_settings, reason: str) -> int:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        orders = (await session.execute(select(ExecutionOrder).where(ExecutionOrder.mode == "LIVE",
                                                                    ExecutionOrder.status == "PENDING"))).scalars().all()
        for order in orders:
            await live_trading.apply_outcome(session, redis, app_settings, order, ExecOutcome("FAILED", error=reason), now)
            order.status = "CANCELLED"
        await session.commit()
    return len(orders)


def build_executor(app_settings, rpc, http_client) -> tuple[SolanaLiveExecutor | None, str | None]:
    if not live_trading_permitted(app_settings):
        return None, "environment locks closed"
    if rpc is None:
        return None, "SOLANA_RPC_URL not configured"
    try:
        wallet = load_wallet(app_settings)
    except WalletError as exc:
        return None, str(exc)
    if wallet is None:
        return None, "WALLET_PRIVATE_KEY not configured"
    return SolanaLiveExecutor(rpc, PumpPortalClient(http_client), wallet), None


async def live_worker_loop(session_factory, redis, app_settings, rpc, http_client, stop_event: asyncio.Event,
                           executor: SolanaLiveExecutor | None = None) -> None:
    if executor is None:
        executor, reason = build_executor(app_settings, rpc, http_client)
    else:
        reason = None
    last_reconcile = None
    reconciled_once = False
    while not stop_event.is_set():
        try:
            if executor is None or not live_trading_permitted(app_settings):
                await _publish(redis, "disabled", reason or "environment locks closed")
                n = await cancel_live_orders(session_factory, redis, app_settings, f"live execution unavailable: {reason}")
                if n:
                    log.warning("live.orders_cancelled", count=n, reason=reason)
            else:
                async with session_factory() as session:
                    live = await live_trading.load_live_settings(session)
                now = datetime.now(timezone.utc)
                if last_reconcile is None or (now - last_reconcile).total_seconds() >= RECONCILE_SECONDS:
                    report = await live_trading.reconcile(session_factory, redis, app_settings, executor, now)
                    last_reconcile, reconciled_once = now, True
                    log.info("live.reconciled", **{k: str(v) for k, v in report.items()})
                if reconciled_once:
                    await _publish(redis, "ready", wallet=executor.wallet.pubkey, min_sol_reserve=live.min_sol_reserve,
                                   wallet_max_age_seconds=live.wallet_max_age_seconds, account=LIVE_ACCOUNT)
                    async with session_factory() as session:
                        ids = (await session.execute(select(ExecutionOrder.id).where(
                            ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "PENDING")
                            .order_by(ExecutionOrder.side.desc(), ExecutionOrder.created_at))).scalars().all()  # SELLs first
                    for order_id in ids:
                        status = await live_trading.process_order(session_factory, redis, app_settings, executor, order_id)
                        log.info("live.order_processed", order_id=str(order_id), status=status)
        except Exception as exc:  # noqa: BLE001 - the worker must keep running; the error is reported
            log.error("live.worker_failed", error=f"{type(exc).__name__}: {exc}")
            await _publish(redis, "error", f"{type(exc).__name__}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
