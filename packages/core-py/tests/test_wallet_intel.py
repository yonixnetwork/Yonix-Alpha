"""Wallet intelligence: early buyers, causal Beta reputation, recycled
wallets and dump cohorts; and the gate's (configurable) use of a HIGH dump
cluster."""

import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import wallet_intel as wi  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import LaunchBuyer  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import FinalDecision  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings, validate  # noqa: E402
from yonixalpha_core.solana.flow import Trade  # noqa: E402

from tests.test_launch_features import T0, Curve  # noqa: E402
from tests.test_safety_gate import healthy  # noqa: E402

CFG = wi.WalletConfig(early_buyers=8)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def launch_with(wallets: list[str], dumpers: set[str], start=T0):
    """Each wallet buys 0.3 SOL in turn; `dumpers` sell everything 30 s later."""
    c = Curve()
    trades = [c.trade(w, start + timedelta(seconds=1 + i), 0.3, True) for i, w in enumerate(wallets)]
    got = {t.trader: t.token_raw for t in trades}
    for i, w in enumerate(sorted(dumpers)):
        k = c.vsol * c.vtok
        c.vtok += got[w]
        new_sol = k // c.vtok
        sol = c.vsol - new_sol
        c.vsol = new_sol
        trades.append(Trade(start + timedelta(seconds=40 + i), w, False, sol, got[w], c.vsol, c.vtok))
    return trades


# --- pure ----------------------------------------------------------------------------

def test_early_buyers_and_early_sells():
    trades = launch_with(["a", "b", "c", "d"], {"b"})
    buyers = wi.early_buyers(trades, 3)
    assert [b["wallet"] for b in buyers] == ["a", "b", "c"] and buyers[0]["rank"] == 1 and buyers[0]["sol_lamports"] == 3 * 10**8
    share, covered = wi.early_sold_share(trades, "b", buyers[1]["first_buy_at"], 120, buyers[1]["tokens"])
    assert share == 1.0 and covered is True
    assert wi.early_sold_share(trades[3:], "b", buyers[1]["first_buy_at"], 120, buyers[1]["tokens"])[1] is False  # trimmed


def test_outcome_labels_and_beta_shrinkage():
    assert wi.launch_outcome(Decimal("60"), Decimal("-10"), CFG) == "WIN"
    assert wi.launch_outcome(Decimal("10"), Decimal("-70"), CFG) == "LOSS"
    assert wi.launch_outcome(Decimal("10"), Decimal("-10"), CFG) == "FLAT" and wi.launch_outcome(None, None, CFG) is None
    lucky = wi.beta_reputation(2, 2, 0.2, 10)  # 2 of 2: the prior keeps it close to the base rate
    proven = wi.beta_reputation(12, 9, 0.2, 10)
    assert lucky["lower"] < 0.2 < proven["lower"] and lucky["mean"] < 0.4


def test_cohorts_link_only_wallets_that_dumped_together_often_enough():
    edges = {("a", "b"): 3, ("b", "c"): 4, ("c", "d"): 1, ("x", "y"): 9}
    groups = wi.cohorts(["a", "b", "c", "d", "e"], edges, 3)
    assert groups == [{"a", "b", "c"}]
    stats = {"a": {"n": 4, "dumps": 4}, "b": {"n": 4, "dumps": 3}, "c": {"n": 3, "dumps": 3}, "d": {"n": 0}}
    assert wi.dump_cluster(stats, groups, CFG)["level"] == "MEDIUM"
    assert wi.dump_cluster(stats, groups, replace(CFG, cluster_high_wallets=3))["level"] == "HIGH"
    assert wi.dump_cluster({"d": {}}, [], CFG)["level"] == "UNKNOWN"
    assert wi.dump_cluster({"d": {"n": 5, "dumps": 0}}, [], CFG)["level"] == "LOW"


# --- recorded history ------------------------------------------------------------------

