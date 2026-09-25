"""Paper execution for gate-approved plans: the same TradePlan a live order
would carry, filled against the same liquidity model or quote the gate
measured, with fees, price impact and Token-2022 transfer fees applied on
every leg. Partial take-profits, a never-loosening trailing stop, and
MFE/MAE tracking are managed here.

Pure functions (entry_fill, exit_fill, manage_step) hold the arithmetic;
open_position / apply_step persist it. Amounts are in the paper account's
quote currency (SOL for the Solana engines); quantities are whole tokens.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MLFeatureSnapshot, PaperAccount, PaperPosition, TradingCandidate
from yonixalpha_core.safety.gate import Assessment
from yonixalpha_core.safety.liquidity import BPS, ConstantProductModel
from yonixalpha_core.safety.models import ExecutionQuote
from yonixalpha_core.safety.planning import ratchet_trailing_stop
from yonixalpha_core.safety.store import add_timeline_event
from yonixalpha_core.state_machine import CandidateState, apply_transition

LABEL_SOURCE = "paper_engine_realized_pnl"
DUST = Decimal("1e-9")


class FillError(ValueError):
    pass


@dataclass(frozen=True)
class Fill:
    quantity: Decimal  # tokens bought or sold
    quote_amount: Decimal  # quote spent (entry) or received net (exit)
    fee_quote: Decimal
    price: Decimal  # effective quote per token, costs included
    reason: str


def entry_fill(size_quote: Decimal, model: ConstantProductModel | None, quote: ExecutionQuote | None,
               market_price: Decimal, entry_cost_bps: Decimal | None, transfer_fee_bps: int | None) -> Fill:
    """Tokens received for `size_quote`. The curve model is exact for
    pump.fun; for a quoted venue the gate's measured entry cost is applied
    to the market price. A Token-2022 transfer fee reduces tokens received."""
    if size_quote <= 0:
        raise FillError("size must be positive")
    tfee = Decimal(transfer_fee_bps or 0) / BPS
    if model is not None:
        swap = model.simulate_buy(size_quote)
        tokens = swap.amount_out * (1 - tfee)
        fee = swap.fee_paid
    elif quote is not None and entry_cost_bps is not None and market_price > 0:
        tokens = size_quote * (1 - entry_cost_bps / BPS) / market_price * (1 - tfee)
        fee = size_quote * quote.fee_bps_per_side / BPS
    else:
        raise FillError("no liquidity model or measured quote to fill against")
    if tokens <= 0:
        raise FillError("fill would receive no tokens")
    return Fill(tokens, size_quote, fee, size_quote / tokens, "entry")


def exit_fill(quantity: Decimal, model: ConstantProductModel | None, market_price: Decimal | None,
              exit_cost_bps: Decimal | None, transfer_fee_bps: int | None, reason: str) -> Fill:
    """Quote received for selling `quantity` now. With a current curve
    model the sale is simulated exactly against present reserves; otherwise
    the plan's estimated exit cost is charged against the market price."""
    if quantity <= 0:
        raise FillError("quantity must be positive")
    tfee = Decimal(transfer_fee_bps or 0) / BPS
    sold = quantity * (1 - tfee)  # the transfer fee is withheld from what reaches the pool
    if model is not None:
        swap = model.simulate_sell(sold)
        proceeds, fee = swap.amount_out, swap.fee_paid
    elif market_price is not None and exit_cost_bps is not None:
        gross = sold * market_price
        proceeds = gross * (1 - exit_cost_bps / BPS)
        fee = gross - proceeds
    else:
        raise FillError("no current price to exit against")
    return Fill(quantity, proceeds, fee, proceeds / quantity, reason)


@dataclass
class PositionState:
    initial_quantity: Decimal
    remaining_quantity: Decimal
    stop_loss: Decimal
    take_profits: list[tuple[Decimal, Decimal]]  # (price, fraction of initial quantity)
    tp_hits: list[int]
    trailing_enabled: bool
    trailing_distance_pct: Decimal | None
    trailing_activation_price: Decimal | None
    trailing_stop: Decimal | None
    highest_price: Decimal | None
    lowest_price: Decimal | None


@dataclass
class StepResult:
    exits: list[tuple[Decimal, str]] = field(default_factory=list)  # (quantity, reason)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False


