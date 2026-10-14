"""SOL-denominated risk settings end to end (2026-10-13): the saved fixed
trade size is what the next paper entry uses; the account-wide daily loss
limit counts every Solana strategy on the shared paper book; a strategy's own
limit counts only its own losses; a blocked account fetches no token data."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core import rpc_usage
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, Token, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import MINT, FakeRpc, seed_healthy_launch

from app.gate_eval import evaluate_with_gate

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ENV = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
# A first GLOBAL save in an empty database starts from the generic defaults;
# production's GLOBAL carries the Solana stop floor (settings.ENGINE_DEFAULTS).
SOL = {"min_stop_pct": "0.10"}


async def make_candidate(db, engine="discovery") -> TradingCandidate:
    token = Token(mint_address=MINT, symbol="PIPE", first_seen_source="pump_stream")
    db.add(token)
    await db.flush()
    c = TradingCandidate(token_id=token.id, engine=engine, state=CandidateState.DISCOVERED.value,
                         state_history=[{"state": "discovered", "at": NOW.isoformat(), "reason": "t"}],
                         detail={"source": "pump_stream", "mint": MINT, "strategy": "solana_fresh"})
    db.add(c)
    await db.commit()
    return c


async def closed_loss(db, engine: str, pnl: str, asset: str = "OTHERMINT") -> None:
    acct = await store.get_paper_account(db, "solana")
    exit_at = max(NOW - timedelta(minutes=5), NOW.replace(hour=0, minute=0, second=0))
    db.add(PaperPosition(symbol="L", provider="paper", side="LONG", entry_price=Decimal("1"), quantity=Decimal("1"),
                         stop_loss=None, take_profit=[], status="closed", exit_price=Decimal("0.5"), exit_reason="stop_loss",
                         realized_pnl=Decimal(pnl), entry_at=exit_at - timedelta(minutes=1), exit_at=exit_at,
                         account_id=acct.id, engine=engine, asset_id=asset, entry_cost_quote=Decimal("1"),
                         proceeds_quote=Decimal("1") + Decimal(pnl)))
    acct.cash_balance += Decimal(pnl)
    await db.commit()


async def test_saved_fixed_sol_amount_is_what_the_next_paper_entry_uses(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "trade_size_mode": "FIXED_SOL", "fixed_trade_size_sol": "0.03",
                                                     "max_loss_per_trade_sol": "0.02"}, None)
    await db_session.commit()
    eff, _ = await store.load_settings(db_session, "solana_fresh")  # what the engine loads
    assert (eff.trade_size_mode, eff.fixed_trade_size_sol) == ("FIXED_SOL", Decimal("0.03"))
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    assert a.plan.position_size.value == Decimal("0.03") and a.plan.position_size.method.startswith("FIXED_SOL")
    assert a.plan.max_loss.value == Decimal("0.02") and a.plan.planned_loss_at_stop <= Decimal("0.02")
    pos = (await db_session.execute(select(PaperPosition))).scalar_one()
    buy_fee = Decimal(a.plan.fixed_cost_detail["buy_network_fee"])
    assert pos.entry_cost_quote == Decimal("0.03") + buy_fee  # the configured SOL, not a share of the ~10 SOL book
    assert Decimal(pos.plan["planned_loss_at_stop"]) == a.plan.planned_loss_at_stop


async def _refusal(db_session, redis_client, size: str, max_loss: str):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "trade_size_mode": "FIXED_SOL", "fixed_trade_size_sol": size,
                                                     "max_loss_per_trade_sol": max_loss}, None)
    await db_session.commit()
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a.decision.value == "NO_TRADE"
    assert (await db_session.execute(select(PaperPosition))).first() is None
    return {x.code: x.message for x in a.findings if x.hard_block}


async def test_fixed_size_above_the_loss_limit_is_refused_with_the_reason(db_session, redis_client):
    blocks = await _refusal(db_session, redis_client, "0.2", "0.002")
    assert "0.002" in blocks["TRADE_SIZE_EXCEEDS_RISK"] and "policy REJECT" in blocks["TRADE_SIZE_EXCEEDS_RISK"]


async def test_fixed_size_above_the_pool_cap_is_refused_never_reduced(db_session, redis_client):
    blocks = await _refusal(db_session, redis_client, "0.5", "0.5")
    assert "never reduced silently" in blocks["FIXED_SIZE_ABOVE_POOL_FRACTION"]


async def test_account_daily_limit_counts_every_strategy_and_fetches_no_token_data(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "max_daily_loss_quote": "0.05"}, None)
    await db_session.commit()
    await closed_loss(db_session, "solana_momentum", "-0.06")  # another strategy on the shared book
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    rpc = FakeRpc(curve)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW)
    assert a is None and rpc.calls == []  # no RPC for an entry the account already forbids
    assert cand.detail["entry_blocked"]["codes"] == ["DAILY_LOSS_LIMIT"]
    assert "MAX_DAILY_LOSS_SOL 0.05" in cand.detail["entry_blocked"]["reason"]
    assert (await db_session.execute(select(RiskAssessment))).first() is None
    counts = await rpc_usage.entry_blocked_today(redis_client)
    assert counts["solana_fresh|DAILY_LOSS_LIMIT"] == 1 and counts["solana_fresh|*"] == 1
    # The re-check is paced at 60 s: an immediate retry does nothing at all.
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW) is None
    assert (await rpc_usage.entry_blocked_today(redis_client))["solana_fresh|*"] == 1


async def test_strategy_own_daily_limit_counts_only_its_own_losses(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "max_daily_loss_quote": "0.5"}, None)
    await store.save_settings(db_session, "solana_fresh", {"max_daily_loss_quote": "0.02"}, None)
    await db_session.commit()
    await closed_loss(db_session, "solana_momentum", "-0.1")  # not fresh's own loss, account limit not reached
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    a = await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)
    assert a is not None and a.decision.value == "EXECUTE", a.reasons


async def test_strategy_own_daily_limit_blocks_that_strategy(db_session, redis_client):
    await store.save_settings(db_session, "solana_fresh", {"max_daily_loss_quote": "0.02"}, None)
    await db_session.commit()
    await closed_loss(db_session, "solana_fresh", "-0.03")
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    rpc = FakeRpc(curve)
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW) is None
    assert cand.detail["entry_blocked"]["codes"] == ["STRATEGY_DAILY_LOSS_LIMIT"] and rpc.calls == []


async def test_manual_buy_cannot_bypass_the_daily_limit(db_session, redis_client):
    await store.save_settings(db_session, "GLOBAL", {**SOL, "max_daily_loss_quote": "0.05"}, None)
    await db_session.commit()
    await closed_loss(db_session, "solana_migration", "-0.06")
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    operator: dict = {"id": "req1", "username": "admin"}
    rpc = FakeRpc(curve)
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW, operator=operator) is None
    assert operator["result"]["status"] == "BLOCKED" and operator["result"]["reason"].startswith("DAILY_LOSS_LIMIT")
    assert rpc.calls == []


async def test_loss_cooldown_waits_before_any_token_data_without_crashing(db_session, redis_client):
    # LOSS_COOLDOWN is a WAIT, not a hard block: the pre-check read the wrong
    # attribute (Finding.decision) and crashed every evaluation during a cooldown.
    await store.save_settings(db_session, "GLOBAL", {**SOL, "cooldown_after_loss_seconds": 600}, None)
    await db_session.commit()
    await closed_loss(db_session, "solana_fresh", "-0.001")
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    rpc = FakeRpc(curve)
    assert await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, rpc), cand, NOW) is None
    assert cand.detail["entry_blocked"]["codes"] == ["LOSS_COOLDOWN"] and rpc.calls == []
