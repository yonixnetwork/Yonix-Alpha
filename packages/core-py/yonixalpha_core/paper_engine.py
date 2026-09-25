"""Paper execution for gate-approved plans: the same TradePlan a live order
would carry, filled against the same liquidity the gate measured (bonding
curve, order book, or measured quote), with fees, price impact and Token-2022
transfer fees on every leg. Handles LONG and SHORT, partial take-profits, an
optional move of the stop to breakeven after TP1, a never-loosening trailing
stop, operator controls (exit now, pause management), MFE/MAE, and explicit
outcome labels for ML.

Accounting, in the paper account's quote currency:
- spot (Solana curve): buying spends the full size (fee included); selling
  returns the net proceeds.
- futures (order book / quote): opening reserves margin = notional / leverage
  plus the entry fee; each close returns that slice of margin plus the
  direction-adjusted PnL minus the exit fee.
In both cases realized PnL = proceeds_quote - entry_cost_quote, so it is net
of every simulated fee and impact.

Pure functions hold the arithmetic; open_position / apply_step persist it.
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
from yonixalpha_core.safety.liquidity import BPS, ConstantProductModel, close_fill, open_fill
from yonixalpha_core.safety.models import ExecutionQuote
from yonixalpha_core.safety.planning import ratchet_trailing_stop
from yonixalpha_core.safety.store import add_timeline_event, marked_value, venue_kind  # noqa: F401 - re-exported
from yonixalpha_core.state_machine import CandidateState, apply_transition

PAPER_SIMULATOR_VERSION = "2.0.0"
LABEL_SOURCE = "paper_engine_realized_pnl"
DUST = Decimal("1e-9")


class FillError(ValueError):
    pass


@dataclass(frozen=True)
class Fill:
    quantity: Decimal  # base units bought or sold
    quote_amount: Decimal  # spot entry: quote spent incl. fee; exits: net quote received / margin returned
    fee_quote: Decimal
    price: Decimal  # effective price, costs included
    reason: str
    impact_bps: Decimal = Decimal(0)


def _sign(side: str) -> Decimal:
    return Decimal(1) if side != "SHORT" else Decimal(-1)


def entry_fill(size_quote: Decimal, model, quote: ExecutionQuote | None, market_price: Decimal,
               entry_cost_bps: Decimal | None, transfer_fee_bps: int | None, side: str = "LONG") -> Fill:
    """Opens `size_quote` of notional. Curve and book models are simulated
    exactly; a measured quote applies the gate's entry cost to the market
    price. A Token-2022 transfer fee reduces tokens received."""
    if size_quote <= 0:
        raise FillError("size must be positive")
    tfee = Decimal(transfer_fee_bps or 0) / BPS
    if model is not None:
        o = open_fill(model, size_quote, side)
        if not o.complete or o.quantity <= 0:
            raise FillError("liquidity could not absorb the order at fill time")
        qty = o.quantity * (1 - tfee)
        if isinstance(model, ConstantProductModel):
            return Fill(qty, size_quote, o.fee, size_quote / qty, "entry", o.impact_bps)
        return Fill(qty, o.quote, o.fee, o.avg_price, "entry", o.impact_bps)
    if quote is not None and entry_cost_bps is not None and market_price > 0:
        if side != "LONG":
            raise FillError("quoted venues are spot; cannot open a short")
        qty = size_quote * (1 - entry_cost_bps / BPS) / market_price * (1 - tfee)
        fee = size_quote * quote.fee_bps_per_side / BPS
        return Fill(qty, size_quote, fee, size_quote / qty, "entry", entry_cost_bps - quote.fee_bps_per_side)
    raise FillError("no liquidity model or measured quote to fill against")


def exit_fill(quantity: Decimal, model, market_price: Decimal | None, exit_cost_bps: Decimal | None,
              transfer_fee_bps: int | None, reason: str, side: str = "LONG") -> Fill:
    """Closes `quantity` now. Returns the gross quote exchanged in
    `quote_amount` for futures (callers turn it into PnL) and net proceeds
    for spot longs."""
    if quantity <= 0:
        raise FillError("quantity must be positive")
    tfee = Decimal(transfer_fee_bps or 0) / BPS
    sold = quantity * (1 - tfee)  # a transfer fee is withheld before the pool sees the tokens
    if model is not None:
        c = close_fill(model, sold, side)
        if not c.complete:
            raise FillError("visible liquidity cannot absorb the exit")
        if isinstance(model, ConstantProductModel):
            net = c.quote - c.fee
            return Fill(quantity, net, c.fee, net / quantity, reason, c.impact_bps)
        return Fill(quantity, c.quote, c.fee, c.avg_price, reason, c.impact_bps)
    if market_price is not None and exit_cost_bps is not None:
        gross = sold * market_price
        cost = gross * exit_cost_bps / BPS
        if side == "LONG":
            return Fill(quantity, gross - cost, cost, (gross - cost) / quantity, reason, exit_cost_bps)
        return Fill(quantity, gross + cost, cost, (gross + cost) / quantity, reason, exit_cost_bps)
    raise FillError("no current price to exit against")


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
    side: str = "LONG"
    move_stop_to_breakeven_at_tp1: bool = False
    breakeven_price: Decimal | None = None
    management_paused: bool = False


@dataclass
class StepResult:
    exits: list[tuple[Decimal, str]] = field(default_factory=list)  # (quantity, reason)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False


def _beyond(price: Decimal, level: Decimal, side: str) -> bool:
    """Has price reached a favourable level (TP / activation)?"""
    return price >= level if side != "SHORT" else price <= level


def _stopped(price: Decimal, stop: Decimal, side: str) -> bool:
    return price <= stop if side != "SHORT" else price >= stop


def manage_step(s: PositionState, price: Decimal, exit_now: bool = False) -> StepResult:
    """One management tick at the current market price. Mutates `s`; returns
    exits to fill and timeline events. Order: operator exit, then the stop
    (a gap through both stop and TP must not book the TP), then take-profits,
    then the breakeven move and trailing ratchet. A paused position still
    honours its stop — risk is never left undefined — but takes no profits
    and doesn't trail."""
    r = StepResult()
    side = s.side
    s.highest_price = price if s.highest_price is None else max(s.highest_price, price)
    s.lowest_price = price if s.lowest_price is None else min(s.lowest_price, price)

    if exit_now and s.remaining_quantity > 0:
        r.exits.append((s.remaining_quantity, "manual_exit"))
        r.events.append(("manual_exit", {"price": str(price)}))
        s.remaining_quantity = Decimal(0)
        r.closed = True
        return r

    trailing_active = s.trailing_stop is not None
    if trailing_active:
        effective = max(s.stop_loss, s.trailing_stop) if side != "SHORT" else min(s.stop_loss, s.trailing_stop)
    else:
        effective = s.stop_loss
    if _stopped(price, effective, side):
        by_trail = trailing_active and effective == s.trailing_stop and s.trailing_stop != s.stop_loss
        reason = "trailing_stop" if by_trail else "stop_loss"
        r.exits.append((s.remaining_quantity, reason))
        r.events.append((reason, {"price": str(price), "stop": str(effective)}))
        s.remaining_quantity = Decimal(0)
        r.closed = True
        return r

    if s.management_paused:
        return r

    for i, (tp_price, fraction) in enumerate(s.take_profits):
        if i in s.tp_hits or not _beyond(price, tp_price, side):
            continue
        qty = min(s.initial_quantity * fraction, s.remaining_quantity)
        s.tp_hits.append(i)
        if qty > 0:
            r.exits.append((qty, f"take_profit_{i + 1}"))
            s.remaining_quantity -= qty
        r.events.append((f"take_profit_{i + 1}", {"price": str(price), "level": str(tp_price), "quantity": str(qty)}))
        if i == 0 and s.move_stop_to_breakeven_at_tp1 and s.breakeven_price is not None:
            tighter = max(s.stop_loss, s.breakeven_price) if side != "SHORT" else min(s.stop_loss, s.breakeven_price)
            if tighter != s.stop_loss:
                s.stop_loss = tighter
                r.events.append(("stop_to_breakeven", {"stop": str(tighter)}))

    if (s.trailing_enabled and s.trailing_distance_pct is not None and s.trailing_stop is None
            and s.trailing_activation_price is not None and _beyond(price, s.trailing_activation_price, side)):
        s.trailing_stop = ratchet_trailing_stop(None, price, s.trailing_distance_pct, side)
        r.events.append(("trailing_activated", {"price": str(price), "stop": str(s.trailing_stop)}))
    elif s.trailing_stop is not None and s.trailing_distance_pct is not None:
        new = ratchet_trailing_stop(s.trailing_stop, price, s.trailing_distance_pct, side)
        if new != s.trailing_stop:
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
        side=p.side if p.side in ("LONG", "SHORT") else "LONG",
        move_stop_to_breakeven_at_tp1=bool(plan.get("move_stop_to_breakeven_at_tp1")),
        breakeven_price=Decimal(plan["breakeven_price"]) if plan.get("breakeven_price") else None,
        management_paused=bool(getattr(p, "management_paused", False)),
    )


