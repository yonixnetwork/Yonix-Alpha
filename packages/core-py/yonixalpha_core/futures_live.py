"""LIVE execution for derivatives and FX strategies (Binance USDⓈ-M, Bybit
linear, Hyperliquid perps, MT5 through the bridge).

Same shape as the Solana live path (live_trading.py): the safety gate
decides the entry, `paper_engine.manage_step` decides every exit, and
`paper_engine.close_position` books the outcome and ML labels. Only the
fills differ — they come from the exchange's own order status and trade
history, never from an assumption:

- enter(): an executable LIVE assessment becomes a `pending_entry`
  position and a PENDING ExecutionOrder (idempotency key
  entry:<assessment>, so a repeated decision cannot open twice);
- process_order(): the worker (services/execution-futures) sends it with
  OUR client id — stored on the order BEFORE sending, so a crash is
  resolved by asking the exchange about that id — and applies only the
  confirmed filled quantity, average price and fee;
- after an entry fills, an exchange-side stop (closePosition / position
  stopLoss / reduce-only trigger) is placed at the plan's stop, so the
  position stays protected even if YonixAlpha is down. If the stop
  cannot be placed, the position is closed at once: no position is ever
  left with undefined risk;
- manage(): price marks, take-profits, trailing and operator exits via
  manage_step; exits become reduce-only market orders; a tightened stop
  (trailing / breakeven) moves the exchange stop too — never loosens it;
- reconcile(): exchange balance → the live book's cash, orders left
  SUBMITTED are looked up, an exchange that is flat while the record is
  open (its stop or a liquidation fired) is booked from the exchange's own
  fills, missing protection is re-placed, size mismatches are flagged.

Accounting is the paper futures model: entry cost = margin + entry fee;
each exit returns margin share + PnL − exit fee.
"""

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, paper_engine
from yonixalpha_core.db.models import ExecutionOrder, PaperAccount, PaperPosition, PlatformSetting, ReconciliationEvent
from yonixalpha_core.execution.base import (
    TERMINAL, UNKNOWN, ExecutionError, NotConfigured, OrderState, close_side, open_side,
)
from yonixalpha_core.execution.registry import FUTURES_PROVIDERS, PROVIDER_NAME
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.store import add_timeline_event, live_trading_permitted

log = get_logger("core.futures_live")

SETTINGS_KEY = "futures_live_execution"
READY_KEY = "yx:live:futures:{venue}"
SOURCE = {"binance": "BINANCE", "bybit": "BYBIT", "hyperliquid": "HYPERLIQUID", "mt5": "MT5"}
ACTIVE = ("pending_entry", "open", "needs_review")
SUBMITTED_RECHECK_SECONDS = 15
ORDER_EXPIRY_SECONDS = 300
STALE_PENDING_SECONDS = 300
POLL_ATTEMPTS = 6
POLL_DELAY_SECONDS = 0.5


@dataclass
class FuturesLiveSettings:
    """Runtime parameters (dashboard, platform_settings); no secrets."""

    max_leverage: int = 5
    min_free_balance: Decimal = Decimal("10")  # quote currency never committed as margin
    balance_max_age_seconds: int = 120
    max_fill_deviation_pct: Decimal = Decimal("1")  # entry fill vs planned price: beyond this, close at once

    def to_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


LIMITS = {"max_leverage": (Decimal(1), Decimal(20)), "min_free_balance": (Decimal(0), Decimal(1_000_000)),
          "balance_max_age_seconds": (Decimal(30), Decimal(900)), "max_fill_deviation_pct": (Decimal("0.05"), Decimal(10))}


def parse_settings(data: dict[str, Any]) -> tuple[FuturesLiveSettings, list[str]]:
    base, errors = FuturesLiveSettings(), []
    for key, value in (data or {}).items():
        if key not in LIMITS:
            errors.append(f"{key}: unknown setting")
            continue
        if isinstance(value, bool):
            errors.append(f"{key}: must be a number")
            continue
        try:
            d = Decimal(str(value))
        except ArithmeticError:
            errors.append(f"{key}: must be a number")
            continue
        lo, hi = LIMITS[key]
        if not d.is_finite() or d < lo or d > hi:
            errors.append(f"{key}: must be between {lo} and {hi}")
            continue
        setattr(base, key, int(d) if isinstance(getattr(base, key), int) else d)
    return base, errors