async def test_dump_cohort_is_learned_only_from_resolved_launches(db, redis):
    cohort = ["d1", "d2", "d3", "d4"]
    for n in range(3):  # three earlier launches: the cohort buys first and dumps together, the launch fails
        mint = f"Old{n}"
        start = T0 + timedelta(minutes=10 * n)
        trades = launch_with(cohort + [f"o{n}{i}" for i in range(3)], set(cohort), start)
        assert await wi.record_launch(db, redis, mint, trades, int(start.timestamp()), None,
                                      start + timedelta(seconds=300), CFG) == 7
    await db.commit()
    new = launch_with(cohort + ["fresh1", "fresh2"], set(), T0 + timedelta(hours=1))
    now = T0 + timedelta(hours=1, seconds=20)
    before = await wi.assess(redis, new, now, CFG, complete_history=True, created_at=T0 + timedelta(hours=1), mint="New")
    assert before["dump_cluster"]["level"] == "UNKNOWN"  # recorded but not resolved: nothing is known yet
    rows = (await db.execute(select(LaunchBuyer).where(LaunchBuyer.mint == "Old0", LaunchBuyer.wallet == "d1"))).scalar_one()
    assert rows.sold_early is True and rows.early_window_closed is True and rows.outcome is None
    for n in range(3):
        res = await wi.resolve(db, redis, f"Old{n}", Decimal("5"), Decimal("-70"), False, T0 + timedelta(minutes=40), CFG)
        assert res == {"outcome": "LOSS", "buyers": 7, "dumpers": 4}
    await db.commit()
    assert await wi.resolve(db, redis, "Old0", Decimal("5"), Decimal("-70"), False, now, CFG) is None  # once only
    after = await wi.assess(redis, new, now, CFG, complete_history=True, created_at=T0 + timedelta(hours=1), mint="New")
    dc = after["dump_cluster"]
    assert dc["level"] == "HIGH" and dc["largest_cohort"] == 4 and "sold early together" in dc["evidence"][0]
    assert after["smart_money"]["status"] == "UNKNOWN"  # 21 buyer-launches: the base rate itself is not known yet
    # The same history rebuilt from the database, as of a time before / after it resolved.
    assert await wi.reputation_asof(db, ["d1"], T0 + timedelta(minutes=30)) == {}
    assert (await wi.reputation_asof(db, ["d1"], now))["d1"] == {"n": 3, "wins": 0, "dumps": 3}


async def test_smart_money_needs_a_base_rate_and_a_lower_bound_above_it(db, redis):
    await redis.hset(wi.BASE, mapping={"n": 500, "wins": 100})  # base rate 0.2
    await redis.hset(wi.REP + "sm", mapping={"n": 12, "wins": 9})
    await redis.hset(wi.REP + "lucky", mapping={"n": 2, "wins": 2})
    trades = launch_with(["sm", "lucky", "x", "y"], set())
    r = await wi.assess(redis, trades, T0 + timedelta(seconds=30), CFG, complete_history=True, created_at=T0, mint="M")
    sm = r["smart_money"]
    assert sm["status"] == "MEASURED" and sm["proven_wallets"] == 1 and sm["proven"][0]["wallet"] == "sm"
    assert sm["proven_capital_sol"] == "0.3000" and sm["first_proven_arrival_seconds"] == 1.0
    assert "never a BUY trigger" in sm["note"]
    none = await wi.assess(redis, trades, T0 + timedelta(seconds=30), CFG, complete_history=False, created_at=T0)
    assert none["smart_money"]["status"] == "UNKNOWN" and none["dump_cluster"]["level"] == "UNKNOWN"


async def test_base_rate_without_any_win_yet_is_zero_not_an_error(db, redis):
    await redis.hset(wi.BASE, mapping={"n": 150})  # 150 resolved, no WIN yet: "wins" does not exist
    await redis.hset(wi.REP + "x", mapping={"n": 6, "losses": 6})
    r = await wi.assess(redis, launch_with(["x", "y"], set()), T0 + timedelta(seconds=10), CFG,
                        complete_history=True, created_at=T0, mint="M")
    assert r["smart_money"]["status"] == "MEASURED" and r["smart_money"]["base_rate"] == 0.0
    assert r["smart_money"]["proven_wallets"] == 0


