"""LIVE_EXECUTION_SMOKE_TEST: verifies the real execution path with a tiny
amount of SOL. An execution verification tool, not a strategy.

  wallet -> buy -> transaction confirmation -> position -> live price -> PnL
         -> exit decision -> sell -> transaction confirmation -> closed

Two independent switches, both required:
  1. LIVE_SMOKE_TEST_ENABLED=true in the server's .env (not editable from the
     dashboard), with LIVE_SMOKE_TEST_MAX_SOL set (no default) and
     LIVE_SMOKE_TEST_MAX_TRADES buys left;
  2. an admin arms ONE run on the Live page with the admin password and the
     typed phrase CONFIRM_PHRASE. A run expires after its time limit.
The three environment locks and a ready live order worker are required as for
any live order. The global mode is never changed: normal trading stays PAPER.

While a run is armed, the decision engine takes the first candidate of the
run's category (FRESH / MIGRATED / MOMENTUM) that has a BUY signal and
re-assesses it with the FULL safety gate against the LIVE wallet (balance
minus min_sol_reserve), with max_position_size capped at the run's max_sol.
Nothing is forced: a candidate the gate does not approve is recorded as an
attempt with its stage (SAFETY_REJECTED, RISK_REJECTED, MARKET_DATA_UNAVAILABLE,
EXECUTION_ROUTE_UNAVAILABLE) and no buy happens; a run that ends without an
approved candidate is NO_TEST_EXECUTION_CANDIDATE. An approved one goes through
live_trading.enter_live -> the existing order worker (PumpPortal trade-local
-> transaction guard -> sign -> simulate -> send -> confirm -> balance-change
fill), and the position is managed and sold by the existing exit system. The
test-close control only sets the position's exit_requested flag, the same
operator exit every position has.

Status is never stored as a claim: run_view derives every stage from the real
execution_orders / paper_positions rows (ORDER_SUBMITTED needs a signed and
sent transaction, TRANSACTION_CONFIRMED a confirmed signature, ACTUALLY_FILLED
the wallet balance change).
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import live_trading
from yonixalpha_core.db.models import ExecutionOrder, LiveSmokeTest, PaperPosition
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import GlobalMode, StrategyMode

CATEGORY_ENGINE = {"FRESH": "solana_fresh", "MIGRATED": "solana_migration", "MOMENTUM": "solana_momentum"}
CONFIRM_PHRASE = "SPEND REAL SOL"
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 5, 120, 30
MAX_ATTEMPTS_KEPT = 50
LAMPORTS = Decimal(1_000_000_000)

ROUTE_CODES = {"NO_BUY_ROUTE", "NO_SELL_ROUTE", "ROUTE_UNVERIFIED", "EXECUTION_UNAVAILABLE", "LIVE_NOT_READY",
               "LIVE_NOT_PERMITTED"}
# executor failure (live_trading.failure_code_of suffix) -> smoke-test stage
FAILURE_STAGE = {
    "TRANSACTION_BUILD_FAILED": "QUOTE_FAILED",
    "NO_EXECUTABLE_ROUTE": "QUOTE_FAILED",
    "RPC_UNAVAILABLE": "SUBMISSION_FAILED",
    "VENUE_UNSTABLE": "QUOTE_FAILED",
    "REQUEST_REJECTED": "TRANSACTION_REJECTED",
    "REFUSED_BY_TRANSACTION_GUARD": "TRANSACTION_REJECTED",
    "SIMULATION_FAILED": "TRANSACTION_REJECTED",
    "FAILED_ON_CHAIN": "TRANSACTION_REJECTED",
    "SUBMISSION_FAILED": "SUBMISSION_FAILED",
    "CONFIRMATION_TIMEOUT": "TRANSACTION_UNCONFIRMED",
    "CONFIRMATION_FAILED": "TRANSACTION_UNCONFIRMED",
}


class SmokeRefused(Exception):
    def __init__(self, stage: str, reason: str):
        super().__init__(reason)
        self.stage, self.reason = stage, reason


def config_status(app_settings: Any) -> dict[str, Any]:
    """The server-side switch and limits, and why arming is impossible."""
    enabled = bool(getattr(app_settings, "LIVE_SMOKE_TEST_ENABLED", False))
    max_sol = getattr(app_settings, "LIVE_SMOKE_TEST_MAX_SOL", None)
    max_trades = int(getattr(app_settings, "LIVE_SMOKE_TEST_MAX_TRADES", 0) or 0)
    problems = []
    if not enabled:
        problems.append("LIVE_SMOKE_TEST_ENABLED is false in the server's .env")
    if max_sol is None or Decimal(str(max_sol)) <= 0:
        problems.append("LIVE_SMOKE_TEST_MAX_SOL is not set (there is no default)")
    if max_trades < 1:
        problems.append("LIVE_SMOKE_TEST_MAX_TRADES is below 1")
    if not store.live_trading_permitted(app_settings):
        problems.append("environment locks closed (TRADING_ENABLED, LIVE_TRADING_ENABLED, PAPER_TRADING=false required)")
    return {"enabled": enabled, "max_sol": str(max_sol) if max_sol is not None else None, "max_trades": max_trades,
            "problems": problems, "confirm_phrase": CONFIRM_PHRASE, "categories": list(CATEGORY_ENGINE)}


async def trades_used(session: AsyncSession) -> int:
    """Smoke-test buys ever submitted to the order worker."""
    return (await session.execute(select(func.count()).select_from(LiveSmokeTest)
                                  .where(LiveSmokeTest.position_id.is_not(None)))).scalar_one()


async def expire_stale(session: AsyncSession, now: datetime) -> None:
    rows = (await session.execute(select(LiveSmokeTest).where(LiveSmokeTest.status == "ARMED",
                                                              LiveSmokeTest.expires_at <= now))).scalars().all()
    for run in rows:
        run.status, run.stage, run.finished_at = "EXPIRED", "NO_TEST_EXECUTION_CANDIDATE", now
        seen: dict[str, int] = {}
        for a in run.attempts or []:
            seen[a["stage"]] = seen.get(a["stage"], 0) + 1
        run.stage_reason = ("no safe executable candidate before the run expired"
                            + (": " + ", ".join(f"{k} {v}" for k, v in sorted(seen.items())) if seen
                               else "; no candidate of this category had a BUY signal"))[:500]


async def armed_run(session: AsyncSession, now: datetime) -> LiveSmokeTest | None:
    await expire_stale(session, now)
    return (await session.execute(select(LiveSmokeTest).where(LiveSmokeTest.status == "ARMED")
                                  .order_by(LiveSmokeTest.created_at.desc()).limit(1))).scalar_one_or_none()


async def arm(session: AsyncSession, redis: Redis | None, app_settings: Any, category: str, max_sol: Decimal | None,
              minutes: int, username: str, now: datetime) -> LiveSmokeTest:
    cfg = config_status(app_settings)
    if cfg["problems"]:
        raise SmokeRefused("NOT_ARMED", "; ".join(cfg["problems"]))
    if category not in CATEGORY_ENGINE:
        raise SmokeRefused("NOT_ARMED", f"category must be one of {', '.join(CATEGORY_ENGINE)}")
    if not (MIN_MINUTES <= minutes <= MAX_MINUTES):
        raise SmokeRefused("NOT_ARMED", f"minutes must be between {MIN_MINUTES} and {MAX_MINUTES}")
    ceiling = Decimal(cfg["max_sol"])
    size = ceiling if max_sol is None else max_sol
    if size <= 0 or size > ceiling:
        raise SmokeRefused("NOT_ARMED", f"max_sol must be above 0 and at most LIVE_SMOKE_TEST_MAX_SOL ({ceiling})")
    used = await trades_used(session)
    if used >= cfg["max_trades"]:
        raise SmokeRefused("NOT_ARMED", f"{used} of LIVE_SMOKE_TEST_MAX_TRADES={cfg['max_trades']} smoke-test buys used")
    if await armed_run(session, now) is not None:
        raise SmokeRefused("NOT_ARMED", "another smoke-test run is already armed")
    ready, why = await live_trading.live_readiness(redis, app_settings, now)
    if not ready:
        raise SmokeRefused("EXECUTION_ROUTE_UNAVAILABLE", f"live execution not ready: {why}")
    run = LiveSmokeTest(category=category, max_sol=size, status="ARMED", armed_by=username[:64],
                        expires_at=now + timedelta(minutes=minutes), attempts=[])
    session.add(run)
    await session.flush()
    return run


def classify(assessment) -> tuple[str, list[str]]:
    """Stage for a smoke-test assessment the gate did not approve."""
    blocking = [f for f in assessment.findings if f.action == assessment.decision]
    codes = [f.code for f in blocking]
    cats = {f.code: f.category.value for f in blocking}
    if any(cats[c] not in ("DATA", "EXECUTION", "ACCOUNT") and c not in ROUTE_CODES for c in codes):
        return "SAFETY_REJECTED", codes
    if any(c in ROUTE_CODES for c in codes):
        return "EXECUTION_ROUTE_UNAVAILABLE", codes
    if any(cats[c] == "DATA" for c in codes):
        return "MARKET_DATA_UNAVAILABLE", codes
    return "RISK_REJECTED", codes


def _attempt(run: LiveSmokeTest, entry: dict[str, Any]) -> None:
    run.attempts = ([*(run.attempts or []), entry])[-MAX_ATTEMPTS_KEPT:]


async def try_entry(session: AsyncSession, redis: Redis | None, app_settings: Any, run: LiveSmokeTest, *, candidate,
                    engine: str, inp, versions: dict, evidence: dict, lifecycle: str, provenance: dict,
                    now: datetime) -> PaperPosition | None:
    """One smoke-test entry attempt for a candidate with a BUY signal. The
    full gate runs again against the live wallet with the size capped at
    run.max_sol; only an executable LIVE plan creates the buy."""
    from yonixalpha_core.safety.store import assessment_key, persist_assessment

    await session.refresh(run, with_for_update=True)  # one buy per run, even with concurrent evaluations
    if run.status != "ARMED" or run.expires_at <= now:
        return None
    base = {"at": now.isoformat(), "mint": inp.asset_id, "symbol": inp.symbol, "engine": engine, "lifecycle": lifecycle}
    cfg = config_status(app_settings)
    if cfg["problems"] or await trades_used(session) >= cfg["max_trades"]:
        run.status, run.stage, run.finished_at = "CANCELLED", "NOT_ARMED", now
        run.stage_reason = ("; ".join(cfg["problems"]) or "LIVE_SMOKE_TEST_MAX_TRADES reached")[:500]
        return None
    controls, account, _ = await pipeline.load_controls(session, redis, app_settings, engine, StrategyMode.AUTO,
                                                        inp.asset_id, now, False, live=True)
    cap = min(controls.settings.max_position_size_quote, Decimal(run.max_sol))
    smoke_settings = replace(controls.settings, max_position_size_quote=cap)
    ready, why = await live_trading.live_readiness(redis, app_settings, now)
    live_inp = replace(inp, account=controls.account, global_mode=GlobalMode.LIVE, strategy_mode=StrategyMode.AUTO,
                       live_trading_permitted=controls.live_trading_permitted, live_ready=ready, live_not_ready_reason=why,
                       manual_approval_granted=False)
    a = assess(live_inp, smoke_settings, versions={**versions, "smoke_test": str(run.id)})
    a.inputs_snapshot = {**evidence, "smoke_test": {"run": str(run.id), "max_sol": str(run.max_sol),
                                                     "wallet_available_sol": str(controls.account.available_balance)}}
    row, _ = await persist_assessment(session, a, candidate.id,
                                      assessment_key(engine, inp.asset_id, f"smoke:{run.id}:{int(now.timestamp()) // 30}"))
    if not (a.executable and a.execution_target.value == "LIVE"):
        stage, codes = classify(a)
        _attempt(run, {**base, "stage": stage, "codes": codes[:8], "decision": a.decision.value,
                       "reason": "; ".join(a.reasons)[:300], "assessment_id": str(row.id)})
        return None
    size = a.plan.position_size.value
    if size > Decimal(run.max_sol):  # defensive: the cap is a hard planning limit
        _attempt(run, {**base, "stage": "RISK_REJECTED", "codes": ["SMOKE_SIZE_ABOVE_MAX"],
                       "reason": f"planned {size} SOL above the run's max {run.max_sol}"})
        return None
    try:
        position = await live_trading.enter_live(
            session, redis, account, a, row.id, candidate, now, lifecycle, inp.token.decimals if inp.token else None,
            {**provenance, "venue": {**(provenance.get("venue") or {}), "smoke_test_run": str(run.id)}})
    except ValueError as exc:
        _attempt(run, {**base, "stage": "POSITION_CREATION_FAILED", "codes": [], "reason": str(exc)[:300],
                       "assessment_id": str(row.id)})
        return None
    run.status, run.stage = "USED", "BUY_REQUESTED"
    run.position_id, run.assessment_id, run.mint, run.engine = position.id, row.id, inp.asset_id, engine
    run.stage_reason = f"{size} SOL buy queued for the live order worker (route {position.execution_route})"
    _attempt(run, {**base, "stage": "BUY_REQUESTED", "codes": [], "reason": run.stage_reason, "assessment_id": str(row.id)})
    await store.add_timeline_event(session, "smoke_test_entry", now, {"run": str(run.id), "size_sol": str(size),
                                                                      "route": position.execution_route},
                                   candidate_id=candidate.id, assessment_id=row.id, position_id=position.id)
    return position


def failure_stage(side: str, status: str, error: str | None, signature: str | None, result: dict | None) -> str:
    err = (error or "").lower()
    if "not for the configured wallet" in err:
        return "SIGNING_FAILED"
    if "fill unreadable" in err:
        return "FILL_UNVERIFIED"
    code = live_trading.failure_code_of(side, status, error, signature, result)
    stage = FAILURE_STAGE.get(code.split("_", 1)[1], "TRANSACTION_UNCONFIRMED")
    return "SELL_FAILED: " + stage if side == "SELL" else stage


def order_view(o: ExecutionOrder, planned_price: Decimal | None, decimals: int | None) -> dict[str, Any]:
    """Requested vs actual for one live order, from the order row only."""
    fill = (o.result or {}).get("fill") or None
    out: dict[str, Any] = {
        "side": o.side, "reason": o.reason, "route": o.route, "provider": o.provider, "status": o.status,
        "requested": {"amount": o.amount, "amount_kind": o.amount_kind, "slippage_pct": str(o.slippage_pct),
                      "limits": o.limits},
        "signature": o.signature, "created_at": o.created_at, "submitted_at": o.submitted_at, "confirmed_at": o.confirmed_at,
        "order_submitted": o.submitted_at is not None and bool((o.result or {}).get("sent", o.status in ("SUBMITTED", "CONFIRMED"))),
        "transaction_confirmed": o.status == "CONFIRMED",
        "actually_filled": False, "error": o.error,
    }
    if fill and decimals is not None:
        tokens = Decimal(abs(int(fill["token_change_raw"]))) / Decimal(10) ** decimals
        sol = Decimal(abs(int(fill["sol_change_lamports"]))) / LAMPORTS
        right_way = (int(fill["token_change_raw"]) > 0 and int(fill["sol_change_lamports"]) < 0) if o.side == "BUY" else (
            int(fill["token_change_raw"]) < 0 and int(fill["sol_change_lamports"]) > 0)
        out["actually_filled"] = o.status == "CONFIRMED" and right_way
        out["actual"] = {"tokens": str(tokens), "sol": str(sol), "network_fee_sol": str(Decimal(int(fill["fee_lamports"])) / LAMPORTS),
                         "price_sol": str(sol / tokens) if tokens > 0 else None, "slot": fill.get("slot")}
        if planned_price and tokens > 0:
            px = sol / tokens
            out["actual"]["slippage_vs_plan_pct"] = str(((px / planned_price - 1) * 100 * (1 if o.side == "BUY" else -1))
                                                        .quantize(Decimal("0.01")))
    if o.status in ("FAILED", "EXPIRED", "CANCELLED"):
        out["failure_stage"] = failure_stage(o.side, o.status, o.error, o.signature, o.result)
    return out


async def run_view(session: AsyncSession, run: LiveSmokeTest, now: datetime, price_max_age_seconds: int = 60) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": run.id, "category": run.category, "engine": run.engine or CATEGORY_ENGINE.get(run.category), "max_sol": str(run.max_sol),
        "status": run.status, "stage": run.stage, "stage_reason": run.stage_reason, "armed_by": run.armed_by,
        "created_at": run.created_at, "expires_at": run.expires_at, "finished_at": run.finished_at, "mint": run.mint,
        "attempts": list(reversed((run.attempts or [])[-20:])), "buy": None, "sells": [], "position": None,
    }
    if run.position_id is None:
        return out
    p = await session.get(PaperPosition, run.position_id)
    orders = (await session.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == run.position_id)
                                    .order_by(ExecutionOrder.created_at))).scalars().all()
    decimals = int(((p.plan or {}).get("venue") or {}).get("decimals") or 6) if p else None
    planned = Decimal(str((p.plan or {}).get("entry_price"))) if p and (p.plan or {}).get("entry_price") else None
    buys = [order_view(o, planned, decimals) for o in orders if o.side == "BUY"]
    sells = [order_view(o, p.entry_price if p else None, decimals) for o in orders if o.side == "SELL"]
    out["buy"], out["sells"] = (buys[-1] if buys else None), sells
    if p is not None:
        out["position"] = position_view(p, now, price_max_age_seconds)
    stage = run.stage
    b = out["buy"]
    if b:
        if b.get("failure_stage"):
            stage = b["failure_stage"]
        elif b["actually_filled"]:
            stage = "ACTUALLY_FILLED"
        elif b["transaction_confirmed"]:
            stage = "TRANSACTION_CONFIRMED"
        elif b["order_submitted"]:
            stage = "ORDER_SUBMITTED"
    if p is not None:
        if p.status == "needs_review":
            stage = "FILL_UNVERIFIED"
        elif p.status == "open":
            stage = "POSITION_OPEN"
            if out["position"]["price_status"] != "LIVE":
                stage = "POSITION_OPEN (MARKET_DATA_UNAVAILABLE)"
        if sells:
            last = sells[-1]
            if last.get("failure_stage") and p.status == "open":
                stage = last["failure_stage"]
            elif last["order_submitted"] and p.status == "open":
                stage = "SELL_SUBMITTED"
        if p.status == "closed":
            stage = "POSITION_CLOSED" if any(s["actually_filled"] for s in sells) else "POSITION_CLOSED (no confirmed sell)"
    out["stage"] = stage
    return out


def position_view(p: PaperPosition, now: datetime, price_max_age_seconds: int = 60) -> dict[str, Any]:
    """A live (or paper) position with PnL from its recorded fills and its
    latest mark. Unrealized PnL = remaining quantity x latest price - the
    remaining share of the entry cost; STALE when the mark is too old."""
    qty = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    initial = p.initial_quantity or p.quantity
    cost = p.entry_cost_quote or Decimal(0)
    cost_remaining = cost * (qty / initial) if initial and qty is not None else None
    age = (now - p.last_marked_at).total_seconds() if p.last_marked_at else None
    price_status = "UNAVAILABLE" if p.last_price is None or age is None else ("LIVE" if age <= price_max_age_seconds else "STALE")
    value = (qty * p.last_price) if (qty is not None and p.last_price is not None) else None
    upnl = (value - cost_remaining) if (value is not None and cost_remaining is not None and p.status == "open") else None
    tps = p.take_profit or []
    return {
        "id": p.id, "symbol": p.symbol, "mint": p.asset_id, "status": p.status, "execution_mode": p.execution_mode,
        "engine": p.engine, "lifecycle": p.lifecycle, "execution_route": p.execution_route,
        "execution_provider": p.execution_provider, "entry_price": p.entry_price, "current_price": p.last_price,
        "price_at": p.last_marked_at, "price_age_seconds": round(age, 1) if age is not None else None,
        "price_status": price_status, "price_source": ((p.plan or {}).get("venue") or {}).get("type"),
        "quantity": qty, "initial_quantity": initial, "entry_value": cost, "current_value": value,
        "unrealized_pnl": upnl, "unrealized_pnl_pct": (upnl / cost_remaining * 100).quantize(Decimal("0.01"))
        if upnl is not None and cost_remaining else None,
        "realized_pnl": p.realized_pnl, "fees_paid": p.fees_paid_quote, "stop_loss": p.stop_loss, "take_profits": tps,
        "tp_hits": p.tp_hits or [], "trailing_stop": p.trailing_stop, "highest_price": p.highest_price,
        "entry_at": p.entry_at, "exit_at": p.exit_at, "exit_reason": p.exit_reason, "exit_requested": bool(p.exit_requested),
        "smoke_test_run": ((p.plan or {}).get("venue") or {}).get("smoke_test_run"),
    }