async def load_settings(session: AsyncSession) -> FuturesLiveSettings:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_settings((row.value if row else None) or {})
    return s if not errors else FuturesLiveSettings()


def account_name(venue: str) -> str:
    return f"live_{venue}"


async def get_live_account(session: AsyncSession, venue: str, quote: str) -> PaperAccount:
    name = account_name(venue)
    acct = (await session.execute(select(PaperAccount).where(PaperAccount.name == name))).scalar_one_or_none()
    if acct is None:
        # Unknown balance until the first exchange sync: zero capacity.
        acct = PaperAccount(name=name, quote_currency=quote, starting_balance=Decimal("1e-9"), cash_balance=Decimal(0),
                            reset_at=datetime.now(timezone.utc))
        session.add(acct)
        await session.flush()
    return acct


async def readiness(redis: Redis | None, app_settings: Any, venue: str, now: datetime | None = None) -> tuple[bool, str | None]:
    """Everything that must hold before a LIVE futures order may be created."""
    if venue not in PROVIDER_NAME:
        return False, f"no live execution provider for venue {venue}"
    if not live_trading_permitted(app_settings):
        return False, "environment locks closed (TRADING_ENABLED, LIVE_TRADING_ENABLED, PAPER_TRADING=false required)"
    if redis is None:
        return False, "no Redis connection"
    raw = await redis.get(READY_KEY.format(venue=venue))
    if not raw:
        return False, f"execution-futures worker has not reported {venue} (service down or venue not enabled)"
    state = json.loads(raw)
    if state.get("status") != "ready":
        return False, f"{venue} execution not ready: {state.get('reason')}"
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(state["at"])).total_seconds()
    if age > float(state.get("balance_max_age_seconds", 120)):
        return False, f"{venue} balance is {age:.0f}s old"
    return True, None


def client_id(order: ExecutionOrder) -> str:
    # Binance newClientOrderId / clientAlgoId: ^[.A-Z:/a-z0-9_-]{1,36}$;
    # Bybit orderLinkId <= 36; Hyperliquid hashes it into a 16-byte cloid.
    return f"yx{order.id.hex[:30]}"


# -- entry -------------------------------------------------------------------

