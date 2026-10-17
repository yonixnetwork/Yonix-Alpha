"""Historical candles for backtests: fetched from an exchange's PUBLIC
klines (publichttp, no key), stored in crypto.ohlcv with their source, and
loaded back as a Series. Only CLOSED candles are stored. The provenance of
every series (exchange, host, first / last candle, count, gaps) goes into
each backtest run.

Bounded on purpose: at most MAX_BARS per symbol and interval, so a batch
cannot fill the disk or hold a huge series in memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from yonixalpha_crypto.candles import INTERVAL_SECONDS
from yonixalpha_crypto.db import Ohlcv
from yonixalpha_crypto.strategies.library import Series

MAX_BARS = 40_000
REFETCH_AFTER = timedelta(hours=1)
# (exchange, symbol, interval) -> (start, end, when) of the last fetch in this
# process. A range the exchange itself has gaps in stays below 98% after a
# fetch; without this every run of a batch would download it again.
_last_fetch: dict[tuple[str, str, str], tuple[datetime, datetime, datetime]] = {}


async def ensure_history(sf, md, exchange: str, symbol: str, interval: str, start: datetime, end: datetime,
                         source: str, now: datetime | None = None) -> dict[str, Any]:
    """Fetch the range if the store does not already hold (nearly) all of it."""
    now = now or datetime.now(timezone.utc)
    step = INTERVAL_SECONDS[interval]
    end = min(end, now)
    expected = int((end - start).total_seconds() // step)
    if expected > MAX_BARS:
        raise ValueError(f"{expected} bars requested; the limit is {MAX_BARS} per symbol and interval")
    async with sf() as s:
        have = await s.scalar(select(func.count()).select_from(Ohlcv).where(
            Ohlcv.exchange == exchange, Ohlcv.symbol == symbol, Ohlcv.interval == interval,
            Ohlcv.open_time >= start, Ohlcv.open_time < end)) or 0
    fetched = 0
    key = (exchange, symbol, interval)
    prior = _last_fetch.get(key)
    recent = prior is not None and prior[0] <= start and prior[1] >= end and now - prior[2] < REFETCH_AFTER
    if have < expected * 0.98 and recent:
        return {"expected_bars": expected, "already_stored": have, "fetched": 0,
                "note": f"gaps remain; fetched at {prior[2].isoformat()}, not fetched again within {REFETCH_AFTER}"}
    if have < expected * 0.98:
        rows = await md.history(symbol, interval, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        closed = [c for c in rows if c.open_time + timedelta(seconds=step) <= now]
        async with sf() as s:
            for k in range(0, len(closed), 1000):
                chunk = closed[k:k + 1000]
                stmt = insert(Ohlcv).values([dict(exchange=exchange, symbol=symbol, interval=interval,
                                                  open_time=c.open_time, open=c.open, high=c.high, low=c.low,
                                                  close=c.close, volume=c.volume, source=source, fetched_at=now)
                                             for c in chunk])
                await s.execute(stmt.on_conflict_do_nothing())
            await s.commit()
        fetched = len(closed)
        _last_fetch[key] = (start, end, now)
    return {"expected_bars": expected, "already_stored": have, "fetched": fetched}


def apply_data_quality(result: dict[str, Any], prov: dict[str, Any], expected_bars: int) -> dict[str, Any]:
    """Record gaps and coverage on a finished study and in its verdict. Below
    98% coverage a "candidate" is lowered to "weak": missing candles can hide
    losing trades."""
    from yonixalpha_crypto.backtest.validation import TIERS

    bars = int(prov.get("bars") or 0)
    coverage = round(100 * bars / expected_bars, 2) if expected_bars else 100.0
    q = {"bars": bars, "expected_bars": expected_bars, "coverage_pct": coverage, "gaps": int(prov.get("gaps") or 0)}
    result["data_quality"] = q
    v = result.get("verdict")
    if v and (q["gaps"] or coverage < 98):
        v["reasons"] = list(v["reasons"]) + [f"data: {q['gaps']} gap(s), {coverage}% of the expected candles"]
        if coverage < 98 and v["tier"] == "candidate":
            v["tier"], v["approved_for_consideration"] = "weak", False
            v["rank_key"] = [TIERS.index("weak"), *v["rank_key"][1:]]
    return q


async def load(sf, exchange: str, symbol: str, interval: str, start: datetime, end: datetime) -> tuple[Series, dict]:
    async with sf() as s:
        rows = (await s.execute(select(Ohlcv).where(
            Ohlcv.exchange == exchange, Ohlcv.symbol == symbol, Ohlcv.interval == interval,
            Ohlcv.open_time >= start, Ohlcv.open_time < end).order_by(Ohlcv.open_time))).scalars().all()
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    gaps = [(a.open_time.isoformat(), b.open_time.isoformat()) for a, b in zip(rows, rows[1:])
            if b.open_time - a.open_time > step]
    series = Series([float(r.open) for r in rows], [float(r.high) for r in rows], [float(r.low) for r in rows],
                    [float(r.close) for r in rows], [float(r.volume) for r in rows], [r.open_time for r in rows])
    prov = {"exchange": exchange, "symbol": symbol, "interval": interval, "bars": len(rows),
            "first": rows[0].open_time.isoformat() if rows else None,
            "last": rows[-1].open_time.isoformat() if rows else None,
            "sources": sorted({r.source for r in rows}), "gaps": len(gaps), "gap_examples": gaps[:5],
            "note": "public exchange klines; closed candles only"}
    return series, prov


def align(lead: Series, traded: Series) -> tuple[Series, Series]:
    """Keep only the bars both series have (pair strategies)."""
    common = sorted(set(lead.times) & set(traded.times))
    def pick(s: Series) -> Series:
        idx = {t: i for i, t in enumerate(s.times)}
        ii = [idx[t] for t in common]
        return Series([s.o[i] for i in ii], [s.h[i] for i in ii], [s.l[i] for i in ii], [s.c[i] for i in ii],
                      [s.v[i] for i in ii], common)
    a, b = pick(lead), pick(traded)
    b.lead = a
    return a, b
