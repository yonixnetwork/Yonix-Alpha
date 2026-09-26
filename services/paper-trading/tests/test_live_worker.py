"""LIVE execution lifecycle with the provider boundary mocked.

No transaction is built, signed or sent here: `FakeExecutor` stands in for
SolanaLiveExecutor (PumpPortal + RPC) and returns the outcomes a real
executor produces. Everything above that boundary — orders, idempotency,
fills from balance changes, exits via manage_step, realized PnL,
reconciliation after a restart — is the production code.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from yonixalpha_core import live_trading
from yonixalpha_core.db.models import (
    ExecutionOrder, MLFeatureSnapshot, PaperPosition, ReconciliationEvent, Token, TradingCandidate,
)
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import ExecutionTarget
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.solana.live_exec import ExecOutcome, Fill
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, empty_account, logs_of, seed_healthy_launch, wallet

from app.gate_manage import manage_gate_positions
from app.live_worker import live_worker_loop

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRESH = default_settings_for("solana_fresh")
WALLET = wallet(140)
LIVE_ON = SimpleNamespace(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False, TELEGRAM_BOT_TOKEN=None,
                          TELEGRAM_CHAT_ID=None)
LOCKED = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
OTHER_MINT = wallet(141)


class WalletRpc:
    def __init__(self, lamports: int, tokens: dict[str, int]):
        self.lamports, self.tokens = lamports, tokens

    async def call(self, method, params=None):
        if method == "getBalance":
            return {"value": self.lamports}
        if method == "getTokenAccountsByOwner":
            if params[1]["programId"] != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
                return {"value": []}
            return {"value": [{"account": {"data": {"parsed": {"info": {
                "mint": m, "tokenAmount": {"amount": str(a), "decimals": 6}}}}}} for m, a in self.tokens.items()]}
        raise AssertionError(f"unexpected RPC {method}")


class FakeExecutor:
    """The provider boundary: records requests, returns scripted outcomes."""

    def __init__(self, lamports: int = 5_000_000_000, tokens: dict[str, int] | None = None):
        self.wallet = SimpleNamespace(pubkey=WALLET)
        self.rpc = WalletRpc(lamports, tokens or {})
        self.outcomes: list[ExecOutcome] = []
        self.lookups: dict[str, ExecOutcome] = {}
        self.requests = []

    async def execute(self, req, exp, on_signed):
        self.requests.append((req, exp))
        out = self.outcomes.pop(0)
        if out.signature:
            await on_signed(out.signature)
        return out

    async def lookup(self, signature, mint):
        return self.lookups.get(signature, ExecOutcome("PENDING", signature))


def confirmed(sig: str, sol_change: int, token_change: int, fee: int = 5_000) -> ExecOutcome:
    return ExecOutcome("CONFIRMED", sig, fill=Fill(sol_change, token_change, fee, 6, 123, int(NOW.timestamp())), sent=True)


async def live_assessment(session_factory, redis):
    """A real gate assessment of a healthy launch, targeted at LIVE (the gate
    only does that itself with every lock open and a ready worker)."""
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    assert a.executable, a.reasons
    a.execution_target = ExecutionTarget.LIVE
    async with session_factory() as s:
        token = Token(mint_address=MINT, first_seen_source="t")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state="analyzing", state_history=[])
        s.add(cand)
        await s.flush()
        row, _ = await store.persist_assessment(s, a, cand.id, "k")
        s.add(MLFeatureSnapshot(candidate_id=cand.id, assessment_id=row.id, engine="solana_fresh", symbol="PIPE", features={"x": 1},
                                feature_version="v"))
        await s.commit()
        return curve, a, row.id, cand.id


async def enter(session_factory, redis, cash=Decimal(5)):
    curve, a, aid, cid = await live_assessment(session_factory, redis)
    async with session_factory() as s:
        acct = await live_trading.get_live_account(s)
        acct.cash_balance = cash
        cand = await s.get(TradingCandidate, cid)
        p = await live_trading.enter_live(s, redis, acct, a, aid, cand, NOW, "FRESH", 6,
                                          {"source": "PUMPFUN", "lifecycle": "FRESH", "feature_version": "fv1",
                                           "venue": {"creator": None}})
        await s.commit()
        return curve, a, p.id, p.pending_order_id, cid


async def open_live(session_factory, redis):
    curve, a, pid, oid, cid = await enter(session_factory, redis)
    ex = FakeExecutor()
    size = a.plan.position_size.value
    spent = int(size * 1_000_000_000) + 2_039_280 + 5_000  # size + ATA rent + network fee, as a wallet would show
    ex.outcomes.append(confirmed("sig-buy", -spent, 3_000_000_000_000))
    assert await live_trading.process_order(session_factory, redis, LIVE_ON, ex, oid) == "CONFIRMED"
    return curve, a, pid, cid, ex, spent


async def test_buy_request_is_bounded_and_opens_from_the_actual_fill(session_factory, redis_client):
    curve, a, pid, cid, ex, spent = await open_live(session_factory, redis_client)
    req, exp = ex.requests[0]
    size = a.plan.position_size.value
    assert (req.action, req.mint, req.denominated_in_sol, req.pool) == ("buy", MINT, True, "pump")
    assert Decimal(str(req.amount)) == size
    assert exp.wallet == WALLET and exp.side == "buy"
    assert exp.max_sol_in_lamports == int(size * Decimal("1.10") * 1_000_000_000)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        order = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == pid))).scalar_one()
        cand = await s.get(TradingCandidate, cid)
    assert order.status == "CONFIRMED" and order.signature == "sig-buy"
    assert p.status == "open" and p.execution_mode == "LIVE" and p.pending_order_id is None
    assert p.quantity == Decimal(3_000_000)  # 3e12 raw / 1e6
    assert p.entry_cost_quote == Decimal(spent) / Decimal(1_000_000_000)  # actual SOL out of the wallet, not the plan
    assert abs(p.entry_price - p.entry_cost_quote / p.quantity) < Decimal("1e-17")  # stored at 18 dp
    assert (p.source, p.lifecycle, p.execution_provider, p.execution_route, p.feature_version) == (
        "PUMPFUN", "FRESH", "pumpportal_local", "pump", "fv1")
    assert cand.state == CandidateState.MANAGING.value


async def test_a_second_entry_for_the_same_mint_is_refused(session_factory, redis_client):
    curve, a, pid, oid, cid = await enter(session_factory, redis_client)
    async with session_factory() as s:
        acct = await live_trading.get_live_account(s)
        with pytest.raises(ValueError, match="already exists"):
            await live_trading.enter_live(s, redis_client, acct, a, None, None, NOW, "FRESH", 6, {})


async def test_entry_larger_than_wallet_minus_reserve_is_refused(session_factory, redis_client):
    _, a, aid, cid = await live_assessment(session_factory, redis_client)
    async with session_factory() as s:
        acct = await live_trading.get_live_account(s)
        acct.cash_balance = a.plan.position_size.value  # nothing left for the 0.05 SOL reserve
        with pytest.raises(ValueError, match="reserve"):
            await live_trading.enter_live(s, redis_client, acct, a, aid, None, NOW, "FRESH", 6, {})


@pytest.mark.parametrize("outcome", [
    ExecOutcome("FAILED", "sig-x", error="simulation failed: slippage", sent=False),
    ExecOutcome("EXPIRED", "sig-y", error="not confirmed within 75s", sent=True),  # sent is not filled
    ExecOutcome("FAILED", None, error="transaction refused by guard: unknown program"),
])
async def test_unfilled_buy_never_opens_a_position(session_factory, redis_client, outcome):
    _, _, pid, oid, cid = await enter(session_factory, redis_client)
    ex = FakeExecutor()
    ex.outcomes.append(outcome)
    status = await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid)
    assert status == ("EXPIRED" if outcome.status == "EXPIRED" else "FAILED")
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        cand = await s.get(TradingCandidate, cid)
        acct = await live_trading.get_live_account(s)
    assert p.status == "failed" and p.quantity == 0 and p.exit_reason == "entry_failed"
    # Scenario D: first failure -> back to the gate for a fresh full re-evaluation (one retry allowed).
    assert cand.state == CandidateState.ANALYZING.value
    assert cand.state_history[-1]["reason"].startswith("BUY_FAILED (attempt 1/2)")
    assert acct.cash_balance == Decimal(5)  # nothing debited without a fill


async def test_second_buy_failure_rejects_and_never_duplicates(session_factory, redis_client):
    from yonixalpha_core.db.models import TradeTimelineEvent

    curve, a, pid, oid, cid = await enter(session_factory, redis_client)
    ex = FakeExecutor()
    ex.outcomes.append(ExecOutcome("FAILED", None, error="transaction guard refused to sign: unexpected program"))
    await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid)
    # The gate re-evaluates (new assessment) and tries once more; it fails again.
    async with session_factory() as s:
        acct = await live_trading.get_live_account(s)
        cand = await s.get(TradingCandidate, cid)
        row, _ = await store.persist_assessment(s, a, cid, "k-retry")
        await s.flush()
        p2 = await live_trading.enter_live(s, redis_client, acct, a, row.id, cand, NOW, "FRESH", 6, {"venue": {}})
        await s.commit()
        oid2 = p2.pending_order_id
    ex.outcomes.append(ExecOutcome("EXPIRED", "sig-x", error="not found; blockhash expired"))
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid2) == "EXPIRED"
    async with session_factory() as s:
        cand = await s.get(TradingCandidate, cid)
        live = (await s.execute(select(PaperPosition).where(PaperPosition.execution_mode == "LIVE",
                                                            PaperPosition.status.in_(("open", "pending_entry"))))).scalars().all()
        codes = [e.detail["code"] for e in (await s.execute(select(TradeTimelineEvent).where(
            TradeTimelineEvent.event_type == "live_entry_failed").order_by(TradeTimelineEvent.occurred_at))).scalars()]
    assert cand.state == CandidateState.REJECTED.value and "no retries left" in cand.state_history[-1]["reason"]
    assert live == []  # no position, no duplicate
    assert codes == ["BUY_REFUSED_BY_TRANSACTION_GUARD", "BUY_CONFIRMATION_TIMEOUT"]


async def test_confirmed_buy_without_tokens_needs_review_not_an_invented_position(session_factory, redis_client):
    _, _, pid, oid, _ = await enter(session_factory, redis_client)
    ex = FakeExecutor()
    ex.outcomes.append(confirmed("sig-odd", -100_000_000, 0))
    await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        ev = (await s.execute(select(ReconciliationEvent))).scalars().all()
    assert p.status == "needs_review"
    assert [e.kind for e in ev] == ["buy_confirmed_without_tokens"]


async def test_processing_an_order_twice_buys_once(session_factory, redis_client):
    _, _, pid, oid, _ = await enter(session_factory, redis_client)
    ex = FakeExecutor()
    ex.outcomes.append(confirmed("sig-buy", -100_000_000, 1_000_000))
    first = await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid)
    second = await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid)
    assert (first, second) == ("CONFIRMED", "skipped")
    assert len(ex.requests) == 1


async def test_stop_loss_becomes_a_sell_and_pnl_comes_from_the_sell_fill(session_factory, redis_client):
    curve, a, pid, cid, ex, spent = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        crash = p.stop_loss * Decimal("0.8")
        out = await live_trading.manage_live_position(s, p, crash, None, NOW + timedelta(seconds=20))
        await s.commit()
    assert out["requested"] == "stop_loss"
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        sell = await s.get(ExecutionOrder, p.pending_order_id)
    assert p.status == "open"  # nothing changes until the sell confirms
    assert (sell.side, sell.status, sell.amount, sell.reason) == ("SELL", "PENDING", "3000000000000", "stop_loss")

    # A second tick while the sell is pending must not queue another.
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        again = await live_trading.manage_live_position(s, p, crash, None, NOW + timedelta(seconds=21))
        await s.commit()
    assert again["requested"] is None

    received = 60_000_000
    ex.outcomes.append(confirmed("sig-sell", received, -3_000_000_000_000))
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, sell.id) == "CONFIRMED"
    req, exp = ex.requests[-1]
    assert (req.action, req.denominated_in_sol, req.amount, exp.max_tokens_in) == ("sell", False, "3000000", 3_000_000_000_000)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        cand = await s.get(TradingCandidate, cid)
        label = (await s.execute(select(MLFeatureSnapshot).where(MLFeatureSnapshot.candidate_id == cid))).scalar_one()
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.remaining_quantity == 0
    assert p.realized_pnl == (Decimal(received) - Decimal(spent)) / Decimal(1_000_000_000)
    assert cand.state == CandidateState.CLOSED.value
    assert label.label == 0 and label.label_source == "live_execution_realized_pnl"


async def test_failed_exit_is_retried_with_more_slippage(session_factory, redis_client):
    _, _, pid, _, ex, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        await live_trading.manage_live_position(s, p, p.stop_loss / 2, None, NOW)
        await s.commit()
        first = p.pending_order_id
    ex.outcomes.append(ExecOutcome("FAILED", "sig-f", error="slippage exceeded"))
    await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, first)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert p.exit_failures == 1 and p.status == "open" and p.pending_order_id is None
        await live_trading.manage_live_position(s, p, p.stop_loss / 2, None, NOW + timedelta(seconds=5))
        await s.commit()
        retry = await s.get(ExecutionOrder, p.pending_order_id)
        orig = await s.get(ExecutionOrder, first)
    assert retry.id != first and retry.slippage_pct == orig.slippage_pct + 10


async def test_restart_with_a_signed_order_resolves_it_without_rebuying(session_factory, redis_client):
    _, _, pid, oid, _ = await enter(session_factory, redis_client)
    # Crash after the signature was persisted, before confirmation was seen.
    async with session_factory() as s:
        o = await s.get(ExecutionOrder, oid)
        o.status, o.signature, o.submitted_at = "SIGNED", "sig-crash", NOW - timedelta(seconds=60)
        await s.commit()
    ex = FakeExecutor(tokens={MINT: 1_000_000_000})
    ex.lookups["sig-crash"] = confirmed("sig-crash", -150_000_000, 1_000_000_000)
    report = await live_trading.reconcile(session_factory, redis_client, LIVE_ON, ex, NOW)
    assert report["orders_resolved"] == 1
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, oid) == "skipped"
    assert ex.requests == []
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        kinds = (await s.execute(select(ReconciliationEvent.kind))).scalars().all()
    assert p.status == "open" and p.quantity == Decimal(1000)
    assert "order_confirmed_on_reconcile" in kinds
    # A second reconcile does not apply the same fill again.
    await live_trading.reconcile(session_factory, redis_client, LIVE_ON, ex, NOW + timedelta(seconds=30))
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).quantity == Decimal(1000)


async def test_signed_order_never_seen_on_chain_expires(session_factory, redis_client):
    _, _, pid, oid, _ = await enter(session_factory, redis_client)
    async with session_factory() as s:
        o = await s.get(ExecutionOrder, oid)
        o.status, o.signature, o.submitted_at = "SUBMITTED", "sig-lost", NOW - timedelta(seconds=400)
        await s.commit()
    await live_trading.reconcile(session_factory, redis_client, LIVE_ON, FakeExecutor(), NOW)
    async with session_factory() as s:
        assert (await s.get(ExecutionOrder, oid)).status == "EXPIRED"
        assert (await s.get(PaperPosition, pid)).status == "failed"


async def test_reconcile_flags_missing_tokens_and_records_unknown_holdings_once(session_factory, redis_client):
    _, _, pid, _, _, _ = await open_live(session_factory, redis_client)
    ex = FakeExecutor(lamports=4_000_000_000, tokens={OTHER_MINT: 55})  # our token is gone; an unknown one appeared
    report = await live_trading.reconcile(session_factory, redis_client, LIVE_ON, ex, NOW)
    await live_trading.reconcile(session_factory, redis_client, LIVE_ON, ex, NOW + timedelta(seconds=30))
    assert report["mismatches"] == 1 and report["unknown_holdings"] == 1
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        kinds = (await s.execute(select(ReconciliationEvent.kind).order_by(ReconciliationEvent.created_at))).scalars().all()
        acct = await live_trading.get_live_account(s)
    assert p.status == "needs_review"  # never an invented exit
    assert kinds.count("unknown_holding") == 1
    assert acct.cash_balance == Decimal(4)  # the wallet is the source of truth
    w = json.loads(await redis_client.get(live_trading.WALLET_KEY))
    assert w["sol"] == "4" and w["pubkey"] == WALLET


async def test_gate_manage_turns_a_crash_into_a_live_sell_order(session_factory, redis_client):
    curve, _, pid, _, _, _ = await open_live(session_factory, redis_client)
    later = NOW + timedelta(seconds=30)
    events = [curve.trade(wallet(50 + i), later, 2_000_000_000, False) for i in range(12)]
    await pump_stream.ingest_logs(redis_client, logs_of(*events), "sig-dump", later)
    counts = await manage_gate_positions(session_factory, redis_client, None, later, None, LIVE_ON)
    assert counts["managed"] == 1 and counts["closed"] == 0
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        sells = (await s.execute(select(ExecutionOrder).where(ExecutionOrder.side == "SELL"))).scalars().all()
    assert p.status == "open" and len(sells) == 1 and sells[0].reason in ("stop_loss", "exit_intel_exit")
    assert sells[0].amount == "3000000000000"


async def test_worker_with_locks_closed_reports_disabled_and_cancels_live_orders(session_factory, redis_client):
    _, _, pid, oid, _ = await enter(session_factory, redis_client)
    stop = asyncio.Event()
    task = asyncio.create_task(live_worker_loop(session_factory, redis_client, LOCKED, None, None, stop))
    await asyncio.sleep(0.5)
    stop.set()
    await task
    state = json.loads(await redis_client.get(live_trading.READY_KEY))
    assert state["status"] == "disabled"
    ready, reason = await live_trading.live_readiness(redis_client, LOCKED, NOW)
    assert not ready and "locks" in reason
    async with session_factory() as s:
        assert (await s.get(ExecutionOrder, oid)).status == "CANCELLED"
        assert (await s.get(PaperPosition, pid)).status == "failed"


async def test_worker_reconciles_before_processing_and_then_reports_ready(session_factory, redis_client):
    _, a, pid, oid, _ = await enter(session_factory, redis_client)
    ex = FakeExecutor(lamports=5_000_000_000)
    ex.outcomes.append(confirmed("sig-w", -100_000_000, 2_000_000))
    stop = asyncio.Event()
    task = asyncio.create_task(live_worker_loop(session_factory, redis_client, LIVE_ON, None, None, stop, executor=ex))
    for _ in range(50):
        await asyncio.sleep(0.1)
        async with session_factory() as s:
            if (await s.get(ExecutionOrder, oid)).status != "PENDING":
                break
    stop.set()
    await task
    state = json.loads(await redis_client.get(live_trading.READY_KEY))
    assert state["status"] == "ready" and state["wallet"] == WALLET
    ready, reason = await live_trading.live_readiness(redis_client, LIVE_ON, datetime.now(timezone.utc))
    assert ready, reason
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"
        n = (await s.execute(select(func.count()).select_from(ReconciliationEvent))).scalar_one()
    assert n == 0


async def test_migrated_live_entry_routes_to_pumpswap(session_factory, redis_client):
    _, a, aid, cid = await live_assessment(session_factory, redis_client)
    async with session_factory() as s:
        acct = await live_trading.get_live_account(s)
        acct.cash_balance = Decimal(5)
        p = await live_trading.enter_live(s, redis_client, acct, a, aid, None, NOW, "MIGRATED", 6,
                                          {"pool": "Pool1111", "venue": {"pool": "Pool1111"}})
        order = await s.get(ExecutionOrder, p.pending_order_id)
        await s.commit()
    assert (p.lifecycle, p.execution_route, p.pool, p.plan["venue"]["type"]) == ("MIGRATED", "pump-amm", "Pool1111", "pumpswap_pool")
    req, exp = live_trading._trade_request(order, WALLET)
    assert req.pool == "pump-amm" and req.action == "buy" and exp.side == "buy"


async def test_curve_position_keeps_managing_through_migration_and_sells_on_pumpswap(session_factory, redis_client):
    """Scenario B: bought on the bonding curve, the token migrates while the
    position is open. The same position continues (PRE_MIGRATION ->
    POST_MIGRATION), is priced from the PumpSwap pool, and its exit is a
    PumpSwap sell — a bonding-curve sell of a completed curve cannot fill."""
    from yonixalpha_core.db.models import TradeTimelineEvent
    from yonixalpha_core.solana import pumpswap
    from yonixalpha_core.testing.pumpswap import FakePoolRpc, trade_history

    _, _, pid, _, ex, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert (p.lifecycle, p.execution_route) == ("FRESH", "pump")
    later = NOW + timedelta(seconds=30)
    pool = pumpswap.canonical_pool(MINT)
    await redis_client.hset(pump_stream.curve_key(MINT), mapping={"complete": 1, "pool": pool,
                                                                  "migrated_at": int(later.timestamp())})
    await redis_client.set(pump_stream.HEARTBEAT, later.isoformat())
    # The pool trades well below the position's stop.
    rpc = FakePoolRpc(MINT, 700_000_000_000_000, 30 * 10**9, trade_history(pool, 10, 0, later - timedelta(seconds=200)),
                      inner=FakeRpc(None))
    counts = await manage_gate_positions(session_factory, redis_client, None, later, None, LIVE_ON, rpc)
    assert counts["managed"] == 1 and counts["unpriced"] == 0
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        sell = await s.get(ExecutionOrder, p.pending_order_id)
        moved = (await s.execute(select(TradeTimelineEvent).where(TradeTimelineEvent.event_type == "position_migrated"))).scalar_one()
    assert (p.status, p.lifecycle, p.execution_route, p.pool) == ("open", "MIGRATED", "pump-amm", pool)
    assert moved.detail["market_state"] == "POST_MIGRATION" and moved.detail["route_before"] == "pump"
    assert (sell.side, sell.route) == ("SELL", "pump-amm")
    req, _ = live_trading._trade_request(sell, WALLET)
    assert req.pool == "pump-amm"

    ex.outcomes.append(confirmed("sig-sell-amm", 20_000_000, -3_000_000_000_000))
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, sell.id) == "CONFIRMED"
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
    assert p.status == "closed" and p.realized_pnl < 0