async def enter(session: AsyncSession, account: PaperAccount, assessment, assessment_id, venue: str, symbol: str,
                now: datetime, provenance: dict[str, Any] | None = None) -> PaperPosition:
    """Pending LIVE position + its entry order for an executable LIVE
    assessment. Raises ValueError when anything forbids it. Caller commits."""
    provenance = provenance or {}
    plan = assessment.plan
    if not assessment.executable or assessment.execution_target.value != "LIVE" or not plan.complete:
        raise ValueError("assessment is not an executable LIVE plan")
    if plan.side not in ("LONG", "SHORT"):
        raise ValueError(f"unsupported side {plan.side}")
    provider = PROVIDER_NAME.get(venue)
    if provider is None:
        raise ValueError(f"no live execution provider for venue {venue}")
    live = await load_settings(session)
    leverage = int(plan.leverage) if plan.leverage and plan.leverage > 0 else 1
    if leverage > live.max_leverage:
        raise ValueError(f"leverage {leverage} exceeds the live limit {live.max_leverage}")
    notional = plan.position_size.value
    margin = notional / leverage
    if margin > account.cash_balance - live.min_free_balance:
        raise ValueError(f"margin {margin:.4f} exceeds available {account.cash_balance} minus reserve {live.min_free_balance}")
    busy = (await session.execute(select(PaperPosition.id).where(
        PaperPosition.execution_mode == "LIVE", PaperPosition.execution_provider == provider,
        PaperPosition.asset_id == symbol, PaperPosition.status.in_(ACTIVE)))).first()
    if busy is not None:
        raise ValueError(f"a live {venue} position for {symbol} already exists")
    qty = notional / plan.entry_price

    position = PaperPosition(
        symbol=symbol[:64], provider="live", side=plan.side, entry_price=plan.entry_price, quantity=Decimal(0),
        stop_loss=plan.stop_loss.value, take_profit=[str(tp.price.value) for tp in plan.take_profits], entry_at=now,
        status="pending_entry", account_id=account.id, assessment_id=assessment_id, engine=assessment.engine,
        asset_id=symbol, initial_quantity=Decimal(0), remaining_quantity=Decimal(0), entry_cost_quote=Decimal(0),
        proceeds_quote=Decimal(0), fees_paid_quote=Decimal(0), max_loss_quote=plan.max_loss.value,
        plan={**plan.to_dict(), "venue": {"type": f"{venue}_live", "kind": "futures", "venue": venue, "symbol": symbol,
                                         "leverage": leverage, **(provenance.get("venue") or {})}},
        tp_hits=[], highest_price=plan.entry_price, lowest_price=plan.entry_price, last_price=plan.entry_price,
        last_marked_at=now, execution_mode="LIVE", source=SOURCE[venue], execution_provider=provider,
        execution_route="perp" if venue != "mt5" else "fx", strategy=assessment.strategy,
        model_version=provenance.get("model_version"), feature_version=provenance.get("feature_version"),
    )
    session.add(position)
    await session.flush()
    order = ExecutionOrder(
        position_id=position.id, assessment_id=assessment_id, mode="LIVE", side=open_side(plan.side), reason="entry",
        mint=symbol[:64], provider=provider, route=position.execution_route, amount=str(qty), amount_kind="base",
        slippage_pct=live.max_fill_deviation_pct, priority_fee_sol=Decimal(0),
        limits={"leverage": leverage, "ref_price": str(plan.entry_price), "stop": str(plan.stop_loss.value),
                "notional": str(notional)},
        status="PENDING", idempotency_key=f"entry:{assessment_id}",
    )
    session.add(order)
    await session.flush()
    position.pending_order_id = order.id
    await add_timeline_event(session, "live_entry_requested", now,
                             {"venue": venue, "symbol": symbol, "side": plan.side, "quantity": str(qty),
                              "notional": str(notional), "leverage": leverage, "stop": str(plan.stop_loss.value)},
                             assessment_id=assessment_id, position_id=position.id)
    return position


async def request_exit(session: AsyncSession, position: PaperPosition, quantity: Decimal, reason: str,
                       now: datetime) -> ExecutionOrder | None:
    """Queues a reduce-only close for `quantity` unless one is pending."""
    if position.pending_order_id is not None or position.status != "open":
        return None
    qty = min(quantity, position.remaining_quantity or Decimal(0))
    if qty <= 0:
        return None
    seq = len(position.tp_hits or []) + position.exit_failures
    order = ExecutionOrder(
        position_id=position.id, assessment_id=position.assessment_id, mode="LIVE", side=close_side(position.side),
        reason=reason[:32], mint=position.asset_id, provider=position.execution_provider, route=position.execution_route,
        amount=str(qty), amount_kind="base", slippage_pct=Decimal(0), priority_fee_sol=Decimal(0),
        limits={"reduce_only": True, "full": qty >= (position.remaining_quantity or Decimal(0)),
                "ref_price": str(position.last_price) if position.last_price else None},
        status="PENDING", idempotency_key=f"exit:{position.id}:{reason}:{seq}:{int(now.timestamp())}",
    )
    session.add(order)
    await session.flush()
    position.pending_order_id = order.id
    await add_timeline_event(session, "live_exit_requested", now, {"reason": reason, "quantity": str(qty)},
                             assessment_id=position.assessment_id, position_id=position.id)
    return order


# -- worker --------------------------------------------------------------------

def _venue_of(position: PaperPosition | None, order: ExecutionOrder) -> str:
    for venue, name in PROVIDER_NAME.items():
        if name == order.provider:
            return venue
    raise ValueError(f"order provider {order.provider} is not a futures provider")


async def _poll(provider, symbol: str, cid: str, first: OrderState, sleep=asyncio.sleep) -> OrderState:
    state = first
    for _ in range(POLL_ATTEMPTS):
        if state.status in TERMINAL:
            return state
        await sleep(POLL_DELAY_SECONDS)
        state = await provider.order_status(symbol, cid)
    return state


