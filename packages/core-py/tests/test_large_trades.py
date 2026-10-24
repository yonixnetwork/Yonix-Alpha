"""Large-buy observation: the definition is applied exactly (liquidity and
recent-volume tests, a quiet token is not measured, one event per cooldown),
each event gets an ordinary control moment, what followed is measured from
stored trades only (new buyers, earlier holders selling, the large buyer's
own selling), a truncated history is INCOMPLETE, and the study splits by
time and withholds a finding without enough hold-out pairs."""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import json  # noqa: E402

import pytest  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import large_trades as lt  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import LargeTradeEvent  # noqa: E402
from yonixalpha_core.solana import pump_stream  # noqa: E402
from yonixalpha_core.solana.flow import Trade  # noqa: E402
from yonixalpha_core.tools import large_buy_study  # noqa: E402

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SOL = 1_000_000_000


def tr(sec, trader, buy, sol, vsol=30 * SOL, vtok=1_000_000_000_000, tok=1_000_000):
    return Trade(T0 + timedelta(seconds=sec), trader, buy, int(sol * SOL), tok, int(vsol), vtok)


def test_the_definition_needs_volume_context_and_a_large_share_of_liquidity():
    pre = [tr(-50, "a", True, 0.05), tr(-40, "b", True, 0.1), tr(-30, "c", False, 0.05)]
    big = tr(0, "W", True, 1.0)  # 1 SOL: 10 x the median buy (0.075), 3.3 % of 30 SOL
    ok, info = lt.qualifies(pre + [big], big)
    assert ok and info["rel_volume"] >= lt.MIN_REL_VOLUME and info["rel_liquidity"] >= 0.02
    assert not lt.qualifies(pre[:2] + [big], big)[0]  # 2 trades before: too quiet to measure
    busy = [tr(-50 + i, f"x{i}", True, 0.5) for i in range(5)]
    assert not lt.qualifies(busy + [big], big)[0]  # only 2 x the usual buy: not large here
    deep = tr(0, "W", True, 1.0, vsol=200 * SOL)
    assert not lt.qualifies(pre + [deep], deep)[0]  # 0.5 % of a deep curve


def test_what_followed_counts_new_buyers_earlier_holders_selling_and_the_buyer_itself():
    hist = [tr(-50, "h1", True, 0.1), tr(-40, "h2", True, 0.1), tr(-30, "x", True, 0.1)]
    ev = tr(0, "W", True, 1.0, tok=5_000_000)
    after = [tr(10, "n1", True, 0.2, vsol=33 * SOL), tr(20, "h1", False, 0.15, vsol=32 * SOL),
             tr(200, "W", False, 0.5, vsol=31 * SOL, tok=2_500_000), tr(250, "n2", True, 0.1, vsol=31.5 * SOL)]
    post = lt.post_state(hist + [ev] + after, ev.at, ev.virtual_sol / ev.virtual_token, "W", 5_000_000)
    assert post["30s"]["new_buyers"] == 1 and post["30s"]["prior_holder_sells"] == 1
    assert post["30s"]["prior_holder_sell_sol"] == 0.15 and post["30s"]["large_buyer_sold_share"] == 0
    assert post["5m"]["new_buyers"] == 2 and post["5m"]["large_buyer_sold_share"] == 0.5  # the buyer's own sale is apart
    assert post["5m"]["prior_holder_sells"] == 1  # the large buyer is never counted as an earlier holder
    assert post["30s"]["return_pct"] > 0 and post["15m"]["trades"] == 4


@pytest.fixture
async def env():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    redis = from_url(os.environ["REDIS_URL"], decode_responses=True)  # as yonixalpha_core.db.redis does
    await redis.flushdb()
    yield make_session_factory(engine), redis
    await redis.flushdb()
    await redis.aclose()
    await engine.dispose()


async def _store(redis, mint, trades):
    for t in trades:
        await redis.rpush(pump_stream.trades_key(mint), json.dumps(
            [int(t.at.timestamp()), t.trader, 1 if t.is_buy else 0, t.sol_lamports, t.token_raw, t.virtual_sol,
             t.virtual_token]))
        await redis.zadd(pump_stream.ACTIVE, {mint: int(t.at.timestamp())})


