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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core import kill_switch, live_control, live_trading
from yonixalpha_core.db.models import ExecutionOrder
from yonixalpha_core.live_trading import LIVE_ACCOUNT, READY_KEY
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode
from yonixalpha_core.safety.store import live_trading_permitted
from yonixalpha_core.solana.live_exec import ExecOutcome, SolanaLiveExecutor
from yonixalpha_core.solana.pumpportal import PumpPortalClient
from yonixalpha_core.solana.wallet import WalletError, load_wallet
from yonixalpha_core.notify import alert_error

log = get_logger("paper-trading.live")

POLL_SECONDS = 1.0
PENDING_CHECK_SECONDS = 0.2  # between loops, a new PENDING order is picked up within this
RECONCILE_SECONDS = 30
# A failing wallet read (e.g. the RPC answering HTTP 429) is retried 2, 4, 8 .. 60 s
# apart instead of every loop, so the worker does not keep a rate limit hit.
RECONCILE_RETRY_MAX_SECONDS = 60
READY_TTL_SECONDS = 60


def reconcile_backoff(failures: int) -> float:
    return float(min(RECONCILE_RETRY_MAX_SECONDS, 2 ** max(1, failures)))


async def _publish(redis, status: str, reason: str | None = None, **extra) -> None:
    await redis.set(READY_KEY, json.dumps({"status": status, "reason": reason, "at": datetime.now(timezone.utc).isoformat(),
                                           **{k: str(v) for k, v in extra.items()}}), ex=READY_TTL_SECONDS)


async def cancel_live_orders(session_factory, redis, app_settings, reason: str, side: str | None = None,
                             keep_smoke: bool = False) -> int:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        q = select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == live_trading.LIVE_PROVIDER,
            ExecutionOrder.status == "PENDING")
        if side is not None:
            q = q.where(ExecutionOrder.side == side)
        orders = [o for o in (await session.execute(q)).scalars().all()
                  if not (keep_smoke and live_trading.is_smoke_order(o))]
        for order in orders:
            await live_trading.apply_outcome(session, redis, app_settings, order, ExecOutcome("FAILED", error=reason), now)
            order.status = "CANCELLED"
        await session.commit()
    return len(orders)


def _pending_query(handled: set):
    q = select(ExecutionOrder.id).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == live_trading.LIVE_PROVIDER,
        ExecutionOrder.status == "PENDING")
    if handled:
        q = q.where(ExecutionOrder.id.not_in(list(handled)))
    return q.limit(1)


