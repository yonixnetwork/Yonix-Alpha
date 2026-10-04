"""Copy engine end to end (fake EVM node, real Postgres / Redis, a seeded
pump.fun stream): gating, idempotency, mirrored partial sells, NOTIFY, the
kill switch, delay, Solana gate approval and full-exit mirroring. Proves the
logic only; NOT VERIFIED against real target wallets on chain."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core import copy_trading as ct
from yonixalpha_core import kill_switch
from yonixalpha_core.chains import verification
from yonixalpha_core.chains.base import PAPER_REQUIRED
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS
from yonixalpha_core.chains.evm.fourmeme import FourMeme
from yonixalpha_core.db.models import (CopyEvent, CopyPosition, CopyTarget, EvmToken, EvmTrade, PaperPosition,
                                       RiskAssessment)
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.flow import Trade
from yonixalpha_core.testing.evm_node import Node, enc, rpc_for

from app.engine import CopyEngine

TOKEN = "0x1111111111111111111111111111111111111111"
WHALE = "0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa"
MINT = "Mint1111111111111111111111111111111111111pump"
SOL_WHALE = "Whale11111111111111111111111111111111111111"


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.t


def fourmeme(sell_back: Decimal = Decimal("0.98")) -> tuple[Node, FourMeme]:
    node = Node(56)
    lp = FourMeme(rpc_for(node))
    node.on(lp.helper, "getTokenInfo(address)", enc(
        ["uint256", "address", "address"] + ["uint256"] * 8 + ["bool"],
        [2, lp.spec.contracts["manager_v2"], ZERO_ADDRESS, 10 ** 12, 100, 0, 1, 1, 1, 2 * 10 ** 18, 24 * 10 ** 18, False]))

    def try_buy(p):
        funds = int(p[0]["data"][-64:], 16)
        return enc(["address", "address"] + ["uint256"] * 6, [TOKEN, ZERO_ADDRESS, funds * 10 ** 6, funds, 0, 0, 0, 0])

    def try_sell(p):
        amount = int(p[0]["data"][-64:], 16)
        return enc(["address", "address", "uint256", "uint256"], [TOKEN, ZERO_ADDRESS, int(Decimal(amount) / 10 ** 6 * sell_back), 0])

    node.on(lp.helper, "tryBuy(address,uint256,uint256)", try_buy)
    node.on(lp.helper, "trySell(address,uint256)", try_sell)
    node.on(TOKEN, "totalSupply()", enc(["uint256"], [10 ** 27]))  # read by the launch-coordination check
    return node, lp


async def seed_evm(sf, clock, mode="MIRROR", created_ago=timedelta(hours=1)) -> CopyTarget:
    async with sf() as s:
        t = CopyTarget(chain="bsc", wallet=WHALE.lower(), mode=mode, enabled=True, settings={"fixed_size": "0.02"},
                       created_at=clock() - created_ago)
        s.add(t)
        s.add(EvmToken(chain="bsc", token=TOKEN, launchpad="fourmeme", symbol="MOON", created_at=clock() - timedelta(minutes=5),
                       created_block=1, category="FRESH", stage="CURVE", venue={}, stats={"volatility": "0.08"},
                       state={"liquidity_quote": "2"}, safety_verdict="PASS", safety_at=clock(), extra={"launch_seen": True}))
        await s.commit()
        return t


async def whale_trade(sf, clock, i: int, buy: bool, tokens: int, quote: int, ago=timedelta(seconds=2)) -> None:
    async with sf() as s:
        s.add(EvmTrade(event_id=f"bsc:0x{i:064x}:0", chain="bsc", launchpad="fourmeme", token=TOKEN, trader=WHALE, is_buy=buy,
                       token_amount=Decimal(tokens), quote_amount=Decimal(quote), at=clock() - ago))
        await s.commit()


async def evidence(sf, clock, key="fourmeme"):
    async with sf() as s:
        for c in PAPER_REQUIRED:
            await verification.record(s, key, c, True, {"test": True}, "test", clock())
        await s.commit()


async def event_for(sf, i: int) -> CopyEvent:
    async with sf() as s:
        return (await s.execute(select(CopyEvent).where(CopyEvent.source_event_id == f"bsc:0x{i:064x}:0"))).scalar_one()


async def events_of(sf):
    async with sf() as s:
        return list((await s.execute(select(CopyEvent).order_by(CopyEvent.detected_at, CopyEvent.target_at))).scalars())


async def test_evm_copy_is_gated_idempotent_and_mirrors_partial_sells(session_factory, redis_client):
    clock = Clock()
    node, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock)
    await whale_trade(session_factory, clock, 1, True, 10 ** 24, 10 ** 18)
    assert await eng.watch_evm("bsc") == 1
    assert await eng.watch_evm("bsc") == 0  # the same target trade is never processed twice
    ev = (await events_of(session_factory))[0]
    assert ev.decision == "SKIPPED" and ev.reason.startswith("LAUNCHPAD_NOT_VERIFIED")
    rec = ev.detail["decision_record"]  # §77: the target's buy is a trigger, not permission
    assert (rec["decision"], rec["layer"]) == ("NO_TRADE", "DATA_SAFETY") and rec["ml_evidence"]["contribution_pct"] == 0
    assert rec["wallet_evidence"]["source"] == "copy"

    await evidence(session_factory, clock)
    await whale_trade(session_factory, clock, 2, True, 10 ** 24, 10 ** 18)
    assert await eng.watch_evm("bsc") == 1
    ev = (await events_of(session_factory))[-1]
    async with session_factory() as s:
        positions = (await s.execute(select(PaperPosition))).scalars().all()
        assert len(positions) == 1 and positions[0].engine == "evm_copy_bsc", ev.reason
        cp = (await s.execute(select(CopyPosition))).scalar_one()
        assert cp.target_tokens == Decimal(10 ** 24)
        start_qty = positions[0].remaining_quantity
    assert ev.decision == "COPIED" and set(ev.latency_ms) >= {"detection", "analysis", "risk", "execution", "total"}
    rec = ev.detail["decision_record"]
    assert rec["decision"] in ("EXECUTE", "REDUCE_SIZE") and rec["wallet_evidence"]["target_mode"]
    assert rec["safety_evidence"]["verdict"] == "PASS" and rec["provider_status"]["quote_sources"]
    assert ev.latency_ms["landing"] is None and "landing" in ev.latency_ms["live_only"]  # paper: no landing, never 0

    await whale_trade(session_factory, clock, 3, False, 5 * 10 ** 23, 5 * 10 ** 17)  # target sells half
    await eng.watch_evm("bsc")
    ev = (await events_of(session_factory))[-1]
    assert ev.decision == "COPIED" and ev.detail["target_sold_fraction"] == "0.5000"
    async with session_factory() as s:
        p = (await s.execute(select(PaperPosition))).scalar_one()
        assert p.remaining_quantity == start_qty / 2 and p.status == "open"
    await whale_trade(session_factory, clock, 4, False, 5 * 10 ** 23, 5 * 10 ** 17)  # and the rest
    await eng.watch_evm("bsc")
    async with session_factory() as s:
        p = (await s.execute(select(PaperPosition))).scalar_one()
        assert p.status == "closed" and p.exit_reason == "copy_sell"
        # §41: each mirrored sell is a SELL checkpoint of the copy position (review data)
        from yonixalpha_core.db.models import EvmExitSample
        cps = (await s.execute(select(EvmExitSample).order_by(EvmExitSample.at))).scalars().all()
        assert len(cps) == 2 and all(c.engine == "evm_copy_bsc" and c.position_id == p.id for c in cps)
        assert all(c.verdicts["final"] == "SELL" and c.verdicts["deterministic"] == "SELL" for c in cps)
        assert all(any(r.startswith("copy_sell") for r in c.exit_reasons) for c in cps)


async def test_a_target_buying_into_a_bundled_launch_is_not_copied(session_factory, redis_client):
    """Master §11: a target wallet buying is not permission. Three wallets
    bought in the launch block, so the copy is skipped as LAUNCH_COORDINATION
    (counted as blocked by safety in the copy outcomes)."""
    from yonixalpha_core import copy_outcomes as co

    clock = Clock()
    node, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock)
    await evidence(session_factory, clock)
    async with session_factory() as s:
        launched = clock() - timedelta(minutes=5)
        for i in range(3):
            s.add(EvmTrade(event_id=f"bsc:0x{100 + i:064x}:0", chain="bsc", launchpad="fourmeme", token=TOKEN,
                           trader=f"0x{0xb0 + i:040x}", is_buy=True, token_amount=Decimal(10 ** 22),
                           quote_amount=Decimal(10 ** 16), block=1, at=launched))
        await s.commit()
    await whale_trade(session_factory, clock, 5, True, 10 ** 24, 10 ** 18)
    assert await eng.watch_evm("bsc") == 1
    ev = (await events_of(session_factory))[-1]
    assert ev.decision == "SKIPPED" and ev.reason.startswith("LAUNCH_COORDINATION"), ev.reason
    assert ev.detail["decision_record"]["layer"] == "TOKEN_SAFETY"
    assert "LAUNCH_BLOCK_BUNDLE" in ev.reason and co.skip_class(ev.decision, ev.reason) == "BLOCKED_BY_SAFETY"
    async with session_factory() as s:
        row = await s.get(EvmToken, ("bsc", TOKEN))
        assert row.coordination["action"] == "NO_TRADE" and row.coordination_at == clock()
        assert not (await s.execute(select(PaperPosition))).scalars().all()


async def test_notify_kill_switch_delay_and_pre_target_trades(session_factory, redis_client):
    clock = Clock()
    node, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    t = await seed_evm(session_factory, clock, mode="NOTIFY", created_ago=timedelta(minutes=5))
    await evidence(session_factory, clock)
    await whale_trade(session_factory, clock, 1, True, 10 ** 24, 10 ** 18, ago=timedelta(minutes=6))  # before the target existed
    await whale_trade(session_factory, clock, 2, True, 10 ** 24, 10 ** 18)
    await eng.watch_evm("bsc")
    evs = await events_of(session_factory)
    assert [e.decision for e in evs] == ["NOTIFIED"]
    async with session_factory() as s:
        (await s.get(CopyTarget, t.id)).mode = "BUY_ONLY"
        await s.commit()
    await kill_switch.engage(redis_client, "test")
    await whale_trade(session_factory, clock, 3, True, 10 ** 24, 10 ** 18)
    await eng.watch_evm("bsc")
    assert (await event_for(session_factory, 3)).reason.startswith("KILL_SWITCH")
    await kill_switch.disengage(redis_client)
    await whale_trade(session_factory, clock, 4, True, 10 ** 24, 10 ** 18, ago=timedelta(seconds=90))
    await eng.watch_evm("bsc")
    assert (await event_for(session_factory, 4)).reason.startswith("TOO_LATE")
    async with session_factory() as s:
        assert (await s.execute(select(PaperPosition))).scalars().all() == []


async def seed_stream(redis, clock, n=12):
    """A pump.fun curve whose price swings, with the target's buy last."""
    base_ts = int(clock().timestamp()) - 600
    vsol, vtok = 40 * 10 ** 9, 800_000_000 * 10 ** 6
    rows = []
    for i in range(n):
        step = (1 if i % 2 == 0 else -1) * (3 + i % 3) * 10 ** 8
        vsol += step
        rows.append([base_ts + i * 45, f"Trader{i:037d}", 1 if step > 0 else 0, abs(step), 10 ** 12, vsol, vtok])
    rows.append([int(clock().timestamp()) - 2, SOL_WHALE, 1, 10 ** 9, 20 * 10 ** 12, vsol, vtok])
    for r in rows:
        await redis.rpush(pump_stream.trades_key(MINT), json.dumps(r))
    await redis.hset(pump_stream.curve_key(MINT), mapping={"vsol": vsol, "vtok": vtok, "rsol": vsol - 30 * 10 ** 9,
                                                           "rtok": vtok - 200_000_000 * 10 ** 6, "fee_bps": 125,
                                                           "updated_at": rows[-1][0]})
    await redis.hset(pump_stream.meta_key(MINT), mapping={"symbol": "SOLMOON", "creator": "Creator1", "created_at": base_ts,
                                                          "name": "m", "uri": "", "bonding_curve": "", "token_program": "",
                                                          "signature": "", "is_mayhem_mode": 0,
                                                          "initial_real_token_reserves": 0})
    await redis.zadd(pump_stream.ACTIVE, {MINT: rows[-1][0]})
    await redis.set(pump_stream.HEARTBEAT, clock().isoformat())