async def open_position(
    session: AsyncSession,
    account: PaperAccount,
    assessment: Assessment,
    assessment_id: uuid.UUID,
    candidate: TradingCandidate | None,
    model,
    quote: ExecutionQuote | None,
    transfer_fee_bps: int | None,
    now: datetime,
    venue: dict[str, Any] | None = None,
    fill_model=None,
    max_slippage_bps: Decimal | None = None,
) -> PaperPosition:
    """Opens the paper position an executable assessment describes. Refuses
    anything the gate didn't clear for PAPER, and never spends more than the
    account's cash. `fill_model` is the liquidity at fill time (defaults to
    the assessed model); if the fill's impact exceeds what the plan priced in
    by more than `max_slippage_bps`, the order fails like a real one with a
    slippage limit would. Caller commits."""
    plan = assessment.plan
    if not assessment.executable or assessment.execution_target.value != "PAPER" or not plan.complete:
        raise FillError("assessment is not an executable paper plan")
    side = plan.side
    size = plan.position_size.value
    kind = (venue or {}).get("kind") or ("spot" if side == "LONG" and (model is None or isinstance(model, ConstantProductModel)) else "futures")
    fill = entry_fill(size, fill_model or model, quote, plan.entry_price, plan.entry_cost_bps, transfer_fee_bps, side)

    if max_slippage_bps is not None and plan.entry_cost_bps is not None:
        fee_bps = getattr(fill_model or model, "fee_bps", Decimal(0)) or Decimal(0)
        priced_impact = plan.entry_cost_bps - fee_bps
        if fill.impact_bps - priced_impact > max_slippage_bps:
            raise FillError(
                f"fill impact {fill.impact_bps / 100:.2f}% exceeds planned {priced_impact / 100:.2f}% by more than the "
                f"{max_slippage_bps / 100:.2f}% slippage limit — order not filled"
            )

    if kind == "futures":
        leverage = plan.leverage if plan.leverage and plan.leverage > 0 else Decimal(1)
        margin = fill.quote_amount / leverage
        cost = margin + fill.fee_quote
    else:
        margin = Decimal(0)
        cost = size
    if cost > account.cash_balance:
        raise FillError(f"required {cost} exceeds paper cash {account.cash_balance}")
    account.cash_balance -= cost

    first_tp = plan.take_profits[0].price.value if plan.take_profits else None
    venue_info = {**(venue or {}), "kind": kind, "transfer_fee_bps": transfer_fee_bps, "margin": str(margin),
                  "notional": str(fill.quote_amount), "simulator": PAPER_SIMULATOR_VERSION}
    position = PaperPosition(
        candidate_id=candidate.id if candidate else None,
        symbol=assessment.symbol[:64],
        provider="paper",
        side=side,
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
        entry_cost_quote=cost,
        proceeds_quote=Decimal(0),
        fees_paid_quote=fill.fee_quote,
        max_loss_quote=plan.max_loss.value,
        plan={**plan.to_dict(), "venue": venue_info},
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
        session, "entry_submitted", now, {"size": str(size), "side": side, "market_price": str(plan.entry_price)},
        candidate_id=candidate.id if candidate else None, assessment_id=assessment_id, position_id=position.id,
    )
    await add_timeline_event(
        session, "paper_entry", now,
        {"size": str(size), "side": side, "quantity": str(fill.quantity), "fill_price": str(fill.price),
         "market_price": str(plan.entry_price), "fee": str(fill.fee_quote), "impact_bps": str(fill.impact_bps),
         "stop": str(plan.stop_loss.value), "first_tp": str(first_tp) if first_tp else None, "kind": kind},
        candidate_id=candidate.id if candidate else None, assessment_id=assessment_id, position_id=position.id,
    )
    return position


