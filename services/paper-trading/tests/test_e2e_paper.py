"""End-to-end PAPER scenario across both services, on byte-exact synthetic
Pump.fun data (no network):

stream detect -> word filters -> safety gate (tax, sellability, liquidity,
flow, funding links, risk plan) -> AUTO decision without approval -> paper
entry at a simulated curve fill -> TP1 partial exit -> trailing stop exit
(both simulated against the curve after real trades moved it) -> realized
PnL -> database rows -> realtime events -> labelled ML sample.

decision-engine's gate_eval is loaded by path because both services name
their package `app`; it has no service-local imports.
"""

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core import events
from yonixalpha_core.db.models import (
    BlacklistEntry, ExecutionOrder, MLFeatureSnapshot, PaperAccount, PaperPosition, RiskAssessment, Token,
    TradeTimelineEvent, TradingCandidate,
)
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, logs_of, seed_healthy_launch, wallet

from app.gate_manage import manage_gate_positions

_spec = importlib.util.spec_from_file_location(
    "de_gate_eval", Path(__file__).resolve().parents[2] / "decision-engine" / "app" / "gate_eval.py")
gate_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_eval)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PAPER_ENV = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True, TELEGRAM_BOT_TOKEN=None,
                            TELEGRAM_CHAT_ID=None)


async def drain(pubsub) -> list[dict]:
    out, idle = [], 0
    while idle < 5:  # the subscribe confirmation also reads as None here
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if m is None:
            idle += 1
            continue
        idle = 0
        out.append(json.loads(m["data"]))
    return out


async def trade(redis, curve, at, n, sol, is_buy, first_wallet):
    evs = [curve.trade(wallet(first_wallet + i), at, sol, is_buy) for i in range(n)]
    await pump_stream.ingest_logs(redis, logs_of(*evs), f"sig-{at.timestamp()}-{is_buy}", at)


async def test_fresh_launch_paper_trade_end_to_end(session_factory, redis_client):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(events.CHANNEL)

    # 1. Detect: the stream store holds the create event and 40 trades.
    curve = await seed_healthy_launch(redis_client, NOW)
    assert await pump_stream.load_meta(redis_client, MINT) is not None

    async with session_factory() as s:
        # A word filter that must not match "PIPE", and one ALLOW rule: filters run, nothing is blocked.
        s.add(BlacklistEntry(scope="FRESH", field="name", match_type="word", action="BLOCK", value="rug"))
        s.add(BlacklistEntry(scope="GLOBAL", field="any", match_type="substring", action="BLOCK", value="honeypot"))
        token = Token(mint_address=MINT, symbol="PIPE", first_seen_source="pump_stream")
        s.add(token)
        await s.flush()
        cand = TradingCandidate(token_id=token.id, engine="discovery", state=CandidateState.DISCOVERED.value,
                                state_history=[{"state": "discovered", "at": NOW.isoformat(), "reason": "stream"}],
                                detail={"source": "pump_stream", "mint": MINT, "strategy": "solana_fresh"})
        s.add(cand)
        await s.commit()
        cid = cand.id

    # 2-4. Analyze + decide + paper entry (decision-engine).
    async with session_factory() as s:
        cand = await s.get(TradingCandidate, cid)
        a = await gate_eval.evaluate_with_gate(s, redis_client, PAPER_ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    assert a.execution_target.value == "PAPER"
    assert "BLACKLISTED" not in {f.code for f in a.findings}
    assert a.reports["tax"]["decision"] == "PASS" and a.reports["tax"]["confidence"] == "HIGH"
    assert a.reports["sellability"]["status"] == "SELLABLE"
    assert a.reports["liquidity"]["position_to_liquidity"] is not None
    assert a.plan.complete and a.plan.max_loss.value > 0

    async with session_factory() as s:
        pos = (await s.execute(select(PaperPosition))).scalar_one()
        account = await s.get(PaperAccount, pos.account_id)
        start_cash = account.cash_balance + pos.entry_cost_quote
        sample = (await s.execute(select(MLFeatureSnapshot))).scalar_one()
    assert (pos.execution_mode, pos.source, pos.lifecycle, pos.execution_provider, pos.execution_route) == (
        "PAPER", "PUMPFUN", "FRESH", "paper_simulator", "pump")
    assert pos.strategy == a.strategy and pos.feature_version and pos.status == "open"
    assert sample.label is None and sample.feature_version == pos.feature_version
    pid = pos.id

    # 5. Price rises through TP1 on real buys; manage.
    t1 = NOW + timedelta(seconds=40)
    tp1 = Decimal(pos.plan["take_profits"][0]["price"]["value"])
    tp2 = Decimal(pos.plan["take_profits"][1]["price"]["value"])
    i = 0
    while curve.price() <= tp1 * Decimal("1.005"):
        await trade(redis_client, curve, t1, 1, 300_000_000, True, 60 + i)
        i += 1
    assert tp1 < curve.price() < tp2
    c1 = await manage_gate_positions(session_factory, redis_client, None, t1, None, PAPER_ENV)
    async with session_factory() as s:
        pos = await s.get(PaperPosition, pid)
    assert c1["managed"] == 1 and pos.tp_hits == [0] and pos.trailing_stop is not None
    assert pos.remaining_quantity < pos.initial_quantity and pos.status == "open"

    # 6. A dump through the trailing stop; the remainder exits on the curve.
    t2 = t1 + timedelta(seconds=40)
    await trade(redis_client, curve, t2, 10, 1_500_000_000, False, 70)
    c2 = await manage_gate_positions(session_factory, redis_client, None, t2, None, PAPER_ENV)
    assert c2["closed"] == 1

    # 7. PnL and database state.
    async with session_factory() as s:
        pos = await s.get(PaperPosition, pid)
        account = await s.get(PaperAccount, pos.account_id)
        cand = await s.get(TradingCandidate, cid)
        sample = (await s.execute(select(MLFeatureSnapshot))).scalar_one()
        row = await s.get(RiskAssessment, pos.assessment_id)
        kinds = [e.event_type for e in (await s.execute(
            select(TradeTimelineEvent).where(TradeTimelineEvent.candidate_id == cid).order_by(TradeTimelineEvent.occurred_at))).scalars()]
        live_orders = (await s.execute(select(ExecutionOrder))).scalars().all()
    assert pos.status == "closed" and pos.exit_reason in ("trailing_stop", "exit_intel_exit", "stop_loss")
    assert pos.remaining_quantity == 0
    assert pos.realized_pnl == pos.proceeds_quote - pos.entry_cost_quote
    assert account.cash_balance == start_cash - pos.entry_cost_quote + pos.proceeds_quote
    assert cand.state == CandidateState.CLOSED.value
    assert row.decision == "EXECUTE" and row.assessment["reports"]["tax"]["decision"] == "PASS"
    order = ["risk_analysis_started", "assessed.execute", "paper_entry", "paper_exit.take_profit_1", "paper_closed"]
    assert [k for k in kinds if k in order] == order, kinds
    assert live_orders == []  # paper never creates an on-chain order

    # 8. ML sample labelled from the realized outcome.
    assert sample.label == (1 if pos.realized_pnl > 0 else 0) and sample.label_source and sample.outcome
    assert sample.label_source != "live_execution_realized_pnl"

    # 9. Realtime events were published for the dashboard.
    await asyncio.sleep(0.05)
    types = [m["type"] for m in await drain(pubsub)]
    await pubsub.unsubscribe()
    await pubsub.aclose()
    assert "trade.created" in types and "trade.updated" in types and "trade.closed" in types
    assert "balance.updated" in types