async def test_solana_copy_needs_gate_approval_and_mirrors_partial_and_full_exits(session_factory, redis_client):
    clock = Clock()
    eng = CopyEngine(session_factory, redis_client, {}, clock)
    async with session_factory() as s:
        t = CopyTarget(chain="solana", wallet=SOL_WHALE, mode="MIRROR", enabled=True, settings={"fixed_size": "0.05"},
                       created_at=clock() - timedelta(hours=1))
        s.add(t)
        await s.commit()
    await seed_stream(redis_client, clock)
    assert await eng.watch_solana() == 1
    ev = (await events_of(session_factory))[-1]
    assert ev.decision == "SKIPPED" and ev.reason.startswith("NO_GATE_APPROVAL")  # a smart wallet buying is not enough

    async with session_factory() as s:
        s.add(RiskAssessment(idempotency_key=str(uuid.uuid4()), engine="solana_fresh", strategy="fresh", asset_id=MINT,
                             decision="PAPER_TRADE", status_label="EXECUTABLE", executable=True, execution_target="PAPER",
                             overall_risk="LOW", risk_engine_version="test", assessment={}, evaluated_at=clock()))
        await s.commit()
    await redis_client.rpush(pump_stream.trades_key(MINT), json.dumps(
        [int(clock().timestamp()) - 1, SOL_WHALE, 1, 10 ** 9, 20 * 10 ** 12, 40 * 10 ** 9, 800_000_000 * 10 ** 6]))
    await eng.watch_solana()
    ev = (await events_of(session_factory))[-1]
    assert ev.decision == "COPIED", ev.reason
    async with session_factory() as s:
        p = (await s.execute(select(PaperPosition))).scalar_one()
        assert p.engine == "copy_solana" and p.plan["venue"]["type"] == "pump_curve" and not p.exit_requested

    await redis_client.rpush(pump_stream.trades_key(MINT), json.dumps(
        [int(clock().timestamp()), SOL_WHALE, 0, 5 * 10 ** 8, 5 * 10 ** 12, 40 * 10 ** 9, 800_000_000 * 10 ** 6]))  # sells 25% (5 of 20)
    await eng.watch_solana()
    ev = (await events_of(session_factory))[-1]
    assert ev.decision == "COPIED" and ev.reason.startswith("partial exit requested"), ev.reason
    async with session_factory() as s:
        p = (await s.execute(select(PaperPosition))).scalar_one()
        # The Solana position loop sells 25 % of our copy on its next pass.
        assert ct.pending_partial_exit(p.plan) == Decimal("0.25") and not p.exit_requested
    await redis_client.rpush(pump_stream.trades_key(MINT), json.dumps(
        [int(clock().timestamp()) + 1, SOL_WHALE, 0, 3 * 10 ** 9, 30 * 10 ** 12, 40 * 10 ** 9, 800_000_000 * 10 ** 6]))
    clock.t += timedelta(seconds=2)
    await eng.watch_solana()
    assert (await events_of(session_factory))[-1].decision == "COPIED"
    async with session_factory() as s:
        assert (await s.execute(select(PaperPosition))).scalar_one().exit_requested is True


