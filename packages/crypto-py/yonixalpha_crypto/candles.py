"""Candle hygiene, applied to every exchange's data before a strategy sees it:
duplicates dropped (the last copy of an open time wins), sorted oldest
first, still-forming candles removed, gaps and staleness reported."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
                    "4h": 14400, "1d": 86400}


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    interval: str

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=INTERVAL_SECONDS[self.interval])


@dataclass
class CandleSet:
    candles: list[Candle]  # closed only, oldest first, unique open times
    dropped_duplicates: int
    dropped_forming: int
    gaps: list[tuple[datetime, datetime]]  # (last before gap, first after)
    stale: bool
    stale_reason: str | None


def clean(raw: list[Candle], interval: str, now: datetime, *, max_lag_intervals: float = 2.0) -> CandleSet:
    """`stale`: the newest closed candle closed more than max_lag_intervals
    intervals before `now`, so the feed is not keeping up (or the market
    is halted) and no decision may use it."""
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    by_time: dict[datetime, Candle] = {}
    for c in raw:
        by_time[c.open_time] = c
    dup = len(raw) - len(by_time)
    ordered = sorted(by_time.values(), key=lambda c: c.open_time)
    closed = [c for c in ordered if c.close_time <= now]
    gaps = [(a.open_time, b.open_time) for a, b in zip(closed, closed[1:]) if b.open_time - a.open_time > step]
    stale, why = False, None
    if not closed:
        stale, why = True, "no closed candle"
    elif now - closed[-1].close_time > step * max_lag_intervals:
        stale, why = True, f"newest closed candle ended at {closed[-1].close_time.isoformat()}"
    return CandleSet(closed, dup, len(ordered) - len(closed), gaps, stale, why)


def ms(dt_ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(dt_ms) / 1000, tz=timezone.utc)
