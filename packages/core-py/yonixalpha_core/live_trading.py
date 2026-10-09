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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, execution_analysis, exit_plan, paper_engine
from yonixalpha_core.db.models import (
    ExecutionOrder, PaperAccount, PaperPosition, PlatformSetting, ReconciliationEvent, TradeTimelineEvent, TradingCandidate,
)
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.store import add_timeline_event, live_trading_permitted
from yonixalpha_core.solana import rent_reclaim
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
# A buy that failed without filling may be tried once more, after a full
# fresh re-evaluation by the gate.
MAX_ENTRY_ATTEMPTS = 2
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
    # Who builds Pump transactions: "native" (built here from on-chain state to
    # the official layouts) or "pumpportal" (third party; always guarded).
    tx_builder: str = "native"
    # Compute-unit limit requested by native Pump transactions. The priority
    # fee stays priority_fee_sol in total; a tighter limit (measured usage:
    # ~96k curve, ~142k PumpSwap) raises the price per CU validators rank
    # by. Defaults are the values the working path has always used.
    compute_unit_limit_curve: int = 200_000
    compute_unit_limit_amm: int = 350_000
    # After a full exit, close the token's now-empty account so its rent
    # deposit (0.0015 SOL per token, measured) returns to the wallet.
    auto_reclaim_rent: bool = True

    def to_dict(self) -> dict[str, str]:
        return {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in asdict(self).items()}


TX_BUILDERS = ("native", "pumpportal")
BOOL_SETTINGS = ("auto_reclaim_rent",)
LIMITS = {"entry_slippage_pct": (Decimal("0.5"), Decimal("50")), "exit_slippage_pct": (Decimal("1"), Decimal("90")),
          "exit_slippage_step_pct": (Decimal("0"), Decimal("30")), "max_exit_slippage_pct": (Decimal("5"), Decimal("95")),
          "priority_fee_sol": (Decimal("0"), Decimal("0.01")), "max_priority_fee_sol": (Decimal("0"), Decimal("0.02")),
          "max_platform_fee_bps": (Decimal("0"), Decimal("200")), "min_sol_reserve": (Decimal("0.01"), Decimal("100")),
          "wallet_max_age_seconds": (Decimal("30"), Decimal("900")),
          # Floors keep headroom over the measured consumption; a limit below
          # what the program needs makes the transaction fail on chain.
          "compute_unit_limit_curve": (Decimal("120000"), Decimal("400000")),
          "compute_unit_limit_amm": (Decimal("180000"), Decimal("600000"))}


BASE_FEE_SOL = Decimal("0.000005")  # per signature
# Rent deposited into the token account a buy opens (170-byte Token-2022
# account, measured by cost_report 2026-09-28); returned only when closed.
TOKEN_ACCOUNT_RENT_SOL = Decimal("0.00151384")


def fixed_trade_costs(live: LiveExecutionSettings) -> tuple[Decimal, dict[str, str]]:
    """SOL a LIVE round trip costs regardless of its size: the network +
    priority fee of the buy and of the sell, plus either the reclaim
    transaction's fee (token account closed after the exit) or the token
    account's rent deposit (left locked when auto-reclaim is off)."""
    trade_fee = BASE_FEE_SOL + live.priority_fee_sol
    parts = {"buy_network_fee": trade_fee, "sell_network_fee": trade_fee}
    if live.auto_reclaim_rent:
        parts["rent_reclaim_fee"] = BASE_FEE_SOL + Decimal(rent_reclaim.PRIORITY_FEE_LAMPORTS) / LAMPORTS
    else:
        parts["token_account_rent_not_reclaimed"] = TOKEN_ACCOUNT_RENT_SOL
    total = sum(parts.values(), Decimal(0))
    return total, {k: str(v) for k, v in parts.items()} | {"total": str(total)}


