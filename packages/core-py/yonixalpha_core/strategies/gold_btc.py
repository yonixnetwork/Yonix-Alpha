"""Gold vs BTC — ratio analytics. No reference implementation exists in the
user's repositories (goldvsbtc-binance-future was excluded), so this is
built separately and says so.

Data: Binance USDⓈ-M perpetuals, BTCUSDT and a gold contract — XAUUSDT
(tracks spot gold per troy ounce) or PAXGUSDT (a token backed 1:1 by one
troy ounce). The ratio BTC / gold is "how many ounces of gold one bitcoin
buys". Everything here is descriptive; there is no trading signal, because
a ratio trade needs a paired long/short that has not been backtested.
"""

import math
from dataclasses import dataclass

from yonixalpha_core.strategies.indicators import correlation, log_returns, stdev
from yonixalpha_core.venues.common import Candle

NAME = "gold_vs_btc"
VERSION = "1"
DEFAULTS = {"btc": "BTCUSDT", "gold": "XAUUSDT", "interval": "1h", "candles": 500, "z_window": 168, "corr_window": 168}


@dataclass
class RatioPoint:
    time: str
    btc: float
    gold: float
    ratio: float


@dataclass
class RatioAnalysis:
    points: list[RatioPoint]
    ratio: float
    mean: float | None
    std: float | None
    zscore: float | None
    corr: float | None
    btc_change_pct: float | None
    gold_change_pct: float | None
    ratio_change_pct: float | None
    regime: str


def analyse(btc: list[Candle], gold: list[Candle], params: dict | None = None) -> RatioAnalysis:
    p = {**DEFAULTS, **(params or {})}
    g = {c.open_time: c for c in gold if c.closed}
    pts = [RatioPoint(c.open_time.isoformat(), float(c.close), float(g[c.open_time].close),
                      float(c.close) / float(g[c.open_time].close))
           for c in btc if c.closed and c.open_time in g and float(g[c.open_time].close) > 0]
    if len(pts) < 3:
        raise ValueError("not enough aligned closed candles")
    ratios = [x.ratio for x in pts]
    win = ratios[-int(p["z_window"]):]
    mean = sum(win) / len(win)
    sd = stdev(win)
    z = (ratios[-1] - mean) / sd if sd else None
    cw = int(p["corr_window"]) + 1
    corr = correlation(log_returns([x.btc for x in pts][-cw:]), log_returns([x.gold for x in pts][-cw:]))

    def chg(a: float, b: float) -> float | None:
        return (b / a - 1) * 100 if a else None

    if z is None or math.isnan(z):
        regime = "insufficient history"
    elif z >= 2:
        regime = "BTC unusually rich vs gold (z ≥ 2)"
    elif z <= -2:
        regime = "BTC unusually cheap vs gold (z ≤ −2)"
    else:
        regime = "within normal range (|z| < 2)"
    return RatioAnalysis(pts, ratios[-1], mean, sd, z, corr, chg(pts[0].btc, pts[-1].btc), chg(pts[0].gold, pts[-1].gold),
                         chg(pts[0].ratio, pts[-1].ratio), regime)