def outcome_labels(p: PaperPosition) -> dict[str, Any]:
    """Explicit, reproducible outcome of a closed position (spec: ML labels).
    Percentages are relative to the entry fill price, signed so that
    favourable moves are positive for either side."""
    sign = _sign(p.side)
    entry = p.entry_price
    mfe = mae = None
    if entry and p.highest_price is not None and p.lowest_price is not None:
        fav, adv = (p.highest_price, p.lowest_price) if sign > 0 else (p.lowest_price, p.highest_price)
        mfe = sign * (fav / entry - 1)
        mae = sign * (adv / entry - 1)
    hits = set(p.tp_hits or [])
    duration = (p.exit_at - p.entry_at).total_seconds() if p.exit_at and p.entry_at else None
    return {
        "profitable": bool(p.realized_pnl is not None and p.realized_pnl > 0),
        "return_pct": str(p.realized_pnl_pct) if p.realized_pnl_pct is not None else None,
        "tp1": 0 in hits, "tp2": 1 in hits, "tp3": 2 in hits,
        "stop_loss": p.exit_reason == "stop_loss",
        "trailing_exit": p.exit_reason == "trailing_stop",
        "manual_exit": p.exit_reason == "manual_exit",
        "exit_reason": p.exit_reason,
        "mfe_pct": str(mfe) if mfe is not None else None,
        "mae_pct": str(mae) if mae is not None else None,
        "duration_seconds": duration,
        "fees": str(p.fees_paid_quote) if p.fees_paid_quote is not None else None,
        "simulator": PAPER_SIMULATOR_VERSION,
    }


