"""Market regimes per chain and the wallet regime test (master upgrade §28).

Every completed hour of a chain's launchpad trades (evm_trades) is summarised
once into market_regime_hours:

  volume       total quote traded (native units)
  net_flow     (buys - sells) / volume: positive = net buying pressure
  median_range median over tokens with >= 3 trades in the hour of
               (highest / lowest trade price - 1): how much prices moved

and each hour is classified against the medians of the window:

  volume       HIGH_VOLUME / LOW_VOLUME
  trend        BULLISH (net buying) / BEARISH (net selling)
  volatility   HIGH_VOLATILITY / LOW_VOLATILITY

These are launchpad-market regimes (memecoin flow on that chain), not the
price of BNB or ETH. A wallet's closed trades are split by the regime of
the hour each was opened in; a wallet is CONSISTENT only when it was
profitable on both sides of every dimension with enough trades on both
sides, REGIME_DEPENDENT when it lost on one side, INSUFFICIENT_DATA when no
dimension has enough trades on both sides. One summary query per hour of
trades, computed incrementally: the full window is never rescanned.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import EvmTrade, MarketRegimeHour
from yonixalpha_core.wallet_pnl import ClosedTrade

E18 = Decimal(10) ** 18
DIMENSIONS = {"volume": ("HIGH_VOLUME", "LOW_VOLUME"), "trend": ("BULLISH", "BEARISH"),
              "volatility": ("HIGH_VOLATILITY", "LOW_VOLATILITY")}
MAX_HOURS_PER_PASS = 168  # a week of hours per call: the 14-day backfill takes two profile rebuilds
MIN_TRADES_PER_SIDE = 3


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


async def summarise_hour(session: AsyncSession, chain: str, start: datetime) -> dict[str, Any]:
    e = EvmTrade
    end = start + timedelta(hours=1)
    price = cast(e.quote_amount, Numeric) / cast(e.token_amount, Numeric)
    per_token = (select(e.token, func.count().label("n"), func.max(price).label("hi"), func.min(price).label("lo"),
                        func.sum(e.quote_amount).label("vol"),
                        func.sum(case((e.is_buy, e.quote_amount), else_=-e.quote_amount)).label("net"))
                 .where(e.chain == chain, e.at >= start, e.at < end, e.token_amount > 0, e.quote_amount > 0)
                 .group_by(e.token).subquery())
    rng = per_token.c.hi / per_token.c.lo - 1
    row = (await session.execute(select(
        func.coalesce(func.sum(per_token.c.vol), 0), func.coalesce(func.sum(per_token.c.net), 0),
        func.percentile_cont(0.5).within_group(rng).filter(per_token.c.n >= 3),
        func.count(), func.coalesce(func.sum(per_token.c.n), 0)))).one()
    vol, net, med, tokens, trades = row
    return {"chain": chain, "hour": start, "volume": Decimal(vol) / E18, "net_flow": (Decimal(net) / Decimal(vol)) if vol else None,
            "median_range": Decimal(str(med)) if med is not None else None, "tokens": int(tokens), "trades": int(trades)}


async def update(session: AsyncSession, chain: str, now: datetime, window: timedelta = timedelta(days=14)) -> int:
    """Summarises completed hours not stored yet (at most MAX_HOURS_PER_PASS
    per call, oldest first). Caller commits."""
    last = (await session.execute(select(func.max(MarketRegimeHour.hour)).where(MarketRegimeHour.chain == chain))).scalar()
    if last is None:  # first run: start at the first hour that has trades, not at empty history
        first = (await session.execute(select(func.min(EvmTrade.at)).where(
            EvmTrade.chain == chain, EvmTrade.at >= now - window))).scalar()
        if first is None:
            return 0
        start = _hour(first)
    else:
        start = last + timedelta(hours=1)
    start = max(start, _hour(now - window))
    done = 0
    while start + timedelta(hours=1) <= _hour(now) and done < MAX_HOURS_PER_PASS:
        values = await summarise_hour(session, chain, start)
        stmt = insert(MarketRegimeHour).values(**values)
        await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "hour"], set_={
            k: stmt.excluded[k] for k in ("volume", "net_flow", "median_range", "tokens", "trades")}))
        start += timedelta(hours=1)
        done += 1
    return done


def classify(hours: list[Any]) -> dict[datetime, dict[str, str]]:
    """hour -> {volume, trend, volatility} against the window's medians.
    Hours without trades are left out (no regime is invented)."""
    rows = [h for h in hours if h.trades]
    if not rows:
        return {}
    vol_med = statistics.median([float(h.volume) for h in rows])
    ranges = [float(h.median_range) for h in rows if h.median_range is not None]
    rng_med = statistics.median(ranges) if ranges else None
    out = {}
    for h in rows:
        r: dict[str, str] = {"volume": "HIGH_VOLUME" if float(h.volume) > vol_med else "LOW_VOLUME"}
        if h.net_flow is not None:
            r["trend"] = "BULLISH" if h.net_flow > 0 else "BEARISH"
        if rng_med is not None and h.median_range is not None:
            r["volatility"] = "HIGH_VOLATILITY" if float(h.median_range) > rng_med else "LOW_VOLATILITY"
        out[h.hour] = r
    return out


async def load_regimes(session: AsyncSession, chain: str, since: datetime) -> dict[datetime, dict[str, str]]:
    rows = (await session.execute(select(MarketRegimeHour).where(
        MarketRegimeHour.chain == chain, MarketRegimeHour.hour >= _hour(since)))).scalars().all()
    return classify(list(rows))


def regime_test(closed: list[ClosedTrade], regimes: dict[datetime, dict[str, str]],
                min_trades: int = MIN_TRADES_PER_SIDE) -> dict[str, Any]:
    """Per dimension and side: trades, wins, net PnL, median return."""
    if not regimes:
        return {"status": "INSUFFICIENT_DATA", "reason": "no market regime hours recorded for this chain yet",
                "dimensions": {}}
    buckets: dict[str, dict[str, list[ClosedTrade]]] = {d: defaultdict(list) for d in DIMENSIONS}
    unclassified = 0
    for c in closed:
        reg = regimes.get(_hour(c.opened_at))
        if not reg:
            unclassified += 1
            continue
        for d in DIMENSIONS:
            if d in reg:
                buckets[d][reg[d]].append(c)
    dims, compared, losing = {}, 0, []
    for d, sides in DIMENSIONS.items():
        stats = {}
        for side in sides:
            cs = buckets[d].get(side, [])
            pnl = sum((c.pnl for c in cs), Decimal(0))
            rois = [c.roi for c in cs if c.roi is not None]
            stats[side] = {"trades": len(cs), "wins": sum(1 for c in cs if c.pnl > 0),
                           "net_pnl": str(pnl.quantize(Decimal("0.000000001"))) if cs else None,
                           "median_return_pct": round(statistics.median(rois) * 100, 2) if rois else None,
                           "status": "OK" if len(cs) >= min_trades else "INSUFFICIENT_DATA"}
        both = all(stats[s]["status"] == "OK" for s in sides)
        if both:
            compared += 1
            losing += [s for s in sides if Decimal(stats[s]["net_pnl"]) <= 0]
        dims[d] = {"sides": stats, "compared": both}
    if compared == 0:
        status = "INSUFFICIENT_DATA"
        reason = f"fewer than {min_trades} closed trades on both sides of every regime"
    elif losing:
        status, reason = "REGIME_DEPENDENT", "not profitable in: " + ", ".join(losing)
    else:
        status, reason = "CONSISTENT", f"profitable on both sides of {compared} regime dimension(s) with enough trades"
    return {"status": status, "reason": reason, "dimensions": dims, "unclassified_trades": unclassified,
            "basis": "launchpad-market regimes of the hour each trade was opened in"}