async def process_order(session_factory, redis: Redis | None, app_settings: Any, providers: dict, order_id,
                        now_fn=lambda: datetime.now(timezone.utc), sleep=asyncio.sleep) -> str:
    """Runs one PENDING futures order. Returns the order's final status."""
    async with session_factory() as session:
        order = (await session.execute(select(ExecutionOrder).where(ExecutionOrder.id == order_id)
                                       .with_for_update(skip_locked=True))).scalar_one_or_none()
        if order is None or order.status != "PENDING" or order.provider not in FUTURES_PROVIDERS:
            return "skipped"
        position = await session.get(PaperPosition, order.position_id) if order.position_id else None
        venue = _venue_of(position, order)
        cid = client_id(order)
        # The client id is persisted before anything is sent: after a crash,
        # reconcile asks the exchange about exactly this id.
        order.signature, order.status, order.attempts = cid, "SUBMITTED", order.attempts + 1
        order.submitted_at = order.updated_at = now_fn()
        await session.commit()
        symbol, side, amount, is_entry = order.mint, order.side, Decimal(order.amount), order.reason == "entry"
        lim = dict(order.limits or {})
        remaining = (position.remaining_quantity if position is not None else None) or Decimal(0)

    provider = providers[venue]
    try:
        rules = await provider.instrument(symbol)
        ref = Decimal(lim["ref_price"]) if lim.get("ref_price") else None
        if is_entry:
            await provider.prepare(symbol, int(lim.get("leverage") or 1))
            qty = rules.round_qty(amount)
            problem = rules.check(qty, ref or Decimal(0)) if ref else (None if qty > 0 else "quantity rounds to zero")
            if problem:
                state = OrderState(cid, "REJECTED", error=f"not sent: {problem}")
            else:
                state = await provider.market_order(symbol, side, qty, False, cid, ref)
        else:
            qty = rules.round_qty(amount)
            if lim.get("full") or qty <= 0:
                live_pos = await provider.position(symbol)
                qty = abs(live_pos.size) if live_pos else rules.round_qty(remaining)
            if qty <= 0:
                state = OrderState(cid, "REJECTED", error="nothing to close on the exchange")
            else:
                state = await provider.market_order(symbol, side, qty, True, cid, ref)
        state = await _poll(provider, symbol, cid, state, sleep)
    except NotConfigured as exc:
        state = OrderState(cid, "REJECTED", error=f"not sent: {exc}")
    except ExecutionError as exc:
        # Transport failure or an exchange error. It may or may not have
        # reached the book: resolved by reconcile via the client id.
        log.warning("futures_live.order_error", order_id=str(order_id), error=str(exc)[:200])
        async with session_factory() as session:
            await session.execute(update(ExecutionOrder).where(ExecutionOrder.id == order_id).values(
                error=str(exc)[:500], updated_at=now_fn()))
            await session.commit()
        return "SUBMITTED"

    async with session_factory() as session:
        order = await session.get(ExecutionOrder, order_id)
        if state.status not in TERMINAL:
            order.result = state.to_dict()
            await session.commit()
            return order.status  # SUBMITTED: reconcile follows it up until the exchange reports a final state
        await apply_state(session, redis, app_settings, providers, order, state, now_fn())
        await session.commit()
        return order.status


