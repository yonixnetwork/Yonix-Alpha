"""Migration detection that does not depend on the migration event being
in the transaction logs.

The pump stream reads events from `logsSubscribe`. Since pump.fun's
2026-10-08 release (migrate_v2, v3 buys with synthetic migration) almost no
CompletePumpAmmMigrationEvent reached the logs on the production server
(1 in 24 h against about 400 a day before), so migrated tokens stopped
reaching the migration engine. This watch checks, for every curve the
stream saw close to completion (pump_stream.NEAR_COMPLETE), whether its
canonical PumpSwap pool account exists, with one getMultipleAccounts per
BATCH curves (at most MAX_PER_PASS per pass, background RPC priority). A pool that
exists and is owned by the PumpSwap program is recorded exactly as the
migration event would be (curve complete + pool, the MIGRATED index), with
the detection time as the migration time: up to one pass late, never
earlier than it was observable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from yonixalpha_core.solana import pump_stream, pumpswap

MAX_PER_PASS = 200
BATCH = 100  # getMultipleAccounts limit
STATS_FIELD = "migration_pool_detected"


async def detect(redis, rpc, now: datetime) -> dict[str, Any]:
    t = int(now.timestamp())
    near = await redis.zrangebyscore(pump_stream.NEAR_COMPLETE, t - pump_stream.NEAR_COMPLETE_RETENTION, "+inf")
    mints = [m.decode() if isinstance(m, bytes) else m for m in near]
    pending = []
    for m in mints:
        if await redis.zscore(pump_stream.MIGRATED, m) is None:
            pending.append(m)
        else:
            await redis.zrem(pump_stream.NEAR_COMPLETE, m)
    pending = pending[-MAX_PER_PASS:]  # newest first in priority when more are pending
    if not pending:
        return {"near_complete": len(mints), "checked": 0, "detected": 0}
    pools = [pumpswap.canonical_pool(m) for m in pending]
    values: list = []
    for i in range(0, len(pools), BATCH):
        res = await rpc.call("getMultipleAccounts", [pools[i:i + BATCH], {"encoding": "base64", "commitment": "confirmed",
                                                                          "dataSlice": {"offset": 0, "length": 0}}],
                             priority="background")
        chunk = (res or {}).get("value") or []
        values += chunk + [None] * (len(pools[i:i + BATCH]) - len(chunk))
    found = 0
    pipe = redis.pipeline(transaction=False)
    for mint, pool, v in zip(pending, pools, values):
        if not v or v.get("owner") != pumpswap.PUMP_AMM_PROGRAM:
            continue
        pipe.hset(pump_stream.curve_key(mint), mapping={"complete": 1, "pool": pool, "migrated_at": t,
                                                        "migration_source": "pool_account"})
        pipe.expire(pump_stream.curve_key(mint), pump_stream.CURVE_TTL)
        pipe.zadd(pump_stream.MIGRATED, {mint: t}, nx=True)
        pipe.zrem(pump_stream.NEAR_COMPLETE, mint)
        found += 1
    if found:
        pipe.hincrby(pump_stream.STATS, STATS_FIELD, found)
    await pipe.execute()
    return {"near_complete": len(mints), "checked": len(pending), "detected": found}