async def own_position(sf, clock, asset: str, engine: str, mode: str = "PAPER") -> uuid.UUID:
    async with sf() as s:
        p = PaperPosition(symbol="OWN", provider="paper", side="LONG", entry_price=Decimal("0.000001"),
                          quantity=Decimal(1000), remaining_quantity=Decimal(1000), entry_at=clock(), status="open",
                          engine=engine, asset_id=asset, execution_mode=mode, plan={})
        s.add(p)
        await s.commit()
        return p.id


async def test_sell_only_target_queues_exits_on_our_own_paper_positions(session_factory, redis_client):
    """SELL_ONLY (section 29): the target's buys are never copied; its sells
    queue the same fraction of OUR open paper position in the token for the
    owning service to fill. An unobserved holding sells nothing."""
    clock = Clock()
    _, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock, mode="SELL_ONLY")
    await evidence(session_factory, clock)
    pid = await own_position(session_factory, clock, TOKEN, "evm_bsc")

    await whale_trade(session_factory, clock, 1, True, 10 ** 24, 10 ** 18, ago=timedelta(seconds=10))
    await eng.watch_evm("bsc")
    ev = await event_for(session_factory, 1)
    assert ev.decision == "SKIPPED" and ev.reason.startswith("SELL_ONLY_TARGET")
    async with session_factory() as s:
        assert len((await s.execute(select(PaperPosition))).scalars().all()) == 1  # no copy position opened

    await whale_trade(session_factory, clock, 2, False, 25 * 10 ** 22, 10 ** 17)  # sells a quarter of what it held
    await eng.watch_evm("bsc")
    ev = await event_for(session_factory, 2)
    assert ev.decision == "COPIED" and ev.detail["target_sold_fraction"] == "0.2500" and ev.position_id == pid
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert ct.pending_partial_exit(p.plan) == Decimal("0.25") and p.status == "open"