def parse_live_settings(data: dict[str, Any]) -> tuple[LiveExecutionSettings, list[str]]:
    base = LiveExecutionSettings()
    errors: list[str] = []
    for key, value in (data or {}).items():
        if key in BOOL_SETTINGS:
            if str(value).lower() not in ("true", "false"):
                errors.append(f"{key}: must be true or false")
            else:
                setattr(base, key, str(value).lower() == "true")
            continue
        if key == "tx_builder":
            if value not in TX_BUILDERS:
                errors.append(f"tx_builder: must be one of {list(TX_BUILDERS)}")
            else:
                base.tx_builder = value
            continue
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
        PaperPosition.execution_mode == "LIVE", PaperPosition.execution_provider == LIVE_PROVIDER,
        PaperPosition.asset_id == assessment.asset_id,
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
        diagnostics={"decision": provenance.get("decision")} if provenance.get("decision") else None,
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
        diagnostics={"decision": {"decision_at": now.isoformat(), "approval_at": now.isoformat(), "exit_reason": reason,
                                  "price_sol": str(expected_sol_out / quantity) if expected_sol_out and quantity else None,
                                  "expected_sol_out": str(expected_sol_out) if expected_sol_out is not None else None}},
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


def market_fill_price(order: ExecutionOrder, position: PaperPosition, cost_basis: Decimal) -> tuple[Decimal, str]:
    """The market price of a confirmed BUY: the trade price from our own
    trade event (SOL into the curve/pool per token, before fees), else the
    decision's planned entry price, else the cost basis."""
    price = ((order.diagnostics or {}).get("price") or {}).get("trade_price_sol")
    if price:
        return Decimal(str(price)), "trade price from the program's trade event"
    planned = (position.plan or {}).get("entry_price")
    if planned:
        return Decimal(str(planned)), "planned entry price (no trade event decoded)"
    return cost_basis, "cost basis (no market price recorded)"


def market_reference(p: PaperPosition) -> dict[str, Any]:
    """Market entry price, high and low since entry of a position, all
    MARKET prices. A LIVE position filled before the fill's market price was
    recorded had its high/low seeded from the cost basis: a high equal to
    that seed is not a market price (the planned entry price stands in for
    it, as a lower bound: `high_measured` False), and a low equal to it is
    unknown."""
    plan = p.plan or {}
    fill = plan.get("fill") or {}
    if getattr(p, "execution_mode", "PAPER") != "LIVE" or fill.get("market_price"):
        ref = Decimal(fill["market_price"]) if fill.get("market_price") else p.entry_price
        return {"entry": ref, "high": p.highest_price, "low": p.lowest_price, "high_measured": True,
                "basis": fill.get("market_price_basis") or "entry price"}
    planned = Decimal(str(plan["entry_price"])) if plan.get("entry_price") else None
    seed = p.entry_price
    marked_high = p.highest_price is not None and seed is not None and p.highest_price > seed
    low = p.lowest_price if p.lowest_price is not None and seed is not None and p.lowest_price < seed else None
    return {"entry": planned, "high": p.highest_price if marked_high else planned, "low": low, "high_measured": marked_high,
            "basis": "planned entry price (filled before market fill prices were kept)"}


