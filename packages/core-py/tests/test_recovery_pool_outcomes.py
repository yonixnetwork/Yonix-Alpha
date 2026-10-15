"""R6 (2026-10-15): outcome horizons of migrated tokens are priced from
recorded PumpSwap trades, never from the frozen bonding-curve stream
(every migrated token used to get the graduation price 0.00041088...).
A horizon is priced only where one read of the pool proves no trade is
missing; otherwise it is UNAVAILABLE."""

from datetime import timedelta
from decimal import Decimal

from solders.pubkey import Pubkey
from sqlalchemy import select

from yonixalpha_core import opportunities as opp
from yonixalpha_core.db.models import OpportunityOutcome
from yonixalpha_core.solana import pump_stream, pumpswap

from tests.test_opportunities import T0, VT, db, put_trades, redis  # noqa: F401 - fixtures

MINT = str(Pubkey.new_unique())
POOL = pumpswap.canonical_pool(MINT)
GRAD = 410_880_000_000  # virtual SOL lamports at graduation: price_raw 0.00041088 with VT
POOL_BASE = 10**15


def ptrade(sec: int, quote_lamports: int, buy: bool = True) -> pumpswap.PoolTrade:
    return pumpswap.PoolTrade(T0 + timedelta(seconds=sec), f"u{sec}", buy, 10**9, 10**8, POOL_BASE, quote_lamports, 125, POOL)


async def migration_row(db, redis):  # noqa: F811 - the fixtures, passed through
    await put_trades(redis, MINT, [(-300, GRAD // 2), (-60, GRAD)])  # curve history up to graduation
    await redis.zadd(pump_stream.MIGRATED, {MINT: (T0 - timedelta(seconds=30)).timestamp()})
    await redis.hset(pump_stream.curve_key(MINT), mapping={"vsol": GRAD, "vtok": VT, "updated_at": int(T0.timestamp()),
                                                           "fee_bps": 125})
    snap = {"price_raw": str(Decimal(500_000_000_000) / Decimal(POOL_BASE)), "migrated": True}
    await opp.record(db, key="gate:mig", mint=MINT, symbol="MIG", engine="solana_migration", stage="GATE", decision="REJECT",
                     traded=False, reasons=["ESTABLISHED_TOKEN_NOT_MIGRATION"], decided_at=T0, snapshot=snap)
    await db.commit()


async def test_migrated_horizons_use_recorded_pool_trades_and_never_the_graduation_price(db, redis):  # noqa: F811
    await migration_row(db, redis)
    # Two reads of the pool while the token was evaluated: each returned the newest trades.
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(-20, 480_000_000_000), ptrade(3, 500_000_000_000),
                                                    ptrade(8, 550_000_000_000)], now=(T0 + timedelta(seconds=40)).timestamp())
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(8, 550_000_000_000), ptrade(200, 750_000_000_000),
                                                    ptrade(290, 600_000_000_000, buy=False)],
                                      now=(T0 + timedelta(seconds=320)).timestamp())
    await opp.track(db, redis, T0 + timedelta(seconds=3700))
    row = (await db.execute(select(OpportunityOutcome))).scalar_one()
    hz = row.horizons
    assert hz["T+5s"]["change_pct"] == "0.00" and hz["T+5s"]["source"] == "pumpswap pool trades (recorded)"
    assert hz["T+10s"]["change_pct"] == "10.00"  # the 8 s pool trade
    assert hz["T+5m"]["change_pct"] == "20.00"  # the 290 s sell, covered by the second read
    # The pool was not read after 320 s: later horizons are unavailable, never a stale or curve price.
    assert "not read around this time" in hz["T+30m"]["unavailable"] and "T+30m" in row.path
    assert all("0.00041088" not in (h.get("price_raw") or "") for h in hz.values())
    assert row.peak_pct == Decimal("50.00")  # 750 / 500, from the pool
    # Executable returns are not simulated on a pool (a curve model does not apply after migration).
    assert "migrated" in row.path["T+5m"]["executable"]["unknown"]
    assert row.executable_return_pct is None


async def test_a_gap_between_reads_makes_the_horizon_unavailable(db, redis):  # noqa: F811
    await migration_row(db, redis)
    # One read at T+15 s covering trades from T+3 s; the next read at T+600 s only reaches back to T+500 s.
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(3, 500_000_000_000), ptrade(8, 550_000_000_000)],
                                      now=(T0 + timedelta(seconds=15)).timestamp())
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(500, 900_000_000_000)], now=(T0 + timedelta(seconds=600)).timestamp())
    await opp.track(db, redis, T0 + timedelta(seconds=400))
    hz = (await db.execute(select(OpportunityOutcome))).scalar_one().horizons
    assert hz["T+10s"]["change_pct"] == "10.00"
    # T+60s and T+5m: the last known trade is at 8 s but no single read covers 8 s .. 300 s (trades may be missing)
    assert "unavailable" in hz["T+60s"] and "unavailable" in hz["T+5m"]


async def test_tokens_that_never_migrated_are_priced_exactly_as_before(db, redis):  # noqa: F811
    mint = "CurveOnlyMint111111111111111111111111111111"
    await put_trades(redis, mint, [(-5, GRAD // 4), (8, int(GRAD // 4 * 1.1))])
    await opp.record(db, key="obs:c", mint=mint, symbol="C", engine="solana_fresh", stage="OBSERVATION", decision="REJECT",
                     traded=False, reasons=["x"], decided_at=T0, snapshot={"price_raw": str(Decimal(GRAD // 4) / Decimal(VT))})
    await db.commit()
    await opp.track(db, redis, T0 + timedelta(seconds=70))
    hz = (await db.execute(select(OpportunityOutcome))).scalar_one().horizons
    assert hz["T+10s"]["change_pct"] == "10.00" and hz["T+10s"]["source"] == "pump_stream trades"


async def test_a_pool_read_records_its_trades_and_coverage(redis):  # noqa: F811
    class Rpc:
        async def call(self, method, params):
            assert method == "getSignaturesForAddress"
            return [{"signature": "s1", "err": None}]

    import json
    from datetime import datetime, timezone

    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    await redis.set("yx:pumpswap:tx:s1", json.dumps([[at, "u", True, 1, 1, 1000, 2000, 125, POOL]]))
    out = await pumpswap.recent_pool_trades(Rpc(), redis, POOL)
    assert len(out) == 1
    trades, seen = await pumpswap.recorded_pool_history(redis, POOL)
    assert [t.virtual_sol for t in trades] == [2000]
    assert len(seen) == 1 and seen[0][1] == 0.0  # fewer signatures than the limit: the whole history was read