async def apply_state(session: AsyncSession, redis: Redis | None, app_settings: Any, providers: dict,
                      order: ExecutionOrder, state: OrderState, now: datetime) -> None:
    """Moves order and position from the exchange's answer. Only a filled
    quantity > 0 with an average price counts as a fill."""
    order.result = state.to_dict()
    order.updated_at = now
    position = await session.get(PaperPosition, order.position_id) if order.position_id else None
    account = await session.get(PaperAccount, position.account_id) if position is not None else None
    venue = _venue_of(position, order)
    provider = providers.get(venue)

    if state.filled_qty > 0 and state.avg_price:
        order.status, order.confirmed_at = "CONFIRMED", now
        if position is None:
            return
        position.pending_order_id = None
        qty, px, fee = state.filled_qty, state.avg_price, state.fee
        if order.reason == "entry":
            leverage = Decimal(((position.plan or {}).get("venue") or {}).get("leverage") or 1)
            margin = qty * px / leverage
            position.quantity = position.initial_quantity = position.remaining_quantity = qty
            position.entry_price, position.entry_at = px, now
            position.entry_cost_quote = margin + fee
            position.fees_paid_quote = fee
            position.highest_price = position.lowest_price = position.last_price = px
            position.plan = {**position.plan, "venue": {**position.plan["venue"], "margin": str(margin),
                                                        "notional": str(qty * px), "exchange_order_id": state.exchange_id}}
            position.status = "open"
            if account is not None:
                account.cash_balance -= margin + fee
            await add_timeline_event(session, "live_entry_filled", now,
                                     {"quantity": str(qty), "fill_price": str(px), "fee": str(fee), "status": state.status,
                                      "planned_price": str((position.plan or {}).get("entry_price")),
                                      "exchange_order_id": state.exchange_id},
                                     assessment_id=position.assessment_id, position_id=position.id)
            await events.notify(session, redis, app_settings, "entry", f"LIVE entry: {position.symbol} {position.side}",
                                f"{qty} @ {px} on {venue}", "warning", {"position_id": str(position.id)})
            await events.publish(redis, "trade.created", {"position_id": str(position.id), "mode": "LIVE",
                                                          "symbol": position.symbol}, "live")
            planned = Decimal(str((position.plan or {}).get("entry_price") or px))
            deviation = abs(px / planned - 1) * 100 if planned else Decimal(0)
            if deviation > (order.slippage_pct or Decimal(1)):
                await _protection_failed(session, position, f"entry filled {deviation:.2f}% from plan", now)
                return
            if provider is not None:
                await protect(session, provider, position, position.stop_loss, now)
        else:
            await _book_exit(session, redis, app_settings, provider, position, account, qty, px, fee, order.reason, now)
        await events.publish(redis, "balance.updated", {"account_id": str(position.account_id)}, "live")
        return

    order.status = "EXPIRED" if state.status == "EXPIRED" else "FAILED"
    order.error = (state.error or state.status)[:500]
    if position is None:
        return
    position.pending_order_id = None
    if order.reason == "entry":
        position.status, position.exit_reason, position.exit_at = "failed", "entry_failed", now
        await add_timeline_event(session, "live_entry_failed", now, {"error": order.error},
                                 assessment_id=position.assessment_id, position_id=position.id)
        await events.notify(session, redis, app_settings, "provider_failure", f"LIVE entry failed: {position.symbol}",
                            order.error, "warning", {"position_id": str(position.id)})
    else:
        position.exit_failures += 1
        await add_timeline_event(session, "live_exit_failed", now, {"reason": order.reason, "error": order.error,
                                                                    "attempt": position.exit_failures},
                                 assessment_id=position.assessment_id, position_id=position.id)
        if position.exit_failures >= 2:
            await events.notify(session, redis, app_settings, "provider_failure",
                                f"LIVE exit failing: {position.symbol} ({order.reason})",
                                f"attempt {position.exit_failures}: {order.error}", "critical", {"position_id": str(position.id)})