async def apply_outcome(session: AsyncSession, redis: Redis | None, app_settings: Any, order: ExecutionOrder,
                        outcome: ExecOutcome, now: datetime) -> None:
    """Moves the order and its position from the executor's outcome. Only a
    CONFIRMED outcome with the expected balance change counts as a fill."""
    if order.side == RENT_SIDE:
        await _apply_reclaim(session, redis, app_settings, order, outcome, now)
        return
    order.result = outcome.to_dict()
    order.guard = outcome.guard or order.guard
    order.updated_at = now
    position = await session.get(PaperPosition, order.position_id) if order.position_id else None
    if outcome.status == "CONFIRMED" and outcome.fill is not None:
        order.status = "CONFIRMED"  # the analysis reads the final status; set again below
    try:
        cand = await session.get(TradingCandidate, position.candidate_id) if position is not None and position.candidate_id else None
        order.diagnostics = execution_analysis.analyze(order, position, cand)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block applying a fill
        order.diagnostics = {**(order.diagnostics or {}), "analysis_error": f"{type(exc).__name__}: {exc}"[:300]}
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
            position.entry_price = cost / qty  # cost basis: everything the wallet paid, per token (PnL)
            position.entry_at = now
            # Marks, the high since entry and the low are MARKET prices, so they
            # start from the market price of our fill, never from the cost basis:
            # fees and new-account rent on a small buy can be 30-110% of the
            # trade, and a high seeded from the cost basis read as a crash.
            market, basis = market_fill_price(order, position, cost / qty)
            position.highest_price = position.lowest_price = position.last_price = market
            position.plan = {**(position.plan or {}), "fill": {
                "market_price": str(market), "market_price_basis": basis, "cost_basis_price": str(position.entry_price),
                "costs_sol": ((order.diagnostics or {}).get("price") or {}).get("costs_sol")}}
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
                if (await load_live_settings(session)).auto_reclaim_rent:
                    await request_rent_reclaim(session, now, position.asset_id, "rent_reclaim_after_exit", position.id)
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
        attempt = 1
        if position.candidate_id:
            attempt += (await session.execute(select(func.count()).select_from(TradeTimelineEvent).where(
                TradeTimelineEvent.candidate_id == position.candidate_id,
                TradeTimelineEvent.event_type == "live_entry_failed"))).scalar_one()
            cand = await session.get(TradingCandidate, position.candidate_id)
            if cand is not None and cand.state == CandidateState.ENTRY_PENDING.value:
                if attempt < MAX_ENTRY_ATTEMPTS:
                    # Nothing filled (no tokens arrived): the token goes back to
                    # the gate, which re-checks everything with fresh data
                    # before any new buy. Never a blind resend.
                    apply_transition(cand, CandidateState.ANALYZING,
                                     reason=f"BUY_FAILED (attempt {attempt}/{MAX_ENTRY_ATTEMPTS}): {order.error[:120]} — "
                                            "re-evaluated with fresh data before any retry")
                else:
                    apply_transition(cand, CandidateState.REJECTED,
                                     reason=f"BUY_FAILED (attempt {attempt}/{MAX_ENTRY_ATTEMPTS}, no retries left): "
                                            f"{order.error[:120]}")
        await add_timeline_event(session, "live_entry_failed", now, {"error": order.error, "signature": order.signature,
                                                                     "status": order.status, "attempt": attempt,
                                                                     "code": _failure_code(order)},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
        await events.notify(session, redis, app_settings, "provider_failure", f"LIVE entry failed: {position.symbol}",
                            order.error, "warning", {"position_id": str(position.id)})
    else:
        curve_gone = curve_complete_rejection(order)
        if not curve_gone:
            position.exit_failures += 1  # a wrong-route rejection never widens the next sell's slippage
        await add_timeline_event(session, "live_exit_failed", now, {"reason": order.reason, "error": order.error,
                                                                    "attempt": position.exit_failures,
                                                                    "code": _failure_code(order),
                                                                    "curve_complete": curve_gone},
                                 candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
        if curve_gone:
            await switch_to_pumpswap(session, position, now, "bonding-curve sell rejected: curve complete (Pump 6005)")
        if position.exit_failures >= 2 and await _exit_alert_due(redis, position):
            await events.notify(session, redis, app_settings, "provider_failure",
                                f"LIVE exit failing: {position.symbol} ({order.reason})",
                                f"attempt {position.exit_failures}: {order.error}"
                                f" (at most one alert per position every {EXIT_ALERT_EVERY_SECONDS // 60} min)",
                                "critical", {"position_id": str(position.id)})


EXIT_ALERT_EVERY_SECONDS = 900


async def _exit_alert_due(redis: Redis | None, position: PaperPosition) -> bool:
    """One "LIVE exit failing" alert per position every 15 minutes, in
    Redis so it holds across workers and restarts. 2026-10-07: a sell that
    failed on every try (PumpSwap 6053) alerted on each of 4,843 attempts.
    Without Redis every failure alerts, as before."""
    if redis is None:
        return True
    try:
        return bool(await redis.set(f"yx:live:exit_alert:{position.id}", "1", nx=True, ex=EXIT_ALERT_EVERY_SECONDS))
    except Exception:  # noqa: BLE001 - an alert is never lost to a Redis error
        return True


PUMP_CURVE_COMPLETE = 6005  # Pump program error BondingCurveComplete: the curve migrated to PumpSwap


def curve_complete_rejection(order: ExecutionOrder) -> bool:
    """A SELL on the bonding curve that the Pump program rejected because the
    curve is complete. Seen in production (NEAR, 2026-09-28): the sell failed
    on chain with Custom 6005 and the position only moved to PumpSwap 17 s
    later, when its price source changed; the PumpSwap sell then confirmed."""
    return (order.side == "SELL" and order.route == "pump"
            and f"'Custom': {PUMP_CURVE_COMPLETE}}}" in (order.error or ""))


async def switch_to_pumpswap(session: AsyncSession, position: PaperPosition, now: datetime, why: str) -> None:
    """The same position, now in its post-migration market: the next sell goes
    to the canonical PumpSwap pool and the price comes from that pool."""
    from yonixalpha_core.solana import pumpswap

    if position.lifecycle == "MIGRATED" and position.execution_route == "pump-amm":
        return
    before = position.execution_route
    position.lifecycle = "MIGRATED"
    position.pool = pumpswap.canonical_pool(position.asset_id)
    position.execution_route = "pump-amm"
    await add_timeline_event(session, "position_migrated", now,
                             {"market_state": "POST_MIGRATION", "previous_market_state": "PRE_MIGRATION",
                              "pool": position.pool, "route_before": before, "route_after": "pump-amm",
                              "detected_by": why},
                             candidate_id=position.candidate_id, assessment_id=position.assessment_id,
                             position_id=position.id)


def failure_code_of(side: str, status: str, error: str | None, signature: str | None, result: dict | None) -> str:
    """BUY_/SELL_ + the stage that failed, from what the executor recorded
    (solana.live_exec): build -> guard -> simulate -> submit -> confirm."""
    err = (error or "").lower()
    sent = bool((result or {}).get("sent"))
    stage = (result or {}).get("stage")
    if stage in ("NO_EXECUTABLE_ROUTE", "RPC_UNAVAILABLE", "VENUE_UNSTABLE", "TRANSACTION_BUILD_FAILED", "REQUEST_REJECTED"):
        return f"{side}_{stage}"
    if status == "EXPIRED":
        return f"{side}_CONFIRMATION_TIMEOUT"
    if "guard refused" in err:
        return f"{side}_REFUSED_BY_TRANSACTION_GUARD"
    if "simulation" in err:
        return f"{side}_SIMULATION_FAILED"
    if "failed on chain" in err:
        return f"{side}_FAILED_ON_CHAIN"
    if "could not decode" in err or not signature:
        return f"{side}_TRANSACTION_BUILD_FAILED"
    if not sent:
        return f"{side}_SUBMISSION_FAILED"
    return f"{side}_CONFIRMATION_FAILED"


def _failure_code(order: ExecutionOrder) -> str:
    return failure_code_of(order.side, order.status, order.error, order.signature, order.result)


async def _reconcile_event(session, kind: str, severity: str, order: ExecutionOrder | None, position: PaperPosition | None,
                           detail: dict) -> None:
    mint = order.mint if order else (position.asset_id if position else detail.get("mint"))
    session.add(ReconciliationEvent(kind=kind, severity=severity, mint=mint,
                                    position_id=position.id if position else None, order_id=order.id if order else None,
                                    detail=detail))


OUTSIDE_EXIT = "sold_outside"  # exit_reason of a position closed because its tokens left the wallet elsewhere
IN_FLIGHT = ("SIGNED", "SUBMITTED")


class CloseOutsideRefused(ValueError):
    """The position cannot be closed as sold outside (the wallet still holds
    the token, a sell of ours may be in flight, or it is not a LIVE position)."""


async def close_sold_outside(session: AsyncSession, position: PaperPosition, onchain_raw: int, now: datetime,
                             by: str) -> None:
    """Closes a LIVE position whose tokens left the wallet outside this
    system (sold or sent from a wallet app), on the operator's word and only
    after the caller read the wallet on chain: it must hold none of the
    token (at most DUST_RAW). Nothing is invented: the exit price and the
    realized PnL stay empty (unknown), the SOL of any sell this system made
    earlier (partial take profits) stays in proceeds, and the position's
    open orders are cancelled. Refused while one of our own sells is signed
    or submitted, since it may still land."""
    if position.execution_mode != "LIVE" or position.status not in ("open", "needs_review"):
        raise CloseOutsideRefused(f"only an open or needs_review LIVE position can be closed this way (it is "
                                  f"{position.execution_mode} {position.status})")
    if onchain_raw > DUST_RAW:
        raise CloseOutsideRefused(f"the wallet still holds {onchain_raw} raw units of this token: sell it instead")
    orders = (await session.execute(select(ExecutionOrder).where(
        ExecutionOrder.position_id == position.id, ExecutionOrder.status.in_(("PENDING", *IN_FLIGHT))))).scalars().all()
    if any(o.status in IN_FLIGHT for o in orders):
        raise CloseOutsideRefused("a sell of this position is signed or submitted; wait for it to resolve")
    for o in orders:
        o.status, o.error = "CANCELLED", "position closed as sold outside the system"
    # a position that never held tokens (quantity 0) cannot be "closed"; it ends as failed
    position.status = "closed" if (position.quantity or 0) > 0 else "failed"
    position.exit_reason, position.exit_at = OUTSIDE_EXIT, now
    position.exit_price = position.realized_pnl = position.realized_pnl_pct = None
    position.remaining_quantity = Decimal(0)
    position.exit_requested, position.pending_order_id = False, None
    note = ("tokens left the wallet outside this system (sold or moved in a wallet app); exit price and realized PnL "
            "unknown, never estimated")
    await add_timeline_event(session, "closed_outside", now, {"by": by, "onchain_raw": onchain_raw, "note": note,
                                                              "proceeds_recorded_sol": str(position.proceeds_quote or 0)},
                             candidate_id=position.candidate_id, assessment_id=position.assessment_id, position_id=position.id)
    await _reconcile_event(session, "position_closed_outside", "info", None, position, {"by": by, "onchain_raw": onchain_raw})


RENT_SIDE = "RENT"  # closes the wallet's empty token accounts; their rent deposit returns to the wallet
RENT_ACTIVE = ("PENDING", "SIGNED", "SUBMITTED")
ACTIVE_POSITION_STATES = ("pending_entry", "open", "needs_review")


async def request_rent_reclaim(session: AsyncSession, now: datetime, mint: str | None, reason: str,
                               position_id=None) -> ExecutionOrder | None:
    """Queues closing the wallet's empty token account for `mint` (None: all
    empty token accounts). One active request per scope; None when one is
    already queued."""
    scope = mint or "ALL"
    active = (await session.execute(select(ExecutionOrder.id).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == RENT_SIDE, ExecutionOrder.mint == scope,
        ExecutionOrder.status.in_(RENT_ACTIVE)))).first()
    if active is not None:
        return None
    order = ExecutionOrder(
        position_id=position_id, mode="LIVE", side=RENT_SIDE, reason=reason[:32], mint=scope, provider=LIVE_PROVIDER,
        route="close_accounts", amount="0", amount_kind="accounts", slippage_pct=Decimal(0),
        priority_fee_sol=Decimal(rent_reclaim.PRIORITY_FEE_LAMPORTS) / LAMPORTS,
        limits={"mints": [mint] if mint else None, "max_priority_fee_lamports": rent_reclaim.PRIORITY_FEE_LAMPORTS},
        status="PENDING", idempotency_key=f"rent:{scope}:{int(now.timestamp() * 1000)}")
    session.add(order)
    await session.flush()
    return order


async def _apply_reclaim(session: AsyncSession, redis: Redis | None, app_settings: Any, order: ExecutionOrder,
                         outcome: ExecOutcome, now: datetime) -> None:
    order.result = outcome.to_dict()
    order.guard = outcome.guard or order.guard
    order.updated_at = now
    info = outcome.reclaim or {}
    if outcome.status == "SKIPPED":
        order.status, order.error = "SKIPPED", (outcome.error or "nothing to close")[:500]
        return
    if outcome.status != "CONFIRMED" or outcome.fill is None:
        order.status = "EXPIRED" if outcome.status == "EXPIRED" else "FAILED"
        order.error = (outcome.error or outcome.status)[:500]
        await events.notify(session, redis, app_settings, "provider_failure", "Rent reclaim failed",
                            f"{_failure_code(order)}: {order.error}", "warning", {"order_id": str(order.id)})
        return
    order.status, order.confirmed_at = "CONFIRMED", now
    fill = outcome.fill
    closed = info.get("closing") or []
    per_account_fee = fill.fee_lamports // max(1, len(closed))
    credited = []
    linked = await session.get(PaperPosition, order.position_id) if order.position_id else None
    for acc in closed:
        # The deposit belongs to the latest closed live position of that
        # token not yet credited (the linked one for an after-exit request).
        q = select(PaperPosition).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.asset_id == acc["mint"], PaperPosition.status == "closed")
        if linked is not None and linked.asset_id == acc["mint"]:
            q = q.where(PaperPosition.id == linked.id)
        for p in (await session.execute(q.order_by(PaperPosition.exit_at.desc()))).scalars():
            fill_info = dict((p.plan or {}).get("fill") or {})
            if fill_info.get("rent_reclaimed_sol") is not None:
                continue
            refund = Decimal(int(acc["lamports"]) - per_account_fee) / LAMPORTS
            p.proceeds_quote = (p.proceeds_quote or Decimal(0)) + refund
            p.plan = {**(p.plan or {}), "fill": {**fill_info, "rent_reclaimed_sol": str(refund),
                                                 "rent_reclaim_signature": outcome.signature}}
            await paper_engine.rebook_realized(session, p, now, "token account closed: rent deposit returned",
                                               {"refund_sol": str(refund), "signature": outcome.signature})
            credited.append({"position_id": str(p.id), "symbol": p.symbol, "refund_sol": str(refund)})
            break
    order.result = {**order.result, "credited": credited}
    got = Decimal(fill.sol_change_lamports) / LAMPORTS
    await events.notify(session, redis, app_settings, "close", f"Rent returned: {len(closed)} token account(s) closed",
                        f"+{got} SOL to the wallet (tx {outcome.signature})", "info", {"order_id": str(order.id)})
    await events.publish(redis, "balance.updated", {"account_id": None}, "live")


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
        rent = None
        if order.side == RENT_SIDE:
            busy = set((await session.execute(select(PaperPosition.asset_id).where(
                PaperPosition.execution_mode == "LIVE", PaperPosition.status.in_(ACTIVE_POSITION_STATES)))).scalars())
            mints = (order.limits or {}).get("mints")
            rent = (set(mints) if mints else None, busy, int((order.limits or {}).get("max_priority_fee_lamports") or 0))
        else:
            req, exp = _trade_request(order, executor.wallet.pubkey)
        await session.commit()

    async def on_signed(signature: str) -> None:
        async with session_factory() as s:
            await s.execute(update(ExecutionOrder).where(ExecutionOrder.id == order_id).values(
                signature=signature, status="SIGNED", submitted_at=now_fn(), updated_at=now_fn()))
            await s.commit()

    if rent is not None:
        outcome = await executor.close_token_accounts(rent[0], rent[1], on_signed, rent[2])
    else:
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
    empty: list[dict] = []
    lamports, tokens = await wallet_balances(rpc, executor.wallet.pubkey, empty)
    sol = Decimal(lamports) / LAMPORTS
    report["sol"] = str(sol)
    if redis is not None:
        from yonixalpha_core.solana.valuation import value_holdings

        try:
            valuation = await value_holdings(redis, rpc, tokens, now)
        except Exception as exc:  # noqa: BLE001 - valuation is display-only; reconciliation must go on
            valuation = {"holdings": [], "error": f"valuation failed: {type(exc).__name__}"}
        await redis.set(WALLET_KEY, json.dumps({"sol": str(sol), "at": now.isoformat(), "pubkey": executor.wallet.pubkey,
                                                "tokens": len(tokens), "valuation": valuation,
                                                "empty_token_accounts": {"count": len(empty),
                                                                         "rent_sol": str(Decimal(sum(e["lamports"] for e in empty))
                                                                                         / LAMPORTS)}}), ex=3600)
    async with session_factory() as session:
        acct = await get_live_account(session)
        if acct.cash_balance != sol:
            acct.cash_balance = sol
        if acct.starting_balance <= Decimal("1e-9") and sol > 0:
            acct.starting_balance = sol

        stuck = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == LIVE_PROVIDER,
            ExecutionOrder.status.in_(("SIGNED", "SUBMITTED"))))).scalars().all()
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
            ExecutionOrder.mode == "LIVE", ExecutionOrder.provider == LIVE_PROVIDER, ExecutionOrder.status == "PENDING",
            ExecutionOrder.created_at < now - timedelta(seconds=STALE_PENDING_SECONDS)))).scalars().all()
        for order in stale:
            await apply_outcome(session, redis, app_settings, order,
                                ExecOutcome("FAILED", error="order never picked up by the worker; cancelled as stale"), now)
            order.status = "CANCELLED"

        positions = (await session.execute(select(PaperPosition).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.execution_provider == LIVE_PROVIDER,
            PaperPosition.status == "open"))).scalars().all()
        known = {p.asset_id for p in positions}
        known |= set((await session.execute(select(PaperPosition.asset_id).where(
            PaperPosition.execution_mode == "LIVE", PaperPosition.execution_provider == LIVE_PROVIDER,
            PaperPosition.status.in_(("pending_entry", "needs_review"))))).scalars())
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