def manage_step(s: PositionState, price: Decimal) -> StepResult:
    """One management tick for a long position at the current market price.
    Mutates `s`; returns the exits to fill and timeline events. Order: stop
    first (a gap through both stop and TP must not book the TP), then
    take-profits in order, then trailing ratchet."""
    r = StepResult()
    s.highest_price = price if s.highest_price is None else max(s.highest_price, price)
    s.lowest_price = price if s.lowest_price is None else min(s.lowest_price, price)

    trailing_active = s.trailing_stop is not None
    effective_stop = max(s.stop_loss, s.trailing_stop) if trailing_active else s.stop_loss
    if price <= effective_stop:
        reason = "trailing_stop" if trailing_active and s.trailing_stop >= s.stop_loss else "stop_loss"
        r.exits.append((s.remaining_quantity, reason))
        r.events.append((reason, {"price": str(price), "stop": str(effective_stop)}))
        s.remaining_quantity = Decimal(0)
        r.closed = True
        return r

    for i, (tp_price, fraction) in enumerate(s.take_profits):
        if i in s.tp_hits or price < tp_price:
            continue
        qty = min(s.initial_quantity * fraction, s.remaining_quantity)
        s.tp_hits.append(i)
        if qty > 0:
            r.exits.append((qty, f"take_profit_{i + 1}"))
            s.remaining_quantity -= qty
        r.events.append((f"take_profit_{i + 1}", {"price": str(price), "level": str(tp_price), "quantity": str(qty)}))

    if (s.trailing_enabled and s.trailing_distance_pct is not None and s.trailing_stop is None
            and s.trailing_activation_price is not None and price >= s.trailing_activation_price):
        s.trailing_stop = ratchet_trailing_stop(None, price, s.trailing_distance_pct)
        r.events.append(("trailing_activated", {"price": str(price), "stop": str(s.trailing_stop)}))
    elif s.trailing_stop is not None and s.trailing_distance_pct is not None:
        new = ratchet_trailing_stop(s.trailing_stop, price, s.trailing_distance_pct)
        if new > s.trailing_stop:
            s.trailing_stop = new
            r.events.append(("trailing_moved", {"price": str(price), "stop": str(new)}))

    if s.remaining_quantity <= DUST:
        s.remaining_quantity = Decimal(0)
        r.closed = True
    return r


def state_of(p: PaperPosition) -> PositionState:
    plan = p.plan or {}
    tps = [(Decimal(tp["price"]["value"]), Decimal(tp["exit_fraction"])) for tp in plan.get("take_profits", [])]
    trailing = plan.get("trailing") or {}
    return PositionState(
        initial_quantity=p.initial_quantity if p.initial_quantity is not None else p.quantity,
        remaining_quantity=p.remaining_quantity if p.remaining_quantity is not None else p.quantity,
        stop_loss=p.stop_loss,
        take_profits=tps,
        tp_hits=list(p.tp_hits or []),
        trailing_enabled=bool(trailing.get("enabled")),
        trailing_distance_pct=Decimal(trailing["distance_pct"]) if trailing.get("distance_pct") else None,
        trailing_activation_price=Decimal(trailing["activation_price"]) if trailing.get("activation_price") else None,
        trailing_stop=p.trailing_stop,
        highest_price=p.highest_price,
        lowest_price=p.lowest_price,
    )


async def open_position(
    session: AsyncSession,
    account: PaperAccount,
    assessment: Assessment,
    assessment_id: uuid.UUID,
    candidate: TradingCandidate | None,
    model: ConstantProductModel | None,
    quote: ExecutionQuote | None,
    transfer_fee_bps: int | None,
    now: datetime,
    venue: dict[str, Any] | None = None,
) -> PaperPosition:
    """Opens the paper position an executable assessment describes. Refuses
    anything the gate didn't clear for PAPER, and never spends more than the
    account's cash. Caller commits."""
    plan = assessment.plan
    if not assessment.executable or assessment.execution_target.value != "PAPER" or not plan.complete:
        raise FillError("assessment is not an executable paper plan")
    size = plan.position_size.value
    if size > account.cash_balance:
        raise FillError(f"size {size} exceeds paper cash {account.cash_balance}")
    fill = entry_fill(size, model, quote, plan.entry_price, plan.entry_cost_bps, transfer_fee_bps)
    account.cash_balance -= size
    first_tp = plan.take_profits[0].price.value if plan.take_profits else None
    position = PaperPosition(
        candidate_id=candidate.id if candidate else None,
        symbol=assessment.symbol[:64],
        provider="paper",
        side="LONG",
        entry_price=fill.price,
        quantity=fill.quantity,
        stop_loss=plan.stop_loss.value,
        take_profit=[str(tp.price.value) for tp in plan.take_profits],
        entry_at=now,
        status="open",
        account_id=account.id,
        assessment_id=assessment_id,
        engine=assessment.engine,
        asset_id=assessment.asset_id,
        initial_quantity=fill.quantity,
        remaining_quantity=fill.quantity,
        entry_cost_quote=size,
        proceeds_quote=Decimal(0),
        fees_paid_quote=fill.fee_quote,
        max_loss_quote=plan.max_loss.value,
        # The venue (curve vs quoted route, token decimals, transfer fee) is
        # kept with the plan so exits are priced the same way entry was.
        plan={**plan.to_dict(), "venue": {**(venue or {}), "transfer_fee_bps": transfer_fee_bps}},
        tp_hits=[],
        highest_price=plan.entry_price,
        lowest_price=plan.entry_price,
        last_price=plan.entry_price,
        last_marked_at=now,
    )
    session.add(position)
    await session.flush()
    if candidate is not None:
        apply_transition(candidate, CandidateState.QUALIFIED, reason="safety gate: " + assessment.status_label)
        apply_transition(candidate, CandidateState.ENTRY_PENDING, reason=f"paper entry {size} {account.quote_currency}")
        apply_transition(candidate, CandidateState.ENTERED, reason=f"paper fill {fill.quantity} @ {fill.price}")
        apply_transition(candidate, CandidateState.MANAGING, reason="position open")
    await add_timeline_event(
        session, "paper_entry", now,
        {"size": str(size), "quantity": str(fill.quantity), "fill_price": str(fill.price), "market_price": str(plan.entry_price),
         "fee": str(fill.fee_quote), "stop": str(plan.stop_loss.value), "first_tp": str(first_tp) if first_tp else None},
        candidate_id=candidate.id if candidate else None, assessment_id=assessment_id, position_id=position.id,
    )
    return position