async def _book_exit(session, redis, app_settings, provider, position: PaperPosition, account, qty: Decimal, px: Decimal,
                     fee: Decimal, reason: str, now: datetime) -> None:
    venue_info = (position.plan or {}).get("venue") or {}
    initial = position.initial_quantity or position.quantity
    qty = min(qty, position.remaining_quantity or qty)
    sign = Decimal(1) if position.side == "LONG" else Decimal(-1)
    pnl = sign * (px - position.entry_price) * qty
    returned = Decimal(venue_info.get("margin", "0")) * qty / initial + pnl - fee
    position.remaining_quantity = max(Decimal(0), (position.remaining_quantity or Decimal(0)) - qty)
    position.proceeds_quote = (position.proceeds_quote or Decimal(0)) + returned
    position.fees_paid_quote = (position.fees_paid_quote or Decimal(0)) + fee
    position.exit_failures = 0
    if reason.startswith("take_profit_"):
        position.tp_hits = sorted(set((position.tp_hits or []) + [int(reason.rsplit("_", 1)[1]) - 1]))
    if account is not None:
        account.cash_balance += returned
    await add_timeline_event(session, f"live_exit.{reason}", now,
                             {"quantity": str(qty), "price": str(px), "fee": str(fee), "pnl": str(pnl), "returned": str(returned)},
                             assessment_id=position.assessment_id, position_id=position.id)
    step = Decimal(0)
    if provider is not None:
        try:
            step = (await provider.instrument(position.asset_id)).qty_step
        except ExecutionError:
            step = Decimal(0)
    if position.remaining_quantity <= step / 2 or position.remaining_quantity <= 0:
        position.remaining_quantity = Decimal(0)
        await paper_engine.close_position(session, position, now, reason, px)
        if provider is not None:
            try:
                await provider.cancel_protection(position.asset_id)
            except ExecutionError as exc:
                log.warning("futures_live.cancel_protection_failed", symbol=position.asset_id, error=str(exc)[:200])
        await events.notify(session, redis, app_settings, "close", f"LIVE position closed: {position.symbol}",
                            f"{reason}, realized {position.realized_pnl:.4f}", "warning", {"position_id": str(position.id)})
    await events.publish(redis, "trade.closed" if position.status == "closed" else "trade.updated",
                         {"position_id": str(position.id), "mode": "LIVE", "reason": reason}, "live")


async def protect(session: AsyncSession, provider, position: PaperPosition, stop: Decimal, now: datetime) -> bool:
    """(Re)places the exchange-side stop for the whole position: the new
    stop first, then the old one is cancelled, so the position is never
    without one. If no stop can be placed the position is closed: it must
    never stay open with undefined risk."""
    old = ((position.plan or {}).get("venue") or {}).get("exchange_stop_id")
    try:
        sid = await provider.set_stop(position.asset_id, position.side, stop, position.remaining_quantity,
                                      f"yx{position.id.hex[:24]}s{int(now.timestamp()) % 100000}")
    except ExecutionError as exc:
        await _protection_failed(session, position, f"exchange stop not placed: {exc}", now)
        return False
    if old and old != sid:
        try:
            await provider.cancel_stop(position.asset_id, old)
        except ExecutionError as exc:
            # The old stop is looser than the new one, so leaving it costs nothing.
            log.warning("futures_live.old_stop_not_cancelled", symbol=position.asset_id, error=str(exc)[:200])
    position.plan = {**position.plan, "venue": {**position.plan["venue"], "exchange_stop": str(stop), "exchange_stop_id": sid}}
    await add_timeline_event(session, "exchange_stop_set", now, {"stop": str(stop), "id": sid},
                             assessment_id=position.assessment_id, position_id=position.id)
    return True


async def _protection_failed(session, position: PaperPosition, why: str, now: datetime) -> None:
    await _reconcile_event(session, "protection_failed", "critical", None, position, {"reason": why[:300]})
    position.exit_requested = True
    await request_exit(session, position, position.remaining_quantity or Decimal(0), "protection_failed", now)


async def _reconcile_event(session, kind: str, severity: str, order: ExecutionOrder | None, position: PaperPosition | None,
                           detail: dict) -> None:
    session.add(ReconciliationEvent(kind=kind, severity=severity,
                                    mint=order.mint if order else (position.asset_id if position else detail.get("symbol")),
                                    position_id=position.id if position else None, order_id=order.id if order else None,
                                    detail=detail))


# -- management ----------------------------------------------------------------

def _effective_stop(p: PaperPosition) -> Decimal:
    if p.trailing_stop is None:
        return p.stop_loss
    return max(p.stop_loss, p.trailing_stop) if p.side == "LONG" else min(p.stop_loss, p.trailing_stop)