@dataclass
class ProtectedExit:
    check: Any
    applied: bool
    decimals: int
    remaining_raw: int

    def to_dict(self) -> dict[str, Any]:
        return {**self.check.to_dict(), "applied": self.applied}


async def _exit_protection(session: AsyncSession, p: PaperPosition, qty: Decimal, reason: str, price: Decimal,
                           now: datetime) -> ProtectedExit | None:
    """Sellable-amount check (exit_plan) for one LIVE sale. Applied only in
    mode PAPER_AND_LIVE; otherwise the would-be change is recorded on the
    timeline (shadow) and the sale goes out as before."""
    decimals = ((p.plan or {}).get("venue") or {}).get("decimals")
    if decimals is None or price is None or price <= 0:
        return None
    dec = int(decimals)
    cfg = await exit_plan.load_settings(session)
    live = await load_live_settings(session)
    remaining_raw = exit_plan.to_raw(p.remaining_quantity or Decimal(0), dec)
    check = exit_plan.check_exit(remaining_raw, exit_plan.to_raw(qty, dec), reason, price / Decimal(10) ** dec,
                                 BASE_FEE_SOL + live.priority_fee_sol, cfg)
    applied = cfg.applies(live=True)
    if check.changed:
        await add_timeline_event(session, f"exit_protection.{check.action.lower()}" + ("" if applied else ".shadow"), now,
                                 {"exit_reason": reason, "applied": applied, **check.to_dict()}, candidate_id=p.candidate_id,
                                 assessment_id=p.assessment_id, position_id=p.id)
    return ProtectedExit(check, applied and check.changed, dec, remaining_raw)


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
    protected = await _exit_protection(session, p, qty, reason, price, now)
    out["exit_protection"] = protected.to_dict() if protected is not None else None
    if protected is not None and protected.applied:
        if protected.check.action == exit_plan.DEFERRED:
            # The level counts as reached; its tokens stay in the position and leave with a later exit.
            idx = int(reason.rsplit("_", 1)[1]) - 1 if reason.startswith("take_profit_") else None
            if idx is not None:
                p.tp_hits = sorted(set((p.tp_hits or []) + [idx]))
            return out
        qty = exit_plan.from_raw(protected.check.sell_raw, protected.decimals) \
            if protected.check.sell_raw < protected.remaining_raw else (p.remaining_quantity or Decimal(0))
    expected = None
    if model is not None:
        c = paper_engine.close_fill(model, qty, "LONG")
        expected = (c.quote - c.fee) if c.complete else None
    else:
        expected = qty * price
    order = await request_live_exit(session, p, qty, reason, expected, now)
    out["requested"] = order.reason if order else None
    return out
