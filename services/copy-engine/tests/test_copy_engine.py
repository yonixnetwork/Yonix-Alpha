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
    assert ev.latency_ms["landing"] == "not applicable (paper)"

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