async def test_recycled_wallets_count_other_launches_in_the_last_day(db, redis):
    for n in range(5):
        await redis.zadd(wi.SEEN + "bot", {f"L{n}": (T0 - timedelta(hours=n + 1)).timestamp()})
    await redis.zadd(wi.SEEN + "human", {"L0": (T0 - timedelta(hours=1)).timestamp()})
    r = await wi.assess(redis, launch_with(["bot", "human"], set()), T0 + timedelta(seconds=10), CFG,
                        complete_history=True, created_at=T0, mint="M")
    assert r["recycled_wallets"] == ["bot"]


# --- gate -------------------------------------------------------------------------------

def test_dump_cluster_high_is_a_warning_by_default_and_blocks_only_when_configured():
    intel = {"stage": "FRESH", "regime": {"mayhem": False, "curve_math": {"valid": True}},
             "wallets": {"dump_cluster": {"level": "HIGH", "evidence": ["cohort of 4 wallets"]}}}
    a = assess(healthy(intel=intel), SafetySettings())
    f = [x for x in a.findings if x.code == "DUMP_CLUSTER_HIGH"]
    assert f and f[0].action == FinalDecision.EXECUTE and "not an accusation" in f[0].message
    b = assess(healthy(intel=intel), replace(SafetySettings(), dump_cluster_high_action="NO_TRADE"))
    assert b.decision == FinalDecision.NO_TRADE


def test_wallet_settings_validate():
    assert validate(SafetySettings()) == []
    assert validate(replace(SafetySettings(), dump_cluster_medium_wallets=5, dump_cluster_high_wallets=4))
    assert validate(replace(SafetySettings(), dump_cluster_high_action="BUY"))


async def _history(db, redis, peak, drawdown):
    cohort = ["d1", "d2", "d3", "d4"]
    for n in range(3):
        start = T0 + timedelta(minutes=10 * n)
        trades = launch_with(cohort + [f"o{n}{i}" for i in range(3)], set(cohort), start)
        await wi.record_launch(db, redis, f"Old{n}", trades, int(start.timestamp()), None, start + timedelta(seconds=300), CFG)
    await db.commit()
    for n in range(3):
        await wi.resolve(db, redis, f"Old{n}", Decimal(peak), Decimal(drawdown), False, T0 + timedelta(minutes=40), CFG)
    await db.commit()
    return launch_with(cohort + ["fresh1"], set(), T0 + timedelta(hours=1))


async def test_selling_early_into_a_flat_launch_is_flipping_not_dumping(db, redis):
    new = await _history(db, redis, "10", "-20")  # FLAT: neither a win nor a collapse
    r = await wi.assess(redis, new, T0 + timedelta(hours=1, seconds=20), CFG, complete_history=True,
                        created_at=T0 + timedelta(hours=1), mint="New")
    assert r["dump_cluster"]["level"] == "LOW" and r["dump_cluster"]["largest_cohort"] == 0
    assert (await wi.reputation_asof(db, ["d1"], T0 + timedelta(hours=1)))["d1"]["dumps"] == 0


async def test_counters_are_rebuilt_once_from_the_table(db, redis):
    new = await _history(db, redis, "5", "-70")  # LOSS: a real dump cohort
    now = T0 + timedelta(hours=1, seconds=20)
    before = await wi.assess(redis, new, now, CFG, complete_history=True, created_at=T0 + timedelta(hours=1), mint="New")
    for k in await redis.keys("yx:wi2:*"):  # counters lost (or written under the old definition)
        await redis.delete(k)
    assert await wi.rebuild_counters(db, redis, now, CFG) == 3
    assert await wi.rebuild_counters(db, redis, now, CFG) is None  # once only: never double-counted
    after = await wi.assess(redis, new, now, CFG, complete_history=True, created_at=T0 + timedelta(hours=1), mint="New")
    assert after["dump_cluster"] == before["dump_cluster"] and after["dump_cluster"]["level"] == "HIGH"
    assert await redis.hgetall(wi.BASE) == {"n": "21"}
