"""Entry quality from the pre-entry trade stream: a volatility estimate that
says how much it can be trusted, and a multi-window deterioration check.

Volatility confidence
    AVAILABLE       1-minute returns (>= 3) over 15 min, or 10-second returns
                    (>= 3) over 5 min scaled to 1 minute: the measurements the
                    gate has always used, unchanged.
    LOW_CONFIDENCE  neither exists, but the token has at least
                    MICRO_MIN_TRADES trades in the last 5 minutes: volatility
                    from trade-to-trade log returns, scaled to 1 minute by
                    the observed trade rate (a random walk in trade time).
                    Measured, but from few samples: the gate asks for
                    approval instead of trading it automatically.
    UNAVAILABLE     fewer trades than that: no number is produced (never
                    invented) and the reason says how many trades there were.

Deterioration (never from one window alone)
    Compares the current flow window with the previous one AND with the
    longer context, so a short lull in volume is not a collapse. Indicators:
      seller_acceleration   sellers at least doubled vs the previous window
                            (>= 3 sellers) and sell volume exceeds buy volume
      buyer_stall           new buyers at most half the previous window's
                            (which had >= 3)
      volume_collapse       window volume <= 30% of the previous window AND
                            <= 30% of the median window of the 4 minutes
                            before that (the baseline)
      sharp_reversal        price >= 25% below the context's local high and
                            still falling in this window
      unsupported_spike     price up >= 30% in the window driven by <= 2
                            buyers or one wallet with >= 70% of buy volume
    STRONG when two or more fire together: the gate waits (auto) or asks
    for confirmation (manual). One indicator alone is only reported.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from yonixalpha_core.solana.flow import (
    FAST_VOLATILITY_WINDOW_SECONDS,
    Trade,
    in_window,
    measured_volatility,
)

AVAILABLE, LOW_CONFIDENCE, UNAVAILABLE = "AVAILABLE", "LOW_CONFIDENCE", "UNAVAILABLE"
MICRO_MIN_TRADES = 6  # >= 5 trade-to-trade returns
MICRO_WINDOW_SECONDS = FAST_VOLATILITY_WINDOW_SECONDS

CONTEXT_WINDOWS = 4  # longer context = this many flow windows before the previous one
SELLER_ACCEL_FACTOR = 2
MIN_SELLERS_FOR_ACCEL = 3
BUYER_STALL_RATIO = Decimal("0.5")
MIN_PREV_BUYERS = 3
VOLUME_COLLAPSE_RATIO = Decimal("0.3")
REVERSAL_FROM_HIGH = Decimal("0.25")
SPIKE_RETURN = Decimal("0.30")
SPIKE_MAX_BUYERS = 2
SPIKE_TOP_WALLET_SHARE = Decimal("0.70")
STRONG_MIN_INDICATORS = 2


def volatility_estimate(trades: list[Trade], now: datetime, window_seconds: int, decimals: int) -> dict[str, Any]:
    """{"value": Decimal|None, "confidence", "source", "samples"}"""
    vol, how = measured_volatility(trades, now, window_seconds, decimals)
    if vol is not None:
        return {"value": vol, "confidence": AVAILABLE, "source": how, "samples": None}
    recent = in_window(trades, now, MICRO_WINDOW_SECONDS)
    prices = [t.price(decimals) for t in recent]
    rets = [math.log(float(b / a)) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(recent) < MICRO_MIN_TRADES or len(rets) < MICRO_MIN_TRADES - 1:
        return {"value": None, "confidence": UNAVAILABLE, "samples": len(recent),
                "source": f"unavailable: {len(recent)} trades in the last {MICRO_WINDOW_SECONDS // 60} min "
                          f"(need {MICRO_MIN_TRADES}); {how}"}
    mean = sum(rets) / len(rets)
    per_trade = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    span = max((recent[-1].at - recent[0].at).total_seconds(), 1.0)
    trades_per_minute = (len(recent) - 1) * 60.0 / span
    per_minute = per_trade * math.sqrt(max(trades_per_minute, 1.0))
    return {"value": Decimal(str(round(per_minute, 8))), "confidence": LOW_CONFIDENCE, "samples": len(rets),
            "source": f"trade-to-trade returns ({len(rets)}) scaled by {trades_per_minute:.1f} trades/min "
                      "(young token, low confidence)"}


def _stats(window: list[Trade], decimals: int) -> dict[str, Any]:
    buys = [t for t in window if t.is_buy]
    sells = [t for t in window if not t.is_buy]
    by_wallet: dict[str, int] = {}
    for t in buys:
        by_wallet[t.trader] = by_wallet.get(t.trader, 0) + t.sol_lamports
    buy_vol = sum(t.sol_lamports for t in buys)
    return {
        "buyers": len({t.trader for t in buys}), "sellers": len({t.trader for t in sells}),
        "buy_volume": buy_vol, "sell_volume": sum(t.sol_lamports for t in sells),
        "volume": sum(t.sol_lamports for t in window), "trades": len(window),
        "first_price": window[0].price(decimals) if window else None,
        "last_price": window[-1].price(decimals) if window else None,
        "top_buyer_share": (Decimal(max(by_wallet.values())) / Decimal(buy_vol)) if buy_vol else None,
    }


def deterioration(trades: list[Trade], now: datetime, window_seconds: int, decimals: int) -> dict[str, Any]:
    """{"strong": bool, "indicators": [...], "evidence": [...], "metrics": {...}}"""
    cur_w = in_window(trades, now, window_seconds)
    prev_w = in_window(trades, now - timedelta(seconds=window_seconds), window_seconds)
    # Baseline = the median per-window volume of the windows BEFORE the
    # previous one: a spike in the previous window neither sets the baseline
    # nor makes the return to normal look like a collapse.
    ctx_end = now - timedelta(seconds=2 * window_seconds)
    ctx_w = in_window(trades, ctx_end, window_seconds * CONTEXT_WINDOWS)
    cur, prev = _stats(cur_w, decimals), _stats(prev_w, decimals)
    per_window = [sum(t.sol_lamports for t in in_window(trades, ctx_end - timedelta(seconds=window_seconds * k), window_seconds))
                  for k in range(CONTEXT_WINDOWS)]
    per_window.sort()
    mid = len(per_window) // 2
    ctx_avg_volume = Decimal(per_window[mid] if len(per_window) % 2 else (per_window[mid - 1] + per_window[mid]) / 2)
    all_w = cur_w + prev_w + ctx_w
    high = max((t.price(decimals) for t in all_w), default=None)
    price_now = cur["last_price"] or prev["last_price"]
    ind: list[str] = []
    ev: list[str] = []

    if (cur["sellers"] >= MIN_SELLERS_FOR_ACCEL and cur["sellers"] >= SELLER_ACCEL_FACTOR * max(prev["sellers"], 1)
            and cur["sell_volume"] > cur["buy_volume"]):
        ind.append("seller_acceleration")
        ev.append(f"sellers {prev['sellers']} → {cur['sellers']} and sell volume > buy volume")
    if prev["buyers"] >= MIN_PREV_BUYERS and Decimal(cur["buyers"]) <= Decimal(prev["buyers"]) * BUYER_STALL_RATIO:
        ind.append("buyer_stall")
        ev.append(f"buyers {prev['buyers']} → {cur['buyers']}")
    if (prev["volume"] > 0 and Decimal(cur["volume"]) <= Decimal(prev["volume"]) * VOLUME_COLLAPSE_RATIO
            and ctx_avg_volume > 0 and Decimal(cur["volume"]) <= ctx_avg_volume * VOLUME_COLLAPSE_RATIO):
        ind.append("volume_collapse")
        ev.append(f"volume {cur['volume']} vs previous {prev['volume']} and baseline {int(ctx_avg_volume)} lamports")
    if (high and price_now and high > 0 and (1 - price_now / high) >= REVERSAL_FROM_HIGH
            and cur["first_price"] and cur["last_price"] and cur["last_price"] < cur["first_price"]):
        ind.append("sharp_reversal")
        ev.append(f"price {(1 - price_now / high):.0%} below the local high and falling")
    if cur["first_price"] and cur["last_price"] and cur["first_price"] > 0:
        ret = cur["last_price"] / cur["first_price"] - 1
        concentrated = cur["top_buyer_share"] is not None and cur["top_buyer_share"] >= SPIKE_TOP_WALLET_SHARE
        if ret >= SPIKE_RETURN and (cur["buyers"] <= SPIKE_MAX_BUYERS or concentrated):
            ind.append("unsupported_spike")
            ev.append(f"price +{ret:.0%} in the window from {cur['buyers']} buyer(s)"
                      + (f", top wallet {cur['top_buyer_share']:.0%} of buy volume" if concentrated else ""))

    metrics = {
        "window_seconds": window_seconds,
        "buyers": cur["buyers"], "buyers_prev": prev["buyers"], "sellers": cur["sellers"], "sellers_prev": prev["sellers"],
        "volume_lamports": cur["volume"], "volume_prev_lamports": prev["volume"], "baseline_volume_lamports": int(ctx_avg_volume),
        "trades": cur["trades"], "trades_prev": prev["trades"],
        "window_return": str(cur["last_price"] / cur["first_price"] - 1) if cur["first_price"] and cur["last_price"] else None,
        "below_local_high": str(1 - price_now / high) if high and price_now else None,
        "top_buyer_share": str(cur["top_buyer_share"]) if cur["top_buyer_share"] is not None else None,
    }
    return {"strong": len(ind) >= STRONG_MIN_INDICATORS, "indicators": ind, "evidence": ev, "metrics": metrics}
