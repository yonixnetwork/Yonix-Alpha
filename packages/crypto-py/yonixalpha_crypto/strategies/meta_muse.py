"""Meta Muse Crossover: the leading asset's trend against the traded asset's
trend (inverse divergence). Re-implemented from the rules read in
hoziertom44-arch/Meta_Muse_Crossover_strategy@a163ff7 (bot.py get_trend /
check_and_trade, config.py); no code copied (the repository has no licence).

Rules (original):
- trend per asset: EMA(fast) vs EMA(slow) of closes, pandas
  ewm(span, adjust=False); separation = |fast - slow| / slow; strong when
  separation >= trend_threshold; direction "up" when fast > slow, else "down";
- leading UP-strong and traded DOWN-strong  -> SHORT the traded asset;
  leading DOWN-strong and traded UP-strong  -> LONG the traded asset;
  anything else: no signal;
- in a position: close when either trend is not strong or the signal no
  longer matches the position's side;
- stop stop_pct and target target_pct from the entry price.

Differences, stated (see also the registry notes):
- closed candles only (the original read the forming candle);
- spot DEMO: LONG entries only; SHORT signals are recorded, not simulated;
- size from the risk settings, not a fixed $50 margin at 10x;
- the stop / target are checked against the bid every worker loop (the
  original placed exchange STOP_MARKET / TAKE_PROFIT_MARKET orders).
Defaults: 5m candles, 150 candles of history (original MAX_KLINES), 9 / 21,
0.15%, 1% stop, 2% target."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from yonixalpha_crypto import registry
from yonixalpha_crypto.candles import Candle
from yonixalpha_crypto.strategies import IMPLEMENTATIONS, Decision


def ema(values: list[float], span: int) -> list[float]:
    """pandas Series.ewm(span=span, adjust=False).mean()."""
    if not values:
        return []
    alpha = 2.0 / (span + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


@dataclass(frozen=True)
class Trend:
    direction: str  # "up" | "down"
    strong: bool
    separation: float
    fast: float
    slow: float


def trend(closes: list[float], fast: int, slow: int, threshold: float) -> Trend:
    f, s = ema(closes, fast)[-1], ema(closes, slow)[-1]
    sep = abs(f - s) / s if s else 0.0
    return Trend("up" if f > s else "down", sep >= threshold, sep, f, s)


def signal(t1: Trend, t2: Trend) -> str | None:
    if t1.strong and t2.strong:
        if t1.direction == "up" and t2.direction == "down":
            return "SHORT"
        if t1.direction == "down" and t2.direction == "up":
            return "LONG"
    return None


class MetaMuse:
    spec = registry.get("meta_muse")

    def required_candles(self, params: dict[str, Any]) -> int:
        return int(params.get("candles", 150))

    def traded_symbol(self, symbols: list[str]) -> str:
        return symbols[1]

    def evaluate(self, candles: dict[str, list[Candle]], params: dict[str, Any], position_open: bool) -> Decision:
        p = {k: params.get(k, v) for k, v in self.spec.default_params().items()}
        lead, traded = list(candles)[0], list(candles)[1]
        n = int(p["candles"])
        c1 = [float(c.close) for c in candles[lead][-n:]]
        c2 = [float(c.close) for c in candles[traded][-n:]]
        t1 = trend(c1, int(p["fast"]), int(p["slow"]), float(p["trend_threshold"]))
        t2 = trend(c2, int(p["fast"]), int(p["slow"]), float(p["trend_threshold"]))
        sig = signal(t1, t2)
        detail = {"lead": {"symbol": lead, **t1.__dict__}, "traded": {"symbol": traded, **t2.__dict__},
                  "signal": sig, "candles_used": len(c2)}
        if position_open:
            # Spot positions are LONG only.
            if not t1.strong or not t2.strong or sig != "LONG":
                why = "trend weakened" if not (t1.strong and t2.strong) else "signal no longer LONG"
                return Decision("LONG", "EXIT", reasons=[why], detail=detail)
            return Decision("LONG", "HOLD", reasons=["divergence intact"], detail=detail)
        if sig is None:
            return Decision("NONE", "NONE", reasons=["no divergence of two strong trends"], detail=detail)
        stop_pct, target_pct = Decimal(str(p["stop_pct"])), Decimal(str(p["target_pct"]))
        last = Decimal(str(candles[traded][-1].close))
        return Decision(sig, "ENTER", stop_price=last * (1 - stop_pct), target_price=last * (1 + target_pct),
                        reasons=[f"{lead} {t1.direction} strong, {traded} {t2.direction} strong"],
                        detail={**detail, "stop_pct": str(stop_pct), "target_pct": str(target_pct)})


IMPLEMENTATIONS["meta_muse"] = MetaMuse()
