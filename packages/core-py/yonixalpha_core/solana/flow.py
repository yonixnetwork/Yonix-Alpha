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


# --- wallet-behaviour indicators (spec §20-23) ---------------------------------
# Every indicator below is computed only from on-chain trades this system
# observed. They are reported as indicators ("RELATED-WALLET INDICATOR",
# "SUSPICIOUS CLUSTER"), never as proof that wallets share an owner.

EARLY_WINDOW_SECONDS = 30
SYNC_TOLERANCE = Decimal("0.05")
SYNC_MIN_CLUSTER = 4


def early_buy_share(trades: list[Trade], created_at: datetime | None, supply_raw: int | None) -> Decimal | None:
    """Share of total supply bought in the first 30 s after the create
    event (sniper concentration). None when creation time or supply is
    unknown."""
    if created_at is None or not supply_raw:
        return None
    end = created_at + timedelta(seconds=EARLY_WINDOW_SECONDS)
    bought = sum(t.token_raw for t in trades if t.is_buy and created_at <= t.at <= end)
    return Decimal(bought) / Decimal(supply_raw)


def synchronized_buy_cluster(trades: list[Trade], now: datetime, window_seconds: int) -> int:
    """Largest group of DISTINCT wallets buying in the same second with SOL
    amounts within 5% of each other — a pattern typical of scripted,
    coordinated buying. Returns the group size (0 or 1 means none)."""
    by_second: dict[int, list[Trade]] = {}
    for t in in_window(trades, now, window_seconds):
        if t.is_buy:
            by_second.setdefault(int(t.at.timestamp()), []).append(t)
    best = 0
    for group in by_second.values():
        amounts = sorted(group, key=lambda t: t.sol_lamports)
        for i, anchor in enumerate(amounts):
            lo = Decimal(anchor.sol_lamports)
            cluster = {t.trader for t in amounts[i:] if lo > 0 and Decimal(t.sol_lamports) <= lo * (1 + SYNC_TOLERANCE)}
            best = max(best, len(cluster))
    return best


def round_trip_volume_share(trades: list[Trade], now: datetime, window_seconds: int) -> Decimal | None:
    """Share of window volume from wallets that both bought and sold inside
    the window (repeated in-and-out behaviour, a wash-trading indicator)."""
    window = in_window(trades, now, window_seconds)
    total = sum(t.sol_lamports for t in window)
    if total == 0:
        return None
    buyers = {t.trader for t in window if t.is_buy}
    sellers = {t.trader for t in window if not t.is_buy}
    both = buyers & sellers
    return Decimal(sum(t.sol_lamports for t in window if t.trader in both)) / Decimal(total)


def demand_quality(trades: list[Trade], now: datetime, window_seconds: int, decimals: int | None) -> dict:
    """Signals that volume is real demand rather than churn:
    - unique buyers in each half of the window (organic interest grows);
    - share of trades from wallets that traded >= 3 times (bots/wash);
    - volume churn: gross volume / |net buy volume| (wash trading moves a lot
      of SOL without moving net demand);
    - price change across the window (volume without price response).
    Raw volume alone is never treated as demand."""
    window = in_window(trades, now, window_seconds)
    out: dict = {"unique_buyers_first_half": None, "unique_buyers_second_half": None, "repeated_wallet_share": None,
                 "volume_churn": None, "price_change": None}
    if not window:
        return out
    mid = now - timedelta(seconds=window_seconds / 2)
    out["unique_buyers_first_half"] = len({t.trader for t in window if t.is_buy and t.at <= mid})
    out["unique_buyers_second_half"] = len({t.trader for t in window if t.is_buy and t.at > mid})
    counts: dict[str, int] = {}
    for t in window:
        counts[t.trader] = counts.get(t.trader, 0) + 1
    out["repeated_wallet_share"] = Decimal(sum(1 for t in window if counts[t.trader] >= 3)) / Decimal(len(window))
    gross = sum(t.sol_lamports for t in window)
    net = abs(sum(t.sol_lamports if t.is_buy else -t.sol_lamports for t in window))
    out["volume_churn"] = (Decimal(gross) / Decimal(max(net, 1))) if gross else None
    if decimals is not None and len(window) >= 2 and window[0].virtual_token and window[-1].virtual_token:
        p0, p1 = window[0].price(decimals), window[-1].price(decimals)
        out["price_change"] = (p1 / p0 - 1) if p0 > 0 else None
    return out


def apply_demand_quality(flow, trades: list[Trade], now: datetime, window_seconds: int, decimals: int | None) -> None:
    for k, v in demand_quality(trades, now, window_seconds, decimals).items():
        setattr(flow, k, v)
