"""Gold vs BTC Dual Trend — port of yonixnetwork/goldvsbtc-binance-future
(bot.py analyze_trend / generate_signal, main.py exit rules, config.py).

Rules, unchanged from the repository:
- trend per asset: EMA(9) vs EMA(21) on 15m closes; gap = |fast − slow| /
  slow. STRONG when the gap is at least 0.005% for gold (XAUUSDT) and
  0.03% for BTC (BTCUSDT) — separate thresholds because gold is far less
  volatile;
- gold UP-strong and BTC DOWN-strong  -> SHORT BTC
- gold DOWN-strong and BTC UP-strong  -> LONG BTC
- same direction, or either not strong: no trade
- in a position: close when either trend is no longer strong, when both
  move the same way, or when the signal flips (the repository then
  re-opens the other way at once; here the next closed candle decides,
  through the safety gate like any entry)
- stop 1%, target 2% from entry (STRATEGY levels for the gate).

Structurally this is the same dual-trend engine as Meta Muse (asset 1
leads, asset 2 is traded), so it reuses meta_muse.evaluate with per-asset
thresholds. Deliberate differences, as for Meta Muse: closed candles only,
leverage from the gate (not the repository's 10x), risk-based size (not a
fixed $50 margin). Protective orders on Binance go through the Algo
service (execution/binance.py) — the repository's ccxt stopLossPrice
orders on /fapi/v1/order are rejected by Binance since 2025-12-09 (-4120).
"""

from yonixalpha_core.safety.models import StrategySignal
from yonixalpha_core.strategies import meta_muse

NAME = "gold_btc_trend"
VERSION = "1"
DEFAULTS = {
    "asset1": "XAUUSDT",  # leading trend: gold
    "asset2": "BTCUSDT",  # traded: BTC perpetual
    "interval": "15m",
    "fast": 9,
    "slow": 21,
    "trend_threshold": "0.0003",
    "trend_threshold1": "0.00005",  # gold GOLD_MIN_GAP 0.005%
    "trend_threshold2": "0.0003",  # btc BTC_MIN_GAP 0.03%
    "stop_pct": "0.01",
    "target_pct": "0.02",
    "candles": 150,
}


def evaluate(gold, btc, params: dict | None = None) -> meta_muse.MetaMuseSignal:
    return meta_muse.evaluate(gold, btc, {**DEFAULTS, **(params or {})})


def should_exit(position_side: str, sig: meta_muse.MetaMuseSignal) -> tuple[bool, str]:
    if not sig.trend1.strong or not sig.trend2.strong:
        return True, "trend weakened"
    if sig.trend1.direction == sig.trend2.direction:
        return True, "inverse correlation broke"
    if sig.side != position_side:
        return True, "signal reversal"
    return False, "divergence intact"


def as_strategy_signal(sig: meta_muse.MetaMuseSignal, params: dict | None = None) -> StrategySignal:
    p = {**DEFAULTS, **(params or {})}
    return meta_muse.as_strategy_signal(sig, NAME, VERSION, (float(p["trend_threshold1"]), float(p["trend_threshold2"])))
