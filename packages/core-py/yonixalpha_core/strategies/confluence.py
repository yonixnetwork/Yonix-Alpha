"""Confluence Matrix — port of the strategy math in
yonixnetwork/confluence-matrix-forex (analysis.py, signals.py, config.py),
itself a translation of the "Advanced Confluence Matrix" Pine Script.

Unchanged rules:
- pivots: bar i is a pivot high if strictly above the `lookback` bars on
  each side (mirror for lows); a pivot only counts `lookback` bars later,
  when it is confirmed (no look-ahead);
- entry trigger (MSB): close crosses above the last confirmed pivot high
  (BUY) or below the last confirmed pivot low (SELL);
- confluence score 0-100: +30 structure, +25 liquidity grab, +20 RSI side,
  +25 close within 1 ATR of the broken pivot; V2 mode requires >= threshold;
- plan: wave = lastH - lastL; SL beyond the opposite pivot by 5% of wave;
  TP1 = pivot + 0.618 wave (close 50%, stop to breakeven), TP2 = 1.414 wave.

What is NOT ported: the MetaTrader5 connection and order execution
(Windows-only). The original trades GOLD, EURUSD and GBPUSD through MT5;
here the same math runs on Binance USDⓈ-M gold (XAUUSDT by default) in PAPER
mode. Forex pairs remain unavailable (BLOCKED: no MT5 on Linux).
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from yonixalpha_core.safety.models import StrategyLevels, StrategySignal
from yonixalpha_core.strategies.indicators import atr, rolling_std, rsi
from yonixalpha_core.venues.common import Candle

NAME = "confluence_matrix"
VERSION = "1"
DEFAULTS = {
    "symbol": "XAUUSDT",
    "interval": "15m",
    "mode": "V1",
    "threshold": 75,
    "pivot_lookback": 12,
    "tp1_extension": "0.618",
    "tp2_extension": "1.414",
    "sl_buffer_pct": "0.05",
    "rsi_period": 14,
    "atr_period": 14,
    "liquidity_stdev_len": 20,
    "use_killzones": False,
    "killzones": [[7, 10], [12, 15]],
    "cooldown_bars": 5,
    "candles": 400,
}
SCORES = {"structure": 30, "liquidity": 25, "momentum": 20, "proximity": 25}


def pivot_levels(values: list[float], lookback: int, high: bool) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(lookback, len(values) - lookback):
        c = values[i]
        window = values[i - lookback:i] + values[i + 1:i + 1 + lookback]
        if (high and c > max(window)) or (not high and c < min(window)):
            out[i] = c
    return out


def last_confirmed(pivots: list[float | None], lookback: int) -> list[float | None]:
    """Shift each pivot forward by `lookback` bars (confirmation), then
    forward-fill."""
    shifted: list[float | None] = [None] * lookback + pivots[:-lookback] if lookback else list(pivots)
    out, last = [], None
    for v in shifted[: len(pivots)]:
        if v is not None:
            last = v
        out.append(last)
    return out


@dataclass
class ConfluenceSignal:
    side: str | None  # LONG | SHORT
    entry: float
    stop: float | None
    tp1: float | None
    tp2: float | None
    pivot_high: float | None
    pivot_low: float | None
    score: int
    reason: str
    bar_time: datetime | None = None


def evaluate(candles: list[Candle], params: dict | None = None) -> ConfluenceSignal:
    """Evaluates the last CLOSED candle."""
    p = {**DEFAULTS, **(params or {})}
    bars = [c for c in candles if c.closed]
    lb = int(p["pivot_lookback"])
    if len(bars) < lb * 3 + 2:
        raise ValueError("not enough closed candles for confirmed pivots")
    hi = [float(c.high) for c in bars]
    lo = [float(c.low) for c in bars]
    cl = [float(c.close) for c in bars]
    last_h = last_confirmed(pivot_levels(hi, lb, True), lb)
    last_l = last_confirmed(pivot_levels(lo, lb, False), lb)
    i = len(bars) - 1
    entry = cl[i]
    h, lw, ph, pl = last_h[i], last_l[i], last_h[i - 1], last_l[i - 1]
    if h is None or lw is None:
        return ConfluenceSignal(None, entry, None, None, None, h, lw, 0, "no confirmed pivots yet", bars[i].open_time)

    if p["use_killzones"]:
        hour = bars[i].open_time.hour
        if not any(a <= hour < b for a, b in p["killzones"]):
            return ConfluenceSignal(None, entry, None, None, None, h, lw, 0, "outside killzones", bars[i].open_time)

    msb_up = ph is not None and cl[i - 1] <= ph and cl[i] > h
    msb_down = pl is not None and cl[i - 1] >= pl and cl[i] < lw
    if not (msb_up or msb_down):
        return ConfluenceSignal(None, entry, None, None, None, h, lw, 0, "no market-structure break on the last closed bar",
                                bars[i].open_time)
    side = "LONG" if msb_up else "SHORT"

    rsi_v = rsi(cl, int(p["rsi_period"]))[i]
    atr_v = atr(hi, lo, cl, int(p["atr_period"]))[i]
    sd = rolling_std(cl, int(p["liquidity_stdev_len"]))[i]
    buf = sd * 0.1 if sd is not None else math.nan
    score = 0
    if side == "LONG":
        score += SCORES["structure"] if cl[i] > h else 0
        score += SCORES["liquidity"] if (sd is not None and lo[i] < lw and cl[i] > lw + buf) else 0
        score += SCORES["momentum"] if rsi_v > 50 else 0
        score += SCORES["proximity"] if (cl[i] - h) <= atr_v else 0
    else:
        score += SCORES["structure"] if cl[i] < lw else 0
        score += SCORES["liquidity"] if (sd is not None and hi[i] > h and cl[i] < h - buf) else 0
        score += SCORES["momentum"] if rsi_v < 50 else 0
        score += SCORES["proximity"] if (lw - cl[i]) <= atr_v else 0
    score = max(0, min(100, score))

    if p["mode"] == "V2" and score < int(p["threshold"]):
        return ConfluenceSignal(None, entry, None, None, None, h, lw, score, f"score {score} < threshold {p['threshold']}",
                                bars[i].open_time)

    wave = h - lw
    if wave <= 0:
        return ConfluenceSignal(None, entry, None, None, None, h, lw, score, "invalid wave (lastH <= lastL)", bars[i].open_time)
    buf_sl = wave * float(p["sl_buffer_pct"])
    e1, e2 = float(p["tp1_extension"]), float(p["tp2_extension"])
    if side == "LONG":
        sl, tp1, tp2 = lw - buf_sl, h + wave * e1, h + wave * e2
        if sl >= entry:
            return ConfluenceSignal(None, entry, None, None, None, h, lw, score, "SL on wrong side", bars[i].open_time)
    else:
        sl, tp1, tp2 = h + buf_sl, lw - wave * e1, lw - wave * e2
        if sl <= entry:
            return ConfluenceSignal(None, entry, None, None, None, h, lw, score, "SL on wrong side", bars[i].open_time)
    return ConfluenceSignal(side, entry, sl, tp1, tp2, h, lw, score, "fired", bars[i].open_time)


def levels(sig: ConfluenceSignal) -> StrategyLevels:
    return StrategyLevels(
        stop_loss=Decimal(str(sig.stop)),
        take_profits=[Decimal(str(sig.tp1)), Decimal(str(sig.tp2))],
        move_stop_to_breakeven_at_tp1=True,
        source="confluence pivots",
    )


def as_strategy_signal(sig: ConfluenceSignal) -> StrategySignal:
    reasons = [sig.reason, f"confluence score {sig.score}/100",
               f"pivots H {sig.pivot_high} / L {sig.pivot_low}",
               "backtested in source repo on forex/GOLD via MT5; not re-validated on Binance gold — paper only"]
    return StrategySignal(NAME, VERSION, sig.side is not None, sig.score / 100, reasons)
