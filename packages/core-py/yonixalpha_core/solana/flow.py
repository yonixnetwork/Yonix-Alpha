"""Trade-flow features from decoded pump.fun TradeEvents: buyer/seller
counts, volume concentration, creator selling, price and volatility.

Every feature is computed only from trades whose timestamps fall inside the
window ending at `now`, so nothing observed after a decision point can leak
into the features recorded for it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from yonixalpha_core.safety.models import Observation, TradeFlow

LAMPORTS = Decimal(1_000_000_000)
VOLATILITY_BUCKET_SECONDS = 60
MIN_RETURNS_FOR_VOLATILITY = 3


@dataclass(frozen=True)
class Trade:
    at: datetime
    trader: str
    is_buy: bool
    sol_lamports: int
    token_raw: int
    virtual_sol: int
    virtual_token: int

    def price(self, decimals: int) -> Decimal:
        """Post-trade marginal price, SOL per whole token."""
        return (Decimal(self.virtual_sol) / LAMPORTS) / (Decimal(self.virtual_token) / Decimal(10) ** decimals)


def in_window(trades: list[Trade], now: datetime, window_seconds: int) -> list[Trade]:
    start = now - timedelta(seconds=window_seconds)
    return sorted((t for t in trades if start < t.at <= now), key=lambda t: t.at)


def trade_flow(
    trades: list[Trade], now: datetime, window_seconds: int, creator: str | None, source: str, stream_seen_at: datetime | None
) -> TradeFlow:
    """`stream_seen_at` is when the ingestion stream last delivered *any*
    event. Freshness belongs to the stream, not to this token's last trade:
    a live stream with no trades for this token is fresh data meaning "no
    activity"; a dead stream is stale data, whatever the token did."""
    window = in_window(trades, now, window_seconds)
    buys = [t for t in window if t.is_buy]
    sells = [t for t in window if not t.is_buy]
    volume_by_wallet: dict[str, int] = {}
    for t in window:
        volume_by_wallet[t.trader] = volume_by_wallet.get(t.trader, 0) + t.sol_lamports
    total = sum(volume_by_wallet.values())
    top3 = sum(sorted(volume_by_wallet.values(), reverse=True)[:3])
    return TradeFlow(
        observation=Observation(source, stream_seen_at),
        window_seconds=window_seconds,
        trade_count=len(window),
        buy_count=len(buys),
        sell_count=len(sells),
        unique_buyers=len({t.trader for t in buys}),
        unique_sellers=len({t.trader for t in sells}),
        buy_volume_quote=Decimal(sum(t.sol_lamports for t in buys)) / LAMPORTS,
        sell_volume_quote=Decimal(sum(t.sol_lamports for t in sells)) / LAMPORTS,
        top3_wallet_volume_share=(Decimal(top3) / Decimal(total)) if total > 0 else None,
        creator_sold=(any(t.trader == creator for t in sells) if creator else None),
        wallet_level=True,
    )


def realized_volatility(trades: list[Trade], now: datetime, window_seconds: int, decimals: int) -> Decimal | None:
    """Standard deviation of 1-minute log returns over the window, from the
    last traded price in each minute. None when there are too few minutes
    with trades to estimate it — volatility is never invented."""
    window = in_window(trades, now, window_seconds)
    if not window:
        return None
    start = now - timedelta(seconds=window_seconds)
    last_price_per_bucket: dict[int, Decimal] = {}
    for t in window:
        bucket = int((t.at - start).total_seconds() // VOLATILITY_BUCKET_SECONDS)
        last_price_per_bucket[bucket] = t.price(decimals)
    prices = [last_price_per_bucket[b] for b in sorted(last_price_per_bucket)]
    returns = [math.log(float(b / a)) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(returns) < MIN_RETURNS_FOR_VOLATILITY:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return Decimal(str(round(math.sqrt(var), 8)))


def acceleration(trades: list[Trade], now: datetime, window_seconds: int) -> tuple[int, int, float | None]:
    """(trades in current window, trades in the window before, ratio)."""
    current = len(in_window(trades, now, window_seconds))
    prior = len(in_window(trades, now - timedelta(seconds=window_seconds), window_seconds))
    return current, prior, (current / prior) if prior else None
