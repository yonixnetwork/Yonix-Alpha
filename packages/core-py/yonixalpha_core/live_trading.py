"""Live execution for Pump.fun tokens: the same decisions as paper, with fills
from confirmed on-chain transactions.

Paper and live share everything up to the fill: the safety gate decides the
entry, `paper_engine.manage_step` decides every exit (stop, TPs, trailing,
operator exit, exit intelligence), and `paper_engine.close_position` books
the outcome and ML labels. What differs is the provider:
- PAPER: `paper_engine` simulates the fill against the curve/pool model;
- LIVE: an `ExecutionOrder` is written, the order worker builds the
  transaction with PumpPortal's local API, guards, signs, simulates, sends
  and confirms it (`solana.live_exec`), and only the confirmed balance
  changes of our wallet move the position.

Safety:
- a LIVE order is only created when every lock is open (TRADING_ENABLED,
  LIVE_TRADING_ENABLED, PAPER_TRADING=false), the global mode is LIVE, the
  strategy is AUTO or MANUAL, and the readiness check passed;
- idempotency keys make a repeated decision unable to buy twice; one open
  or pending live position per mint; one pending order per position;
- the signature is persisted before sending; reconciliation resolves any
  order left SIGNED/SUBMITTED by a crash;
- the wallet (not the database) is the source of truth for balances.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, paper_engine
from yonixalpha_core.db.models import ExecutionOrder, PaperAccount, PaperPosition, PlatformSetting, ReconciliationEvent, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.store import add_timeline_event, live_trading_permitted
from yonixalpha_core.solana.live_exec import ExecOutcome, SolanaLiveExecutor, wallet_balances
from yonixalpha_core.solana.pumpportal import TradeRequest
from yonixalpha_core.solana.txguard import GuardExpectation
from yonixalpha_core.state_machine import CandidateState, apply_transition

log = get_logger("core.live_trading")

LAMPORTS = Decimal(1_000_000_000)
LIVE_ACCOUNT = "live_solana"
LIVE_PROVIDER = "pumpportal_local"
PAPER_PROVIDER = "paper_simulator"
SETTINGS_KEY = "live_execution"
WALLET_KEY = "yx:live:wallet"
READY_KEY = "yx:live:executor"
DUST_RAW = 1
SIGNED_RECHECK_SECONDS = 20
ORDER_EXPIRY_SECONDS = 150
STALE_PENDING_SECONDS = 300


@dataclass
class LiveExecutionSettings:
    """Runtime execution parameters (dashboard, stored in platform_settings);
    secrets never live here."""

    entry_slippage_pct: Decimal = Decimal("10")
    exit_slippage_pct: Decimal = Decimal("25")
    exit_slippage_step_pct: Decimal = Decimal("10")  # added per failed exit attempt
    max_exit_slippage_pct: Decimal = Decimal("60")
    priority_fee_sol: Decimal = Decimal("0.0001")
    max_priority_fee_sol: Decimal = Decimal("0.002")
    max_platform_fee_bps: int = 100  # PumpPortal documents 0.5%; bounded at 1%
    min_sol_reserve: Decimal = Decimal("0.05")  # never spent: rent + fees for exits
    wallet_max_age_seconds: int = 120

    def to_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


LIMITS = {"entry_slippage_pct": (Decimal("0.5"), Decimal("50")), "exit_slippage_pct": (Decimal("1"), Decimal("90")),
          "exit_slippage_step_pct": (Decimal("0"), Decimal("30")), "max_exit_slippage_pct": (Decimal("5"), Decimal("95")),
          "priority_fee_sol": (Decimal("0"), Decimal("0.01")), "max_priority_fee_sol": (Decimal("0"), Decimal("0.02")),
          "max_platform_fee_bps": (Decimal("0"), Decimal("200")), "min_sol_reserve": (Decimal("0.01"), Decimal("100")),
          "wallet_max_age_seconds": (Decimal("30"), Decimal("900"))}


def parse_live_settings(data: dict[str, Any]) -> tuple[LiveExecutionSettings, list[str]]:
    base = LiveExecutionSettings()
    errors: list[str] = []
    for key, value in (data or {}).items():
        if key not in LIMITS:
            errors.append(f"{key}: unknown setting")
            continue
        try:
            d = Decimal(str(value))
        except Exception:  # noqa: BLE001
            errors.append(f"{key}: must be a number")
            continue
        lo, hi = LIMITS[key]
        if not d.is_finite() or d < lo or d > hi:
            errors.append(f"{key}: must be between {lo} and {hi}")
            continue
        setattr(base, key, int(d) if isinstance(getattr(base, key), int) else d)
    if base.priority_fee_sol > base.max_priority_fee_sol:
        errors.append("priority_fee_sol cannot exceed max_priority_fee_sol")
    if base.exit_slippage_pct > base.max_exit_slippage_pct:
        errors.append("exit_slippage_pct cannot exceed max_exit_slippage_pct")
    return base, errors


async def load_live_settings(session: AsyncSession) -> LiveExecutionSettings:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_live_settings((row.value if row else None) or {})
    return s if not errors else LiveExecutionSettings()


async def get_live_account(session: AsyncSession) -> PaperAccount:
    acct = (await session.execute(select(PaperAccount).where(PaperAccount.name == LIVE_ACCOUNT))).scalar_one_or_none()
    if acct is None:
        # Balance is unknown until the first wallet sync; the gate treats a
        # zero balance as no capacity, never as a number to trade against.
        acct = PaperAccount(name=LIVE_ACCOUNT, quote_currency="SOL", starting_balance=Decimal("1e-9"), cash_balance=Decimal(0),
                            reset_at=datetime.now(timezone.utc))
        session.add(acct)
        await session.flush()
    return acct


async def live_readiness(redis: Redis | None, app_settings: Any, now: datetime | None = None) -> tuple[bool, str | None]:
    """Everything that must hold before a LIVE order may be created."""
    import json

    if not live_trading_permitted(app_settings):
        return False, "environment locks closed (TRADING_ENABLED, LIVE_TRADING_ENABLED, PAPER_TRADING=false required)"
    if redis is None:
        return False, "no Redis connection"
    now = now or datetime.now(timezone.utc)
    ex = await redis.get(READY_KEY)
    if not ex:
        return False, "live order worker not running (paper-trading heartbeat missing)"
    state = json.loads(ex)
    if state.get("status") != "ready":
        return False, f"live order worker not ready: {state.get('reason')}"
    raw = await redis.get(WALLET_KEY)
    if not raw:
        return False, "wallet balance not synced yet"
    w = json.loads(raw)
    age = (now - datetime.fromisoformat(w["at"])).total_seconds()
    if age > float(state.get("wallet_max_age_seconds", 120)):
        return False, f"wallet balance is {age:.0f}s old"
    if Decimal(w["sol"]) <= Decimal(state.get("min_sol_reserve", "0.05")):
        return False, f"wallet balance {w['sol']} SOL is at or below the reserve"
    return True, None


def _route(lifecycle: str) -> str:
    return "pump-amm" if lifecycle == "MIGRATED" else "pump"


async def enter_live(session: AsyncSession, redis: Redis | None, account: PaperAccount, assessment, assessment_id,
                     candidate: TradingCandidate | None, now: datetime, lifecycle: str, decimals: int | None,
                     provenance: dict[str, Any]) -> PaperPosition:
    """Creates the pending LIVE position and its BUY order for an executable
    LIVE assessment. Raises ValueError when anything forbids it."""
    plan = assessment.plan
    if not assessment.executable or assessment.execution_target.value != "LIVE" or not plan.complete:
        raise ValueError("assessment is not an executable LIVE plan")
    if plan.side != "LONG":
        raise ValueError("live Solana execution is spot-long only")
    if decimals is None:
        raise ValueError("token decimals unknown")
    live = await load_live_settings(session)
    size = plan.position_size.value
    if size > account.cash_balance - live.min_sol_reserve:
        raise ValueError(f"size {size} SOL exceeds wallet balance {account.cash_balance} minus reserve {live.min_sol_reserve}")
    busy = (await session.execute(select(PaperPosition.id).where(
        PaperPosition.execution_mode == "LIVE", PaperPosition.asset_id == assessment.asset_id,
        PaperPosition.status.in_(("pending_entry", "open", "needs_review"))))).first()
    if busy is not None:
        raise ValueError("a live position for this mint already exists")

    position = PaperPosition(
        candidate_id=candidate.id if candidate else None, symbol=assessment.symbol[:64], provider="live", side="LONG",
        entry_price=plan.entry_price, quantity=Decimal(0), stop_loss=plan.stop_loss.value,
        take_profit=[str(tp.price.value) for tp in plan.take_profits], entry_at=now, status="pending_entry",
        account_id=account.id, assessment_id=assessment_id, engine=assessment.engine, asset_id=assessment.asset_id,
        initial_quantity=Decimal(0), remaining_quantity=Decimal(0), entry_cost_quote=Decimal(0), proceeds_quote=Decimal(0),
        fees_paid_quote=Decimal(0), max_loss_quote=plan.max_loss.value,
        plan={**plan.to_dict(), "venue": {"type": "pumpswap_pool" if lifecycle == "MIGRATED" else "pump_curve", "kind": "spot",
                                         "decimals": decimals, **(provenance.get("venue") or {})}},
        tp_hits=[], highest_price=plan.entry_price, lowest_price=plan.entry_price, last_price=plan.entry_price, last_marked_at=now,
        execution_mode="LIVE", source="PUMPFUN", lifecycle=lifecycle, execution_provider=LIVE_PROVIDER,
        execution_route=_route(lifecycle), pool=provenance.get("pool"), strategy=assessment.strategy,
        model_version=provenance.get("model_version"), feature_version=provenance.get("feature_version"),
    )
    session.add(position)
    await session.flush()
    max_in = int((size * (1 + live.entry_slippage_pct / 100) * LAMPORTS).to_integral_value(ROUND_DOWN))
    order = ExecutionOrder(
        position_id=position.id, assessment_id=assessment_id, mode="LIVE", side="BUY", reason="entry", mint=assessment.asset_id,
        provider=LIVE_PROVIDER, route=_route(lifecycle), amount=str(size), amount_kind="sol",
        slippage_pct=live.entry_slippage_pct, priority_fee_sol=live.priority_fee_sol,
        limits={"max_sol_in_lamports": max_in,
                "max_fee_transfer_lamports": int(size * LAMPORTS * live.max_platform_fee_bps / 10_000),
                "max_priority_fee_lamports": int(live.max_priority_fee_sol * LAMPORTS)},
        status="PENDING", idempotency_key=f"entry:{assessment_id}",
    )
    session.add(order)
    await session.flush()
    position.pending_order_id = order.id
    if candidate is not None:
        apply_transition(candidate, CandidateState.QUALIFIED, reason="safety gate: " + assessment.status_label)
        apply_transition(candidate, CandidateState.ENTRY_PENDING, reason=f"live buy {size} SOL submitted to order worker")
    await add_timeline_event(session, "live_entry_requested", now,
                             {"size_sol": str(size), "route": order.route, "max_sol_in_lamports": max_in},
                             candidate_id=position.candidate_id, assessment_id=assessment_id, position_id=position.id)
    return position


async def request_live_exit(session: AsyncSession, position: PaperPosition, quantity: Decimal, reason: str,
                            expected_sol_out: Decimal | None, now: datetime) -> ExecutionOrder | None:
    """Queues a SELL for `quantity` (whole tokens) unless one is pending."""
    if position.pending_order_id is not None or position.status != "open":
        return None
    decimals = int(((position.plan or {}).get("venue") or {}).get("decimals"))
    raw = int((quantity * Decimal(10) ** decimals).to_integral_value(ROUND_DOWN))
    remaining_raw = int(((position.remaining_quantity or Decimal(0)) * Decimal(10) ** decimals).to_integral_value(ROUND_DOWN))
    raw = min(raw, remaining_raw)
    if raw <= 0:
        return None
    live = await load_live_settings(session)
    slip = min(live.exit_slippage_pct + live.exit_slippage_step_pct * position.exit_failures, live.max_exit_slippage_pct)
    min_out = int((expected_sol_out * (1 - slip / 100) * LAMPORTS).to_integral_value(ROUND_DOWN)) if expected_sol_out else 0
    seq = len(position.tp_hits or []) + position.exit_failures
    order = ExecutionOrder(
        position_id=position.id, assessment_id=position.assessment_id, mode="LIVE", side="SELL", reason=reason[:32],
        mint=position.asset_id, provider=LIVE_PROVIDER, route=position.execution_route or "pump",
        amount=str(raw), amount_kind="tokens", slippage_pct=slip, priority_fee_sol=live.priority_fee_sol,
        limits={"max_tokens_in": raw, "min_sol_out_lamports": min_out,
                "max_fee_transfer_lamports": int((expected_sol_out or Decimal(0)) * LAMPORTS * live.max_platform_fee_bps / 10_000)
                + 10_000,
                "max_priority_fee_lamports": int(live.max_priority_fee_sol * LAMPORTS), "decimals": decimals},
        status="PENDING", idempotency_key=f"exit:{position.id}:{reason}:{seq}:{int(now.timestamp())}",
    )
    session.add(order)
    await session.flush()
    position.pending_order_id = order.id
    await add_timeline_event(session, "live_exit_requested", now,
                             {"reason": reason, "tokens_raw": raw, "slippage_pct": str(slip), "min_sol_out_lamports": min_out},
                             candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
    return order


def _trade_request(order: ExecutionOrder, wallet: str) -> tuple[TradeRequest, GuardExpectation]:
    lim = order.limits or {}
    if order.side == "BUY":
        req = TradeRequest(wallet, "buy", order.mint, order.amount, True, order.slippage_pct, order.priority_fee_sol, order.route)
        exp = GuardExpectation(wallet=wallet, mint=order.mint, side="buy", max_sol_in_lamports=lim["max_sol_in_lamports"],
                               max_fee_transfer_lamports=lim["max_fee_transfer_lamports"],
                               max_priority_fee_lamports=lim["max_priority_fee_lamports"])
    else:
        dec = int(lim["decimals"])
        ui = (Decimal(order.amount) / Decimal(10) ** dec).normalize()
        req = TradeRequest(wallet, "sell", order.mint, format(ui, "f"), False, order.slippage_pct, order.priority_fee_sol, order.route)
        exp = GuardExpectation(wallet=wallet, mint=order.mint, side="sell", max_tokens_in=lim["max_tokens_in"],
                               min_sol_out_lamports=lim.get("min_sol_out_lamports") or None,
                               max_fee_transfer_lamports=lim["max_fee_transfer_lamports"],
                               max_priority_fee_lamports=lim["max_priority_fee_lamports"])
    return req, exp


async def apply_outcome(session: AsyncSession, redis: Redis | None, app_settings: Any, order: ExecutionOrder,
                        outcome: ExecOutcome, now: datetime) -> None:
    """Moves the order and its position from the executor's outcome. Only a
    CONFIRMED outcome with the expected balance change counts as a fill."""
    order.result = outcome.to_dict()
    order.guard = outcome.guard or order.guard
    order.updated_at = now
    position = await session.get(PaperPosition, order.position_id) if order.position_id else None
    account = await session.get(PaperAccount, position.account_id) if position is not None else None
    fill = outcome.fill

    if outcome.status == "CONFIRMED" and fill is not None:
        order.status, order.confirmed_at = "CONFIRMED", now
        if position is None:
            return
        position.pending_order_id = None
        dec = Decimal(10) ** int((order.limits or {}).get("decimals") or ((position.plan or {}).get("venue") or {}).get("decimals"))
        if order.side == "BUY":
            if fill.token_change_raw <= 0 or fill.sol_change_lamports >= 0:
                position.status = "needs_review"
                await _reconcile_event(session, "buy_confirmed_without_tokens", "critical", order, position,
                                       {"token_change_raw": fill.token_change_raw, "sol_change_lamports": fill.sol_change_lamports})
                await events.notify(session, redis, app_settings, "provider_failure", f"LIVE BUY needs review: {position.symbol}",
                                    "transaction confirmed but the wallet did not receive tokens", "critical",
                                    {"position_id": str(position.id), "signature": order.signature})
                return
            qty = Decimal(fill.token_change_raw) / dec
            cost = Decimal(-fill.sol_change_lamports) / LAMPORTS
            position.quantity = position.initial_quantity = position.remaining_quantity = qty
            position.entry_cost_quote = cost
            position.fees_paid_quote = Decimal(fill.fee_lamports) / LAMPORTS
            position.entry_price = cost / qty
            position.entry_at = now
            position.highest_price = position.lowest_price = position.last_price = position.entry_price
            position.status = "open"
            if account is not None:
                account.cash_balance -= cost
            if position.candidate_id:
                cand = await session.get(TradingCandidate, position.candidate_id)
                if cand is not None and cand.state == CandidateState.ENTRY_PENDING.value:
                    apply_transition(cand, CandidateState.ENTERED, reason=f"live fill {qty} @ {position.entry_price}")
                    apply_transition(cand, CandidateState.MANAGING, reason="live position open")
            await add_timeline_event(session, "live_entry_filled", now,
                                     {"signature": order.signature, "tokens": str(qty), "sol_spent": str(cost),
                                      "network_fee_sol": str(position.fees_paid_quote), "fill_price": str(position.entry_price),
                                      "planned_price": str((position.plan or {}).get("entry_price"))},
                                     candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
            await events.notify(session, redis, app_settings, "entry", f"LIVE entry: {position.symbol}",
                                f"{qty} tokens for {cost} SOL (tx {order.signature})", "warning", {"position_id": str(position.id)})
            await events.publish(redis, "trade.created", {"position_id": str(position.id), "mode": "LIVE", "symbol": position.symbol}, "live")
        else:
            sold = Decimal(-fill.token_change_raw) / dec
            received = Decimal(fill.sol_change_lamports) / LAMPORTS
            if sold <= 0:
                await _reconcile_event(session, "sell_confirmed_without_tokens_leaving", "critical", order, position,
                                       {"token_change_raw": fill.token_change_raw})
                position.status = "needs_review"
                return
            position.remaining_quantity = max(Decimal(0), (position.remaining_quantity or Decimal(0)) - sold)
            position.proceeds_quote = (position.proceeds_quote or Decimal(0)) + received
            position.fees_paid_quote = (position.fees_paid_quote or Decimal(0)) + Decimal(fill.fee_lamports) / LAMPORTS
            position.exit_failures = 0
            if order.reason.startswith("take_profit_"):
                idx = int(order.reason.rsplit("_", 1)[1]) - 1
                position.tp_hits = sorted(set((position.tp_hits or []) + [idx]))
            if account is not None:
                account.cash_balance += received
            price = received / sold
            await add_timeline_event(session, f"live_exit.{order.reason}", now,
                                     {"signature": order.signature, "tokens_sold": str(sold), "sol_received": str(received),
                                      "price": str(price)},
                                     candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
            remaining_raw = int((position.remaining_quantity * dec).to_integral_value(ROUND_DOWN))
            if remaining_raw <= DUST_RAW:
                position.remaining_quantity = Decimal(0)
                await paper_engine.close_position(session, position, now, order.reason, price)
                await events.notify(session, redis, app_settings, "close", f"LIVE position closed: {position.symbol}",
                                    f"{order.reason}, realized {position.realized_pnl:.6f} SOL", "warning",
                                    {"position_id": str(position.id)})
            await events.publish(redis, "trade.closed" if position.status == "closed" else "trade.updated",
                                 {"position_id": str(position.id), "mode": "LIVE", "reason": order.reason}, "live")
        await events.publish(redis, "balance.updated", {"account_id": str(position.account_id)}, "live")
        return

    # Not filled.
    order.status = "EXPIRED" if outcome.status == "EXPIRED" else "FAILED"
    order.error = (outcome.error or outcome.status)[:500]
    if position is None:
        return
    position.pending_order_id = None
    if order.side == "BUY":
        position.status = "failed"
        position.exit_reason = "entry_failed"
        position.exit_at = now
        if position.candidate_id:
            cand = await session.get(TradingCandidate, position.candidate_id)
            if cand is not None and cand.state == CandidateState.ENTRY_PENDING.value:
                apply_transition(cand, CandidateState.REJECTED, reason=f"live entry failed: {order.error[:120]}")
        await add_timeline_event(session, "live_entry_failed", now, {"error": order.error, "signature": order.signature},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
        await events.notify(session, redis, app_settings, "provider_failure", f"LIVE entry failed: {position.symbol}",
                            order.error, "warning", {"position_id": str(position.id)})
    else:
        position.exit_failures += 1
        await add_timeline_event(session, "live_exit_failed", now, {"reason": order.reason, "error": order.error,
                                                                    "attempt": position.exit_failures},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
        if position.exit_failures >= 2:
            await events.notify(session, redis, app_settings, "provider_failure",
                                f"LIVE exit failing: {position.symbol} ({order.reason})",
                                f"attempt {position.exit_failures}: {order.error}", "critical", {"position_id": str(position.id)})


async def _reconcile_event(session, kind: str, severity: str, order: ExecutionOrder | None, position: PaperPosition | None,
                           detail: dict) -> None:
    mint = order.mint if order else (position.asset_id if position else detail.get("mint"))
    session.add(ReconciliationEvent(kind=kind, severity=severity, mint=mint,
                                    position_id=position.id if position else None, order_id=order.id if order else None,
                                    detail=detail))


async def process_order(session_factory, redis: Redis | None, app_settings: Any, executor: SolanaLiveExecutor,
                        order_id, now_fn=lambda: datetime.now(timezone.utc)) -> str:
    """Runs one PENDING LIVE order end to end. Returns the final status."""
    async with session_factory() as session:
        order = (await session.execute(select(ExecutionOrder).where(ExecutionOrder.id == order_id)
                                       .with_for_update(skip_locked=True))).scalar_one_or_none()
        if order is None or order.status != "PENDING":
            return "skipped"
        order.attempts += 1
        order.updated_at = now_fn()
        await session.commit()
        req, exp = _trade_request(order, executor.wallet.pubkey)

    async def on_signed(signature: str) -> None:
        async with session_factory() as s:
            await s.execute(update(ExecutionOrder).where(ExecutionOrder.id == order_id).values(
                signature=signature, status="SIGNED", submitted_at=now_fn(), updated_at=now_fn()))
            await s.commit()

    outcome = await executor.execute(req, exp, on_signed)
    async with session_factory() as session:
        order = await session.get(ExecutionOrder, order_id)
        if outcome.status == "PENDING":
            order.status = "SUBMITTED"  # reconciliation will look the signature up
            await session.commit()
            return order.status
        await apply_outcome(session, redis, app_settings, order, outcome, now_fn())
        await session.commit()
        return order.status


async def reconcile(session_factory, redis: Redis | None, app_settings: Any, executor: SolanaLiveExecutor,
                    now: datetime | None = None) -> dict[str, Any]:
    """Wallet vs database, after any restart and periodically:
    1. wallet SOL balance → live book cash (the wallet is the truth);
    2. orders left SIGNED/SUBMITTED → look the signature up; confirmed ones
       are applied, dead ones failed/expired;
    3. open live positions vs token balances: missing tokens → the position
       needs a human (needs_review), never an invented exit;
    4. token balances no live position explains → unknown holding event."""
    import json

    now = now or datetime.now(timezone.utc)
    rpc = executor.rpc
    report: dict[str, Any] = {"orders_resolved": 0, "mismatches": 0, "unknown_holdings": 0}
    lamports, tokens = await wallet_balances(rpc, executor.wallet.pubkey)
    sol = Decimal(lamports) / LAMPORTS
    report["sol"] = str(sol)
    if redis is not None:
        await redis.set(WALLET_KEY, json.dumps({"sol": str(sol), "at": now.isoformat(), "pubkey": executor.wallet.pubkey,
                                                "tokens": len(tokens)}), ex=3600)
    async with session_factory() as session:
        acct = await get_live_account(session)
        if acct.cash_balance != sol:
            acct.cash_balance = sol
        if acct.starting_balance <= Decimal("1e-9") and sol > 0:
            acct.starting_balance = sol

        stuck = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.status.in_(("SIGNED", "SUBMITTED"))))).scalars().all()
        for order in stuck:
            age = (now - (order.submitted_at or order.created_at)).total_seconds()
            if age < SIGNED_RECHECK_SECONDS or not order.signature:
                continue
            outcome = await executor.lookup(order.signature, order.mint)
            if outcome.status == "PENDING" and age > ORDER_EXPIRY_SECONDS:
                outcome.status, outcome.error = "EXPIRED", f"not found {age:.0f}s after signing; blockhash expired"
            if outcome.status != "PENDING":
                await apply_outcome(session, redis, app_settings, order, outcome, now)
                await _reconcile_event(session, f"order_{outcome.status.lower()}_on_reconcile", "warning", order, None,
                                       {"signature": order.signature, "age_seconds": int(age)})
                report["orders_resolved"] += 1
        stale = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "PENDING",
            ExecutionOrder.created_at < now - timedelta(seconds=STALE_PENDING_SECONDS)))).scalars().all()
        for order in stale:
            await apply_outcome(session, redis, app_settings, order,
                                ExecOutcome("FAILED", error="order never picked up by the worker; cancelled as stale"), now)
            order.status = "CANCELLED"

        positions = (await session.execute(select(PaperPosition).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.status == "open"))).scalars().all()
        known = {p.asset_id for p in positions}
        known |= set((await session.execute(select(PaperPosition.asset_id).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.status.in_(("pending_entry", "needs_review"))))).scalars())
        for p in positions:
            dec = Decimal(10) ** int(((p.plan or {}).get("venue") or {}).get("decimals") or 6)
            expected = int(((p.remaining_quantity or Decimal(0)) * dec).to_integral_value(ROUND_DOWN))
            onchain = int((tokens.get(p.asset_id) or {}).get("amount", 0))
            if p.pending_order_id is not None:
                continue
            if onchain == 0 and expected > 0:
                p.status = "needs_review"
                await _reconcile_event(session, "position_tokens_missing", "critical", None, p,
                                       {"expected_raw": expected, "onchain_raw": onchain})
                await events.notify(session, redis, app_settings, "provider_failure", f"LIVE position needs review: {p.symbol}",
                                    "wallet holds none of this token; no exit was recorded", "critical", {"position_id": str(p.id)})
                report["mismatches"] += 1
            elif expected and abs(onchain - expected) > max(DUST_RAW, expected // 100):
                await _reconcile_event(session, "position_quantity_mismatch", "warning", None, p,
                                       {"expected_raw": expected, "onchain_raw": onchain})
                if onchain < expected:
                    p.remaining_quantity = Decimal(onchain) / dec  # never assume more than the wallet holds
                report["mismatches"] += 1
        for mint, info in tokens.items():
            if info.get("amount", 0) > 0 and mint not in known and mint != "So11111111111111111111111111111111111111112":
                if redis is None or await redis.set(f"yx:live:unknown:{mint}", "1", nx=True, ex=86400):
                    await _reconcile_event(session, "unknown_holding", "info", None, None,
                                           {"mint": mint, "amount_raw": info["amount"], "decimals": info.get("decimals")})
                report["unknown_holdings"] += 1
        await session.commit()
    return report


async def manage_live_position(session: AsyncSession, p: PaperPosition, price: Decimal, model, now: datetime,
                               extra_exit: tuple[Decimal, str] | None = None) -> dict[str, Any]:
    """One management tick for an open LIVE position, using exactly the
    paper decision logic (`manage_step`). Price marks, trailing ratchets and
    the breakeven move are saved immediately (they are decisions, not
    fills); a triggered exit becomes a SELL order and the position only
    changes when that order confirms."""
    s = paper_engine.state_of(p)
    tp_before = list(s.tp_hits)
    result = paper_engine.manage_step(s, price, exit_now=bool(p.exit_requested))
    if extra_exit and not result.exits and extra_exit[0] > 0:
        result.exits.append((min(extra_exit[0], p.remaining_quantity or Decimal(0)), extra_exit[1]))
    p.highest_price, p.lowest_price = s.highest_price, s.lowest_price
    p.trailing_stop = s.trailing_stop
    p.stop_loss = s.stop_loss  # only ever tightened by manage_step
    p.last_price, p.last_marked_at = price, now
    for kind, detail in result.events:
        if kind.startswith("trailing") or kind == "stop_to_breakeven":
            await add_timeline_event(session, kind, now, detail, candidate_id=p.candidate_id,
                                     assessment_id=p.assessment_id, position_id=p.id)
    out: dict[str, Any] = {"exits": [r for _, r in result.exits], "requested": None, "tp_hits_simulated": s.tp_hits != tp_before}
    if not result.exits or p.pending_order_id is not None:
        return out
    qty, reason = result.exits[0]
    if reason in ("stop_loss", "trailing_stop", "manual_exit") or (result.closed and len(result.exits) == 1):
        qty = p.remaining_quantity or Decimal(0)  # a full exit sells everything the position holds
    expected = None
    if model is not None:
        c = paper_engine.close_fill(model, qty, "LONG")
        expected = (c.quote - c.fee) if c.complete else None
    else:
        expected = qty * price
    order = await request_live_exit(session, p, qty, reason, expected, now)
    out["requested"] = order.reason if order else None
    return out