async def wait_for_work(session_factory, stop_event: asyncio.Event, handled: set | None = None,
                        timeout: float = POLL_SECONDS, check: float = PENDING_CHECK_SECONDS) -> None:
    """Waits up to `timeout` seconds, returning as soon as a NEW PENDING LIVE
    order exists (one the last loop did not already handle, e.g. a sent order
    still awaiting confirmation) or stop is requested. One indexed query per
    `check` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not stop_event.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(check, remaining))
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with session_factory() as session:
                if (await session.execute(_pending_query(handled or set()))).first() is not None:
                    return
        except Exception:  # noqa: BLE001 - fall back to the plain interval
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            return


async def _genesis_hash(rpc) -> str | None:
    """The network the RPC serves (live_control checks it is mainnet). None
    when unknown: the network check then fails, never passes."""
    if rpc is None:
        return None
    try:
        h = await rpc.call("getGenesisHash", [])
        return h if isinstance(h, str) else None
    except Exception as exc:  # noqa: BLE001 - retried on the next loop
        log.warning("live.genesis_hash_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return None


def build_executor(app_settings, rpc, http_client, jupiter=None) -> tuple[SolanaLiveExecutor | None, str | None]:
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
    return SolanaLiveExecutor(rpc, PumpPortalClient(http_client), wallet, jupiter=jupiter), None


async def live_worker_loop(session_factory, redis, app_settings, rpc, http_client, stop_event: asyncio.Event,
                           executor: SolanaLiveExecutor | None = None, jupiter=None) -> None:
    if executor is None:
        executor, reason = build_executor(app_settings, rpc, http_client, jupiter)
    else:
        reason = None
    last_reconcile = None
    reconciled_once = False
    genesis = None
    genesis_tried = None
    last_control = None
    reconcile_failures, reconcile_retry_at, last_error = 0, None, None
    while not stop_event.is_set():
        handled: set = set()
        try:
            if executor is None or not live_trading_permitted(app_settings):
                await _publish(redis, "disabled", reason or "environment locks closed")
                n = await cancel_live_orders(session_factory, redis, app_settings, f"live execution unavailable: {reason}")
                if n:
                    log.warning("live.orders_cancelled", count=n, reason=reason)
            else:
                async with session_factory() as session:
                    live = await live_trading.load_live_settings(session)
                if hasattr(executor, "builder"):
                    executor.builder = live.tx_builder  # dashboard setting, applied per loop
                if hasattr(executor, "native"):
                    executor.native.cu_limits = {"PUMP_BONDING_CURVE": live.compute_unit_limit_curve,
                                                 "PUMP_AMM": live.compute_unit_limit_amm}
                now = datetime.now(timezone.utc)
                # Reconcile first only at start-up; after that, pending orders go
                # first and the periodic reconcile runs after them.
                if not reconciled_once:
                    if reconcile_retry_at is not None and now < reconcile_retry_at:
                        await _publish(redis, "error", last_error)  # still not ready; the next read is backed off
                    else:
                        try:
                            report = await live_trading.reconcile(session_factory, redis, app_settings, executor, now)
                        except Exception:
                            reconcile_failures += 1
                            reconcile_retry_at = now + timedelta(seconds=reconcile_backoff(reconcile_failures))
                            raise
                        reconcile_failures, reconcile_retry_at = 0, None
                        last_reconcile, reconciled_once = now, True
                        log.info("live.reconciled", **{k: str(v) for k, v in report.items()})
                if reconciled_once:
                    if genesis is None and (genesis_tried is None or (now - genesis_tried).total_seconds() >= 60):
                        genesis_tried = now
                        genesis = await _genesis_hash(getattr(executor, "rpc", None) or rpc)
                    await _publish(redis, "ready", wallet=executor.wallet.pubkey, min_sol_reserve=live.min_sol_reserve,
                                   wallet_max_age_seconds=live.wallet_max_age_seconds, account=LIVE_ACCOUNT,
                                   **({"genesis_hash": genesis} if genesis else {}))
                    # The mandatory live checks (live_control), re-run every CONTROL_SECONDS: a failing or
                    # stale result stops every new LIVE order (live_trading.live_readiness, process_order).
                    if last_control is None or (now - last_control).total_seconds() >= live_control.CONTROL_SECONDS:
                        async with session_factory() as session:
                            rec = await live_control.publish_enforcement(session, redis, app_settings, now)
                        last_control = now
                        if not rec["ok"]:
                            log.warning("live.checks_failing", failed=rec["failed"])
                    # Kill switch: a BUY queued just before it was engaged is not
                    # sent; SELLs (exits) keep running.
                    killed = await kill_switch.is_engaged(redis)
                    if killed and (n := await cancel_live_orders(session_factory, redis, app_settings,
                                                                 "kill switch engaged", side="BUY")):
                        log.warning("live.buys_cancelled_kill_switch", count=n)
                    # LIVE disarmed in the dashboard, or a mandatory check failing: queued BUYs are
                    # cancelled at once (the smoke test, armed separately, runs with PAPER on purpose).
                    async with session_factory() as session:
                        disarmed = await store.load_global_mode(session) != GlobalMode.LIVE
                    checks_ok, why = await live_control.enforcement_ok(redis)
                    if not checks_ok and (n := await cancel_live_orders(
                            session_factory, redis, app_settings, f"live check failed: {why}", side="BUY")):
                        log.warning("live.buys_cancelled_checks", count=n, reason=why)
                    if disarmed and (n := await cancel_live_orders(
                            session_factory, redis, app_settings, "LIVE disarmed: the global mode is not LIVE",
                            side="BUY", keep_smoke=True)):
                        log.warning("live.buys_cancelled_disarmed", count=n)
                    async with session_factory() as session:
                        q = select(ExecutionOrder.id).where(
                            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == live_trading.LIVE_PROVIDER,
                            ExecutionOrder.status == "PENDING")
                        if killed:
                            q = q.where(ExecutionOrder.side != "BUY")
                        ids = (await session.execute(
                            q.order_by(ExecutionOrder.side.desc(), ExecutionOrder.created_at))).scalars().all()  # SELLs first
                    handled.update(ids)
                    for order_id in ids:
                        status = await live_trading.process_order(session_factory, redis, app_settings, executor, order_id)
                        log.info("live.order_processed", order_id=str(order_id), status=status)
                now = datetime.now(timezone.utc)
                if last_reconcile is not None and (now - last_reconcile).total_seconds() >= RECONCILE_SECONDS \
                        and (reconcile_retry_at is None or now >= reconcile_retry_at):
                    try:
                        report = await live_trading.reconcile(session_factory, redis, app_settings, executor, now)
                    except Exception:
                        reconcile_failures += 1
                        reconcile_retry_at = now + timedelta(seconds=reconcile_backoff(reconcile_failures))
                        raise
                    reconcile_failures, reconcile_retry_at = 0, None
                    last_reconcile = now
                    log.info("live.reconciled", **{k: str(v) for k, v in report.items()})
        except Exception as exc:  # noqa: BLE001 - the worker must keep running; the error is reported
            last_error = type(exc).__name__
            log.error("live.worker_failed", error=f"{type(exc).__name__}: {exc}")
            await alert_error("paper-trading", "live.worker_failed", {"error": f"{type(exc).__name__}: {exc}"})
            await _publish(redis, "error", f"{type(exc).__name__}")
        if executor is not None and live_trading_permitted(app_settings):
            await wait_for_work(session_factory, stop_event, handled)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