async def manage_position(session: AsyncSession, provider, p: PaperPosition, price: Decimal, now: datetime) -> dict[str, Any]:
    """One tick for an open LIVE futures position (manage_step decides)."""
    s = paper_engine.state_of(p)
    result = paper_engine.manage_step(s, price, exit_now=bool(p.exit_requested))
    p.highest_price, p.lowest_price, p.trailing_stop, p.stop_loss = s.highest_price, s.lowest_price, s.trailing_stop, s.stop_loss
    p.last_price, p.last_marked_at = price, now
    for kind, detail in result.events:
        if kind.startswith("trailing") or kind == "stop_to_breakeven":
            await add_timeline_event(session, kind, now, detail, assessment_id=p.assessment_id, position_id=p.id)
    out: dict[str, Any] = {"exits": [r for _, r in result.exits], "requested": None, "stop_moved": False}
    if result.exits and p.pending_order_id is None:
        qty, reason = result.exits[0]
        if reason in ("stop_loss", "trailing_stop", "manual_exit") or (result.closed and len(result.exits) == 1):
            qty = p.remaining_quantity or Decimal(0)
        order = await request_exit(session, p, qty, reason, now)
        out["requested"] = order.reason if order else None
        return out
    # Tighten the exchange stop when the software stop moved in our favour.
    venue_info = (p.plan or {}).get("venue") or {}
    current = Decimal(venue_info["exchange_stop"]) if venue_info.get("exchange_stop") else None
    target = _effective_stop(p)
    tighter = current is None or (target > current if p.side == "LONG" else target < current)
    if tighter and p.pending_order_id is None and not result.exits:
        rules = await provider.instrument(p.asset_id)
        if current is None or abs(rules.round_price(target) - current) >= rules.tick_size:
            out["stop_moved"] = await protect(session, provider, p, rules.round_price(target), now)
    return out