async def test_sell_only_never_guesses_an_unobserved_holding(session_factory, redis_client):
    clock = Clock()
    _, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock, mode="SELL_ONLY")
    pid = await own_position(session_factory, clock, TOKEN, "evm_bsc")
    await whale_trade(session_factory, clock, 1, False, 10 ** 23, 10 ** 17)  # a sell, its buy never seen
    await eng.watch_evm("bsc")
    ev = await event_for(session_factory, 1)
    assert ev.decision == "SKIPPED" and ev.reason.startswith("TARGET_HOLDING_UNKNOWN")
    async with session_factory() as s:
        assert ct.pending_partial_exit((await s.get(PaperPosition, pid)).plan) is None


async def test_sell_only_on_solana_touches_paper_positions_never_live_ones(session_factory, redis_client):
    clock = Clock()
    eng = CopyEngine(session_factory, redis_client, {}, clock)
    async with session_factory() as s:
        s.add(CopyTarget(chain="solana", wallet=SOL_WHALE, mode="SELL_ONLY", enabled=True, settings={},
                         created_at=clock() - timedelta(hours=1)))
        await s.commit()
    paper_id = await own_position(session_factory, clock, MINT, "solana_fresh")
    live_id = await own_position(session_factory, clock, MINT, "solana_momentum", mode="LIVE")
    buy = Trade(clock() - timedelta(seconds=20), SOL_WHALE, True, 10 ** 9, 4 * 10 ** 12, 0, 0)
    sell = Trade(clock() - timedelta(seconds=2), SOL_WHALE, False, 10 ** 9, 10 ** 12, 0, 0)
    o = await eng._solana_sell_only((await eng._targets("solana"))[0], MINT, sell, [buy, sell])
    assert o.decision == "COPIED" and o.detail["target_sold_fraction"] == "0.2500"
    async with session_factory() as s:
        assert ct.pending_partial_exit((await s.get(PaperPosition, paper_id)).plan) == Decimal("0.25")
        assert ct.pending_partial_exit((await s.get(PaperPosition, live_id)).plan) is None