async def apply_step(
    session: AsyncSession,
    position: PaperPosition,
    account: PaperAccount,
    price: Decimal,
    model,
    transfer_fee_bps: int | None,
    now: datetime,
    exit_cost_bps: Decimal | None = None,
    extra_exit: tuple[Decimal, str] | None = None,
) -> StepResult:
    """Marks `position` at `price`, fills whatever manage_step (or an
    operator exit request / an exit-intelligence REDUCE in `extra_exit`)
    triggers, and on full exit books realized PnL, writes explicit outcome
    labels to the candidate's ML rows, and closes the candidate. Without a
    model, exits are charged `exit_cost_bps` (default: the plan's estimate);
    pass 0 when `price` is already an effective price from a real sell quote.
    Caller commits."""
    if position.status != "open":
        return StepResult()
    s = state_of(position)
    side = s.side
    exit_now = bool(getattr(position, "exit_requested", False))
    result = manage_step(s, price, exit_now=exit_now)
    if extra_exit and not result.closed:
        qty = min(extra_exit[0], s.remaining_quantity)
        if qty > 0:
            result.exits.append((qty, extra_exit[1]))
            s.remaining_quantity -= qty
            result.events.append((extra_exit[1], {"price": str(price), "quantity": str(qty)}))
            if s.remaining_quantity <= DUST:
                s.remaining_quantity = Decimal(0)
                result.closed = True

    if model is not None:
        exit_cost = None
    elif exit_cost_bps is not None:
        exit_cost = exit_cost_bps
    else:
        exit_cost = Decimal((position.plan or {}).get("exit_cost_bps") or 0)
    kind = venue_kind(position)
    venue = (position.plan or {}).get("venue") or {}
    margin_total = Decimal(venue.get("margin", "0"))
    initial = position.initial_quantity or position.quantity

    for qty, reason in result.exits:
        fill = exit_fill(qty, model, price, exit_cost, transfer_fee_bps, reason, side)
        if kind == "futures":
            if model is not None:
                exit_px, fee = fill.price, fill.fee_quote
            else:
                exit_px, fee = price, qty * price * (exit_cost or Decimal(0)) / BPS
            pnl = _sign(side) * (exit_px - position.entry_price) * qty
            returned = margin_total * qty / initial + pnl - fee
            fees_add = fee
        else:
            returned = fill.quote_amount
            fees_add = fill.fee_quote
            if model is not None and isinstance(model, ConstantProductModel):
                gross = fill.quote_amount + fill.fee_quote
                model = ConstantProductModel(
                    model.quote_reserve - gross, model.token_reserve + qty, model.fee_bps,
                    (model.real_quote_reserve - gross) if model.real_quote_reserve is not None else None,
                )
        account.cash_balance += returned
        position.proceeds_quote = (position.proceeds_quote or Decimal(0)) + returned
        position.fees_paid_quote = (position.fees_paid_quote or Decimal(0)) + fees_add
        await add_timeline_event(session, f"paper_exit.{reason}", now,
                                 {"quantity": str(qty), "returned": str(returned), "fill_price": str(fill.price),
                                  "impact_bps": str(fill.impact_bps)},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
    for kind_evt, detail in result.events:
        if kind_evt.startswith("trailing") or kind_evt == "stop_to_breakeven":
            await add_timeline_event(session, kind_evt, now, detail, candidate_id=position.candidate_id,
                                     assessment_id=position.assessment_id, position_id=position.id)

    position.remaining_quantity = s.remaining_quantity
    position.tp_hits = s.tp_hits
    position.trailing_stop = s.trailing_stop
    position.stop_loss = s.stop_loss
    position.highest_price = s.highest_price
    position.lowest_price = s.lowest_price
    position.last_price = price
    position.last_marked_at = now

    if result.closed:
        await close_position(session, position, now, result.exits[-1][1] if result.exits else "closed", price)
    return result


async def close_position(session: AsyncSession, position: PaperPosition, now: datetime, reason: str, price: Decimal | None) -> None:
    """Books realized PnL (proceeds − entry cost, both actual), writes the
    explicit ML outcome labels and closes the candidate. Shared by the paper
    simulator and live execution, so both record outcomes identically."""
    position.status = "closed"
    position.exit_at = now
    position.exit_reason = reason
    position.exit_price = price
    position.realized_pnl = (position.proceeds_quote or Decimal(0)) - (position.entry_cost_quote or Decimal(0))
    position.realized_pnl_pct = position.realized_pnl / position.entry_cost_quote if position.entry_cost_quote else None
    labels = outcome_labels(position)
    source = LABEL_SOURCE if getattr(position, "execution_mode", "PAPER") != "LIVE" else "live_execution_realized_pnl"
    if position.candidate_id is not None:
        await session.execute(
            update(MLFeatureSnapshot)
            .where(MLFeatureSnapshot.candidate_id == position.candidate_id, MLFeatureSnapshot.label.is_(None))
            .values(label=1 if position.realized_pnl > 0 else 0, label_source=source, outcome=labels)
        )
        candidate = await session.get(TradingCandidate, position.candidate_id)
        if candidate is not None and candidate.state == CandidateState.MANAGING.value:
            apply_transition(candidate, CandidateState.EXIT_SIGNAL, reason=position.exit_reason)
            apply_transition(candidate, CandidateState.EXITING, reason=position.exit_reason)
            apply_transition(candidate, CandidateState.CLOSED,
                             reason=f"{position.execution_mode.lower() if getattr(position, 'execution_mode', None) else 'paper'} closed: "
                                    f"{position.exit_reason}, pnl {position.realized_pnl:.6f}")
    elif position.assessment_id is not None:
        await session.execute(
            update(MLFeatureSnapshot)
            .where(MLFeatureSnapshot.assessment_id == position.assessment_id, MLFeatureSnapshot.label.is_(None))
            .values(label=1 if position.realized_pnl > 0 else 0, label_source=source, outcome=labels)
        )
    await add_timeline_event(session, "live_closed" if getattr(position, "execution_mode", "PAPER") == "LIVE" else "paper_closed", now,
                             {"realized_pnl": str(position.realized_pnl), "pnl_pct": str(position.realized_pnl_pct),
                              "fees": str(position.fees_paid_quote), "mfe_price": str(position.highest_price),
                              "mae_price": str(position.lowest_price), "outcome": labels},
                             candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
