"""Meta Muse Crossover — port of yonixnetwork/meta-muse-crossover-strategy
(bot.py get_trend / check_and_trade, config.py).

Rules, unchanged from the repository:
- trend per asset: EMA(9) vs EMA(21) on 5m closes; strong when
  |fast - slow| / slow >= 0.0015
- BTC (asset 1) up-strong and ETH (asset 2) down-strong  -> SHORT ETH
- BTC down-strong and ETH up-strong                       -> LONG ETH
- anything else: no signal
- while in a position: exit if either trend weakens or the signal no longer
  matches the position's side
- stop 1%, target 2% from entry (supplied to the gate as STRATEGY levels)

Deliberate differences, all toward safety or reproducibility:
- evaluates CLOSED candles only (the original read the still-forming
  candle, so a decision could not be reproduced afterwards);
- leverage is the gate's `max_leverage` (default 1), not the original 10x;
- sizing is the gate's risk-based size, not a fixed $50 margin.
The repository contains no backtest of this edge; it runs in PAPER mode.
"""

from dataclasses import dataclass, field

from yonixalpha_core.safety.models import StrategySignal
from yonixalpha_core.strategies.indicators import ema
from yonixalpha_core.venues.common import Candle

NAME = "meta_muse"
VERSION = "1"
DEFAULTS = {
    "asset1": "BTCUSDT",
    "asset2": "ETHUSDT",
    "interval": "5m",
    "fast": 9,
    "slow": 21,
    "trend_threshold": "0.0015",
    "stop_pct": "0.01",
    "target_pct": "0.02",
    "candles": 150,
}


@dataclass(frozen=True)
class Trend:
    direction: str  # "up" | "down"
    strong: bool
    separation: float
    fast: float
    slow: float


@dataclass
class MetaMuseSignal:
    side: str | None
    trend1: Trend
    trend2: Trend
    reasons: list[str] = field(default_factory=list)


def closed(candles: list[Candle]) -> list[Candle]:
    return [c for c in candles if c.closed]


def trend(closes: list[float], fast: int, slow: int, threshold: float) -> Trend:
    f = ema(closes, fast)[-1]
    s = ema(closes, slow)[-1]
    sep = abs(f - s) / s
    return Trend("up" if f > s else "down", sep >= threshold, sep, f, s)


def evaluate(asset1: list[Candle], asset2: list[Candle], params: dict | None = None) -> MetaMuseSignal:
    p = {**DEFAULTS, **(params or {})}
    c1, c2 = closed(asset1), closed(asset2)
    if len(c1) < int(p["slow"]) * 2 or len(c2) < int(p["slow"]) * 2:
        raise ValueError("not enough closed candles for stable EMAs")
    thr = float(p["trend_threshold"])
    # Per-asset thresholds (Gold vs BTC: gold is far less volatile than BTC);
    # Meta Muse uses one threshold for both.
    thr1 = float(p.get("trend_threshold1") or thr)
    thr2 = float(p.get("trend_threshold2") or thr)
    t1 = trend([float(c.close) for c in c1], int(p["fast"]), int(p["slow"]), thr1)
    t2 = trend([float(c.close) for c in c2], int(p["fast"]), int(p["slow"]), thr2)
    side = None
    if t1.strong and t2.strong:
        if t1.direction == "up" and t2.direction == "down":
            side = "SHORT"
        elif t1.direction == "down" and t2.direction == "up":
            side = "LONG"
    reasons = [
        f"{p['asset1']} {t1.direction} {'strong' if t1.strong else 'weak'} (sep {t1.separation:.4%})",
        f"{p['asset2']} {t2.direction} {'strong' if t2.strong else 'weak'} (sep {t2.separation:.4%})",
    ]
    if side is None:
        reasons.append("no divergence: both trends must be strong and opposite")
    return MetaMuseSignal(side, t1, t2, reasons)


def should_exit(position_side: str, sig: MetaMuseSignal) -> tuple[bool, str]:
    """The repository's early-exit rule."""
    if not sig.trend1.strong or not sig.trend2.strong:
        return True, "trend weakened"
    if sig.side != position_side:
        return True, "signal no longer matches position"
    return False, "divergence intact"


def as_strategy_signal(sig: MetaMuseSignal, name: str = NAME, version: str = VERSION,
                       thresholds: tuple[float, float] | None = None) -> StrategySignal:
    t1, t2 = thresholds or (float(DEFAULTS["trend_threshold"]),) * 2
    strength = min(sig.trend1.separation / t1, sig.trend2.separation / t2)
    return StrategySignal(name, version, sig.side is not None, min(strength, 3.0) / 3.0,
                          sig.reasons + ["no backtested edge in source repository"])