async def test_every_target_buy_gets_a_paper_outcome_after_the_horizon_once(session_factory, redis_client):
    """§35: a skipped buy (launchpad not yet verified) is still measured: entry
    at the first trade after we saw it, exit at the target's own sell (MIRROR).
    Not before the horizon, and only once."""
    clock = Clock()
    node, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock)
    await whale_trade(session_factory, clock, 1, True, 10 ** 24, 10 ** 18)  # price 0.000001
    await eng.watch_evm("bsc")
    seen = clock()
    other = "0x" + "c" * 40
    async with session_factory() as s:
        for i, (mins, trader, buy, quote) in enumerate([(5, other, True, 15 * 10 ** 17), (10, WHALE, False, 13 * 10 ** 17),
                                                       (30, other, False, 8 * 10 ** 17)]):
            s.add(EvmTrade(event_id=f"bsc:0x{100 + i:064x}:0", chain="bsc", launchpad="fourmeme", token=TOKEN, trader=trader,
                           is_buy=buy, token_amount=Decimal(10 ** 24), quote_amount=Decimal(quote),
                           at=seen + timedelta(minutes=mins)))
        await s.commit()
    assert await eng.evaluate_outcomes() == {"evaluated": 0, "no_price_data": 0}  # horizon not reached
    clock.t = seen + timedelta(minutes=61)
    assert await eng.evaluate_outcomes() == {"evaluated": 1, "no_price_data": 0}
    ev = await event_for(session_factory, 1)
    o = ev.outcome
    assert ev.decision == "SKIPPED" and o["class"] == "BLOCKED_BY_SAFETY" and o["reason_code"] == "LAUNCHPAD_NOT_VERIFIED"
    assert (o["simulated_entry"], o["exit_by"], o["simulated_exit"]) == ("0.0000015", "TARGET_SELL", "0.0000013")
    assert o["label"] == "WOULD_HAVE_LOST" and o["min_return_pct"] < -40 and o["mode"] == "MIRROR"
    assert await eng.evaluate_outcomes() == {"evaluated": 0, "no_price_data": 0}  # evaluated once


