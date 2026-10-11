"""Migration detection without the migration event (2026-10-11): pump.fun's
migrate_v2 left almost no CompletePumpAmmMigrationEvent in the transaction
logs, so migrated tokens stopped reaching the migration engine. A curve
the stream saw close to completion is checked for its PumpSwap pool; a pool
that exists is recorded as the event would be, and the funnel hands the
token to the migration engine. No network."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core.db.models import TradingCandidate
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import migration_watch, pump_stream, pumpswap
from yonixalpha_core.solana.launch_features import REAL_TOKEN_OFFSET
from yonixalpha_core.testing.pump import MINT, Curve, logs_of, wallet

from app.funnel import run_funnel

OTHER = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


class PoolRpc:
    def __init__(self, existing: set[str]):
        self.existing, self.calls = existing, []

    async def call(self, method, params=None, priority=None):
        self.calls.append((method, len(params[0]) if params else 0, priority))
        assert method == "getMultipleAccounts"
        return {"value": [{"owner": pumpswap.PUMP_AMM_PROGRAM, "lamports": 1, "data": ["", "base64"]}
                          if p in self.existing else None for p in params[0]]}


async def test_a_trade_near_completion_marks_the_curve_and_far_trades_do_not(redis):
    now = datetime.now(timezone.utc)
    far = Curve(OTHER)
    await pump_stream.ingest_logs(redis, logs_of(far.trade(wallet(1), now, 10**9, True)), "sig-far", now)
    near = Curve(MINT)
    near.vtok = REAL_TOKEN_OFFSET + 20_000_000_000_000  # about 2.5% of the curve's tokens left
    await pump_stream.ingest_logs(redis, logs_of(near.trade(wallet(2), now, 10**9, True)), "sig-near", now)
    members = await redis.zrange(pump_stream.NEAR_COMPLETE, 0, -1)
    assert members == [MINT]


async def test_an_existing_pool_is_recorded_as_a_migration_and_reaches_the_migration_engine(redis, session_factory):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t = int(now.timestamp())
    await redis.zadd(pump_stream.NEAR_COMPLETE, {MINT: t - 30, OTHER: t - 20})
    await redis.hset(pump_stream.meta_key(MINT), mapping={"symbol": "MIG", "created_at": t - 3600})
    await redis.hset(pump_stream.curve_key(MINT), mapping={"vsol": 80 * 10**9, "vtok": REAL_TOKEN_OFFSET + 10**12,
                                                           "updated_at": t - 30})
    rpc = PoolRpc({pumpswap.canonical_pool(MINT)})
    out = await migration_watch.detect(redis, rpc, now)
    assert out == {"near_complete": 2, "checked": 2, "detected": 1}
    assert rpc.calls == [("getMultipleAccounts", 2, "background")]  # one call for every pending curve
    assert await pump_stream.migrated_since(redis, t - 60) == [(MINT, t)]
    curve = await pump_stream.load_curve(redis, MINT)
    assert curve.complete and curve.pool == pumpswap.canonical_pool(MINT)
    assert await redis.zrange(pump_stream.NEAR_COMPLETE, 0, -1) == [OTHER]  # still waiting for its pool
    assert int(await redis.hget(pump_stream.STATS, migration_watch.STATS_FIELD)) == 1
    counts = await run_funnel(redis, session_factory, default_settings_for("solana_fresh"), now + timedelta(seconds=5))
    assert counts["migrations"] == 1
    async with session_factory() as s:
        [c] = (await s.execute(select(TradingCandidate))).scalars().all()
    assert c.engine == "migration" and c.detail["pool"] == pumpswap.canonical_pool(MINT)


async def test_a_migration_already_seen_through_its_event_is_not_checked_again(redis):
    now = datetime.now(timezone.utc)
    t = int(now.timestamp())
    await redis.zadd(pump_stream.NEAR_COMPLETE, {MINT: t - 30})
    await redis.zadd(pump_stream.MIGRATED, {MINT: t - 10})
    rpc = PoolRpc(set())
    assert (await migration_watch.detect(redis, rpc, now))["checked"] == 0
    assert rpc.calls == [] and await redis.zrange(pump_stream.NEAR_COMPLETE, 0, -1) == []
    assert await pump_stream.migrated_since(redis, t - 60) == [(MINT, t - 10)]  # the event's time is kept