@pytest.mark.asyncio
async def test_an_event_and_its_control_are_recorded_then_measured(env):
    sf, redis = env
    base = [tr(-50, "a", True, 0.05), tr(-40, "b", True, 0.1), tr(-30, "c", False, 0.05)]
    ev = tr(0, "W", True, 1.0)
    await _store(redis, "EVT", base + [ev])
    await _store(redis, "CTL", [tr(-45, "p", True, 0.1), tr(-35, "q", True, 0.1), tr(-5, "r", False, 0.05)])
    await redis.hset(pump_stream.meta_key("EVT"), mapping={"creator": "someone", "created_at": int(T0.timestamp()) - 600})
    await redis.zadd(pump_stream.LARGE_CANDIDATES, {f"EVT|{int(ev.at.timestamp())}|W|{ev.sol_lamports}": ev.at.timestamp()})
    async with sf() as s:
        out = await lt.process(s, redis, T0 + timedelta(seconds=5))
        await s.commit()
        assert out == {"candidates": 1, "events": 1, "controls": 1, "rejected": 0}
        rows = {r.kind: r for r in (await s.execute(select(LargeTradeEvent))).scalars()}
        assert rows["CONTROL"].pair_id == rows["EVENT"].id and rows["CONTROL"].mint == "CTL"
        assert rows["EVENT"].pre["large_buyer_is_creator"] is False and rows["EVENT"].pre["token_age_s"] == 600
        # the same token again within the cooldown is not a second event
        await redis.zadd(pump_stream.LARGE_CANDIDATES, {f"EVT|{int(ev.at.timestamp())}|W|{ev.sol_lamports}": ev.at.timestamp()})
        assert (await lt.process(s, redis, T0 + timedelta(seconds=6)))["rejected"] == 1
        # what followed: measured once the last horizon has passed
        await _store(redis, "EVT", [tr(20, "n1", True, 0.3, vsol=32 * SOL), tr(100, "a", False, 0.06, vsol=31 * SOL)])
        assert (await lt.track(s, redis, T0 + timedelta(seconds=60)))["completed"] == 0  # too early
        done = await lt.track(s, redis, T0 + timedelta(seconds=lt.COMPLETE_AFTER + 1))
        await s.commit()
        assert done["completed"] == 2
        ev_row = (await s.execute(select(LargeTradeEvent).where(LargeTradeEvent.kind == "EVENT"))).scalar_one()
        assert ev_row.status == "COMPLETE" and ev_row.post["5m"]["new_buyers"] == 1
        assert ev_row.post["5m"]["prior_holder_sells"] == 1
        study = await large_buy_study.build(s, 14, T0 + timedelta(hours=1))
        assert study["complete_pairs"] == 1 and study["with_control"] == 1
        assert all(t["finding"] == "INSUFFICIENT DATA" for t in study["comparisons"].values())
        assert "Nothing was written" in large_buy_study.render(study)


@pytest.mark.asyncio
async def test_a_truncated_history_is_incomplete_never_guessed(env):
    sf, redis = env
    async with sf() as s:
        s.add(LargeTradeEvent(kind="EVENT", mint="GONE", at=T0, pre={}, status="TRACKING",
                              definition_version=lt.DEFINITION_VERSION))
        await s.commit()
        await _store(redis, "GONE", [tr(500, "z", True, 0.1)])  # history starts after the event
        out = await lt.track(s, redis, T0 + timedelta(seconds=lt.COMPLETE_AFTER + 1))
        await s.commit()
        row = (await s.execute(select(LargeTradeEvent))).scalar_one()
    assert out["incomplete"] == 1 and row.status == "INCOMPLETE" and "no longer covers" in row.post["why"]


@pytest.mark.asyncio
async def test_the_stream_notes_only_buys_large_for_the_curve(env):
    from yonixalpha_core.solana.codec import b58encode
    from yonixalpha_core.testing.pump import Curve, logs_of, wallet

    _, redis = env
    mint = b58encode(bytes([55]) * 32)
    c = Curve(mint)
    logs = logs_of(c.trade(wallet(1), T0, 1 * SOL, True),            # ~3 % of ~31 SOL reserves: noted
                   c.trade(wallet(2), T0, int(0.35 * SOL), True),   # above 0.3 SOL but ~1 %: not noted
                   c.trade(wallet(3), T0, int(0.5 * SOL), False))   # a sell: never noted
    await pump_stream.ingest_logs(redis, logs, "sig", T0)
    noted = await redis.zrange(pump_stream.LARGE_CANDIDATES, 0, -1)
    assert len(noted) == 1 and noted[0].startswith(f"{mint}|{int(T0.timestamp())}|{wallet(1)}|")
