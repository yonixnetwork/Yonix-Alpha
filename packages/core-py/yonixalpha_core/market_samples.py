"""Market samples (2026-10-25): every SAMPLE_SECONDS, SOL/USD and what the
Pump.fun stream saw (launches and migrations in the last hour, tokens traded in
the last 5 minutes, the stream heartbeat age). Day-to-day comparisons
(tools.day_compare) had no SOL price history and no market activity outside
the decisions themselves.

Written by the paper-trading loop right after it refreshes the SOL price
(solana.sol_price, cached 60 s). One row per slot whichever process gets there
first (primary key = slot start). A value that is not known is NULL, never
guessed. Observation only: nothing trades on it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MarketSample
from yonixalpha_core.solana import pump_stream, sol_price

SAMPLE_SECONDS = 300
RETENTION_DAYS = 120


def slot(now: datetime) -> datetime:
    t = int(now.timestamp())
    return datetime.fromtimestamp(t - t % SAMPLE_SECONDS, tz=timezone.utc)


async def record(session: AsyncSession, redis: Any, now: datetime | None = None) -> bool:
    """Writes this slot's sample once. Caller commits. True if a row was written."""
    now = now or datetime.now(timezone.utc)
    at = slot(now)
    if await session.get(MarketSample, at) is not None:
        return False
    t = now.timestamp()
    price, source = None, None
    raw = await redis.get(sol_price.CACHE_KEY)
    if raw:
        try:
            c = json.loads(raw)
            if now - datetime.fromisoformat(c["at"]) <= timedelta(seconds=2 * sol_price.CACHE_TTL):
                price, source = Decimal(str(c["price"])), str(c.get("source") or "")[:64]
        except (ValueError, KeyError, TypeError, InvalidOperation):
            pass
    hb = await pump_stream.heartbeat(redis)
    res = await session.execute(insert(MarketSample).values(
        at=at, sol_usd=price, sol_usd_source=source,
        launches_last_hour=await redis.zcount(pump_stream.RECENT, t - 3600, t),
        migrations_last_hour=await redis.zcount(pump_stream.MIGRATED, t - 3600, t),
        active_tokens_5m=await redis.zcount(pump_stream.ACTIVE, t - 300, t),
        stream_heartbeat_age_s=int(t - hb.timestamp()) if hb else None).on_conflict_do_nothing())
    await session.execute(delete(MarketSample).where(MarketSample.at < now - timedelta(days=RETENTION_DAYS)))
    return bool(res.rowcount)


async def window(session: AsyncSession, since: datetime, until: datetime) -> dict[str, Any]:
    """SOL/USD first / last / low / high and stream activity medians over [since, until)."""
    rows = (await session.execute(select(MarketSample).where(MarketSample.at >= since, MarketSample.at < until)
                                  .order_by(MarketSample.at))).scalars().all()
    prices = [float(r.sol_usd) for r in rows if r.sol_usd is not None]

    def med(vals: list) -> float | None:
        vals = sorted(v for v in vals if v is not None)
        return vals[len(vals) // 2] if vals else None

    expected = max(1, int((min(until, datetime.now(timezone.utc)) - since).total_seconds() // SAMPLE_SECONDS))
    return {"samples": len(rows), "expected_samples": expected,
            "sol_usd_first": prices[0] if prices else None, "sol_usd_last": prices[-1] if prices else None,
            "sol_usd_low": min(prices) if prices else None, "sol_usd_high": max(prices) if prices else None,
            "sol_usd_change_pct": round((prices[-1] / prices[0] - 1) * 100, 2) if len(prices) > 1 else None,
            "median_launches_per_hour": med([r.launches_last_hour for r in rows]),
            "median_migrations_per_hour": med([r.migrations_last_hour for r in rows]),
            "median_active_tokens_5m": med([r.active_tokens_5m for r in rows]),
            "stream_stale_samples": sum(1 for r in rows if r.stream_heartbeat_age_s is None or r.stream_heartbeat_age_s > 60)}