async def apply_step(
    session: AsyncSession,
    position: PaperPosition,
    account: PaperAccount,
    price: Decimal,
    model: ConstantProductModel | None,
    transfer_fee_bps: int | None,
    now: datetime,
    exit_cost_bps: Decimal | None = None,
) -> StepResult:
    """Marks `position` at `price`, fills whatever manage_step triggers, and
    on full exit books realized PnL, labels the candidate's ML rows, and
    closes the candidate. Without a model, exits are charged
    `exit_cost_bps` (default: the plan's estimate); pass 0 when `price` is
    already an effective price from a real sell quote. Caller commits."""
    s = state_of(position)
    result = manage_step(s, price)
    if model is not None:
        exit_cost = None
    elif exit_cost_bps is not None:
        exit_cost = exit_cost_bps
    else:
        exit_cost = Decimal((position.plan or {}).get("exit_cost_bps") or 0)
    for qty, reason in result.exits:
        # Each partial sale moves the reserves; later fills in the same tick
        # use the updated model so impact isn't under-counted.
        fill = exit_fill(qty, model, price, exit_cost, transfer_fee_bps, reason)
        if model is not None:
            model = ConstantProductModel(
                model.quote_reserve - (fill.quote_amount + fill.fee_quote),
                model.token_reserve + qty,
                model.fee_bps,
                (model.real_quote_reserve - fill.quote_amount - fill.fee_quote) if model.real_quote_reserve is not None else None,
            )
        account.cash_balance += fill.quote_amount
        position.proceeds_quote = (position.proceeds_quote or Decimal(0)) + fill.quote_amount
        position.fees_paid_quote = (position.fees_paid_quote or Decimal(0)) + fill.fee_quote
        await add_timeline_event(session, f"paper_exit.{reason}", now,
                                 {"quantity": str(qty), "proceeds": str(fill.quote_amount), "fill_price": str(fill.price)},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
    for kind, detail in result.events:
        if kind.startswith("trailing"):
            await add_timeline_event(session, kind, now, detail, candidate_id=position.candidate_id,
                                     assessment_id=position.assessment_id, position_id=position.id)

    position.remaining_quantity = s.remaining_quantity
    position.tp_hits = s.tp_hits
    position.trailing_stop = s.trailing_stop
    position.highest_price = s.highest_price
    position.lowest_price = s.lowest_price
    position.last_price = price
    position.last_marked_at = now

    if result.closed:
        position.status = "closed"
        position.exit_at = now
        position.exit_reason = result.exits[-1][1] if result.exits else "closed"
        position.exit_price = price
        position.realized_pnl = position.proceeds_quote - position.entry_cost_quote
        position.realized_pnl_pct = position.realized_pnl / position.entry_cost_quote if position.entry_cost_quote else None
        if position.candidate_id is not None:
            await session.execute(
                update(MLFeatureSnapshot)
                .where(MLFeatureSnapshot.candidate_id == position.candidate_id, MLFeatureSnapshot.label.is_(None))
                .values(label=1 if position.realized_pnl > 0 else 0, label_source=LABEL_SOURCE)
            )
            candidate = await session.get(TradingCandidate, position.candidate_id)
            if candidate is not None and candidate.state == CandidateState.MANAGING.value:
                apply_transition(candidate, CandidateState.EXIT_SIGNAL, reason=position.exit_reason)
                apply_transition(candidate, CandidateState.EXITING, reason=position.exit_reason)
                apply_transition(candidate, CandidateState.CLOSED,
                                 reason=f"paper closed: {position.exit_reason}, pnl {position.realized_pnl:.6f}")
        await add_timeline_event(session, "paper_closed", now,
                                 {"realized_pnl": str(position.realized_pnl), "pnl_pct": str(position.realized_pnl_pct),
                                  "fees": str(position.fees_paid_quote), "mfe_price": str(position.highest_price),
                                  "mae_price": str(position.lowest_price)},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
    return result