async def manage(session_factory, providers: dict, price_fn: Callable[[str, str], Awaitable[Decimal | None]],
                 now: datetime) -> dict[str, int]:
    counts = {"managed": 0, "unpriced": 0, "errors": 0}
    async with session_factory() as session:
        ids = (await session.execute(select(PaperPosition.id).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.status == "open",
            PaperPosition.execution_provider.in_(FUTURES_PROVIDERS)))).scalars().all()
    for pid in ids:
        try:
            async with session_factory() as session:
                p = await session.get(PaperPosition, pid)
                if p is None or p.status != "open":
                    continue
                venue = ((p.plan or {}).get("venue") or {}).get("venue")
                price = await price_fn(venue, p.asset_id)
                if price is None:
                    counts["unpriced"] += 1
                    continue
                await manage_position(session, providers[venue], p, price, now)
                await session.commit()
                counts["managed"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate positions from each other
            counts["errors"] += 1
            log.warning("futures_live.manage_failed", position_id=str(pid), error=str(exc)[:200])
    return counts


# -- reconciliation --------------------------------------------------------------

async def publish_readiness(redis: Redis | None, venue: str, status: str, reason: str | None, now: datetime,
                            live: FuturesLiveSettings, balance: Decimal | None = None, quote: str | None = None) -> None:
    if redis is None:
        return
    await redis.set(READY_KEY.format(venue=venue), json.dumps({
        "status": status, "reason": reason, "at": now.isoformat(), "balance": str(balance) if balance is not None else None,
        "quote": quote, "balance_max_age_seconds": live.balance_max_age_seconds}), ex=3600)


async def reconcile_venue(session_factory, redis: Redis | None, app_settings: Any, venue: str, provider,
                          now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    name = PROVIDER_NAME[venue]
    report: dict[str, Any] = {"venue": venue, "orders_resolved": 0, "closed_by_exchange": 0, "mismatches": 0,
                              "protection_replaced": 0}
    async with session_factory() as session:
        live = await load_settings(session)
    if not provider.configured:
        await publish_readiness(redis, venue, "not_configured", "credentials not set", now, live)
        report["status"] = "not_configured"
        return report
    try:
        balance = await provider.balance()
    except ExecutionError as exc:
        await publish_readiness(redis, venue, "unavailable", str(exc)[:200], now, live)
        report["status"] = "unavailable"
        return report
    ready = balance > live.min_free_balance
    await publish_readiness(redis, venue, "ready" if ready else "insufficient_balance",
                            None if ready else f"available {balance} is at or below the reserve {live.min_free_balance}",
                            now, live, balance, provider.quote_currency)
    report["balance"] = str(balance)

    async with session_factory() as session:
        acct = await get_live_account(session, venue, provider.quote_currency)
        # Available balance excludes margin in use, which is what the gate
        # may still commit.
        acct.cash_balance = balance
        if acct.starting_balance <= Decimal("1e-9") and balance > 0:
            acct.starting_balance = balance

        stuck = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == name, ExecutionOrder.status == "SUBMITTED"))).scalars().all()
        for order in stuck:
            age = (now - (order.submitted_at or order.created_at)).total_seconds()
            if age < SUBMITTED_RECHECK_SECONDS or not order.signature:
                continue
            try:
                state = await provider.order_status(order.mint, order.signature)
            except ExecutionError as exc:
                log.warning("futures_live.status_failed", order_id=str(order.id), error=str(exc)[:200])
                continue
            if state.status == UNKNOWN and age > ORDER_EXPIRY_SECONDS:
                state.status, state.error = "EXPIRED", f"unknown to the exchange {age:.0f}s after submission"
            if state.status in TERMINAL:
                await apply_state(session, redis, app_settings, {venue: provider}, order, state, now)
                await _reconcile_event(session, f"order_{order.status.lower()}_on_reconcile", "warning", order, None,
                                       {"client_id": order.signature, "age_seconds": int(age), "exchange_status": state.status})
                report["orders_resolved"] += 1
        stale = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == name, ExecutionOrder.status == "PENDING",
            ExecutionOrder.created_at < now - timedelta(seconds=STALE_PENDING_SECONDS)))).scalars().all()
        for order in stale:
            await apply_state(session, redis, app_settings, {venue: provider}, order,
                              OrderState("", "REJECTED", error="never picked up by the worker; cancelled as stale"), now)
            order.status = "CANCELLED"

        positions = (await session.execute(select(PaperPosition).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.execution_provider == name,
            PaperPosition.status == "open"))).scalars().all()
        for p in positions:
            if p.pending_order_id is not None:
                continue
            try:
                ex = await provider.position(p.asset_id)
            except ExecutionError:
                continue
            expected = p.remaining_quantity or Decimal(0)
            if ex is None or ex.size == 0 or (ex.size > 0) != (p.side == "LONG"):
                # The exchange is flat: its stop, a liquidation or a manual
                # close. Book it from the exchange's own closing fills.
                start = int(p.entry_at.timestamp() * 1000)
                fills = [f for f in await provider.fills_since(p.asset_id, start) if f.side == close_side(p.side)]
                qty = sum((f.qty for f in fills), Decimal(0))
                acct_row = await session.get(PaperAccount, p.account_id)
                if qty > 0 and qty >= expected * Decimal("0.99"):
                    vwap = sum((f.qty * f.price for f in fills), Decimal(0)) / qty
                    fee = sum((f.fee for f in fills), Decimal(0))
                    await _book_exit(session, redis, app_settings, provider, p, acct_row, expected, vwap, fee,
                                     "exchange_stop", now)
                    await _reconcile_event(session, "closed_by_exchange", "warning", None, p,
                                           {"fills": len(fills), "quantity": str(qty), "vwap": str(vwap)})
                    report["closed_by_exchange"] += 1
                else:
                    p.status = "needs_review"
                    await _reconcile_event(session, "position_missing_on_exchange", "critical", None, p,
                                           {"expected": str(expected), "closing_fills_qty": str(qty)})
                    await events.notify(session, redis, app_settings, "provider_failure",
                                        f"LIVE position needs review: {p.symbol} ({venue})",
                                        "the exchange shows no position and no matching closing fills", "critical",
                                        {"position_id": str(p.id)})
                    report["mismatches"] += 1
                continue
            size = abs(ex.size)
            rules = await provider.instrument(p.asset_id)
            if abs(size - expected) > rules.qty_step:
                await _reconcile_event(session, "position_quantity_mismatch", "warning", None, p,
                                       {"expected": str(expected), "exchange": str(size)})
                if size < expected:
                    p.remaining_quantity = size  # never assume more than the exchange holds
                report["mismatches"] += 1
            if not await provider.open_protection(p.asset_id):
                await _reconcile_event(session, "protection_missing", "critical", None, p,
                                       {"stop": str(_effective_stop(p))})
                if await protect(session, provider, p, rules.round_price(_effective_stop(p)), now):
                    report["protection_replaced"] += 1
        await session.commit()
    report["status"] = "ready" if ready else "insufficient_balance"
    return report