async def test_a_solana_buy_older_than_the_stream_history_is_no_price_data(session_factory, redis_client):
    clock = Clock()
    eng = CopyEngine(session_factory, redis_client, {}, clock)
    async with session_factory() as s:
        t = CopyTarget(chain="solana", wallet=SOL_WHALE, mode="NOTIFY", enabled=True, settings={},
                       created_at=clock() - timedelta(hours=5))
        s.add(t)
        await s.flush()
        s.add(CopyEvent(id=uuid.uuid4(), target_id=t.id, chain="solana", wallet=SOL_WHALE, token=MINT, side="BUY",
                        source_event_id="solana:x", target_token_amount=Decimal(10 ** 12), target_quote_amount=Decimal(10 ** 9),
                        target_at=clock() - timedelta(hours=4), detected_at=clock() - timedelta(hours=4),
                        decision="NOTIFIED", reason="notify-only target"))
        await s.commit()
    assert await eng.evaluate_outcomes() == {"evaluated": 0, "no_price_data": 1}
    async with session_factory() as s:
        ev = (await s.execute(select(CopyEvent))).scalar_one()
    assert ev.outcome["status"] == "NO_PRICE_DATA" and ev.outcome["class"] == "NOTIFY_ONLY" and "3 hours" in ev.outcome["reason"]


async def test_stream_lead_is_recorded_when_a_stream_saw_the_target_first(session_factory, redis_client):
    """Master §9 / §13: the target's transaction was on the sequencer feed 1.5 s
    before the confirmed trade was detected; the copy event records that lead
    (the decision itself still uses the confirmed trade)."""
    clock = Clock()
    node, lp = fourmeme()
    eng = CopyEngine(session_factory, redis_client, {"bsc": {"fourmeme": lp}}, clock)
    await seed_evm(session_factory, clock)
    tx = "0x" + "ab" * 32
    async with session_factory() as s:
        s.add(EvmTrade(event_id=f"bsc:{tx}:0", chain="bsc", launchpad="fourmeme", token=TOKEN, trader=WHALE, is_buy=True,
                       token_amount=Decimal(10 ** 24), quote_amount=Decimal(10 ** 18), at=clock() - timedelta(seconds=2),
                       tx_hash=tx))
        await s.commit()
    await redis_client.set(f"yx:evm:seen:bsc:{tx}", json.dumps(
        {"hash": tx, "source": "pending_tx", "seen_at": (clock() - timedelta(milliseconds=1500)).isoformat()}))
    assert await eng.watch_evm("bsc") == 1
    ev = (await events_of(session_factory))[-1]
    assert ev.latency_ms["stream_source"] == "pending_tx" and ev.latency_ms["stream_lead"] == 1500


async def test_wallet_enrichment_step_is_off_until_switched_on(session_factory, redis_client):
    """Nansen / MadeOnSol cost credits: without settings, or with keys but the
    switch off (the default), the step calls nothing."""
    from types import SimpleNamespace

    clock = Clock()
    assert await CopyEngine(session_factory, redis_client, {}, clock).enrich() == {"status": "NOT_CONFIGURED"}
    eng = CopyEngine(session_factory, redis_client, {}, clock,
                     settings=SimpleNamespace(NANSEN_API_KEY="nk", MADEONSOL_API_KEY="mk"))
    assert await eng.enrich() == {"profiles": {"status": "OFF"}, "discovery": {"status": "OFF"}}
