"""Manufactured-pump detector (causal).

A documented pattern of manufactured pumps (LaunchWatch-style detectors,
research summarized in docs/SCANNER_INTELLIGENCE_2026.md): price rises in
small, unusually regular steps — most candles up, similar returns, a steady
buy/sell mix, log-price on a straight line — which organic demand rarely
produces. The detector identifies that pattern and reports it as elevated
manipulation risk. It does not predict a dump; its value as a defensive
signal must be validated on this system's outcomes.

It needs a developed pattern (at least `min_candles` candles with trades
over at least the shortest window), so it is NOT a first-seconds entry
signal: it runs at every (re-)evaluation and on continued observation and
updates the risk. It reads only trades up to `now`; future candles are
used only later, for labels.

Thresholds are configuration (dashboard settings manufactured_pump_*), not
constants: the published detectors' exact values were not verifiable from
this environment, so the defaults are starting points to be calibrated.
The detector version and the thresholds used are stored with every result.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from yonixalpha_core.solana.flow import Trade

DETECTOR_VERSION = "mp-1"


@dataclass(frozen=True)
class MPConfig:
    candle_seconds: int = 10
    windows: tuple[int, ...] = (60, 90, 120, 180, 300)  # trailing windows tried, seconds
    min_candles: int = 6  # candles with trades inside a window
    min_positive_return_share: float = 0.65
    max_return_cv: float = 1.5  # std / mean of candle returns (mean must be > 0)
    max_buy_ratio_std: float = 0.15  # std of per-candle buy share of SOL volume
    min_log_price_r2: float = 0.85  # log(close) vs time, rising
    min_window_return: float = 0.25


def config(s: Any) -> MPConfig:
    return MPConfig(candle_seconds=int(s.manufactured_pump_candle_seconds),
                    min_candles=int(s.manufactured_pump_min_candles),
                    min_positive_return_share=float(s.manufactured_pump_min_positive_share),
                    max_return_cv=float(s.manufactured_pump_max_return_cv),
                    max_buy_ratio_std=float(s.manufactured_pump_max_buy_ratio_std),
                    min_log_price_r2=float(s.manufactured_pump_min_log_r2),
                    min_window_return=float(s.manufactured_pump_min_window_return))


def _price(t: Trade) -> float:
    return t.virtual_sol / t.virtual_token if t.virtual_token else 0.0


def candles(trades: list[Trade], start: datetime, end: datetime, seconds: int, prev_close: float | None) -> list[dict[str, Any]]:
    """Candles over (start, end]; an empty candle carries the previous close
    (its return is 0)."""
    n = max(1, math.ceil((end - start).total_seconds() / seconds))
    out = []
    close = prev_close
    xs = sorted((t for t in trades if start < t.at <= end), key=lambda t: t.at)
    i = 0
    for k in range(n):
        hi = start + timedelta(seconds=seconds * (k + 1))
        c = {"open": close, "close": close, "buy": 0, "sell": 0, "trades": 0}
        while i < len(xs) and xs[i].at <= hi:
            t = xs[i]
            p = _price(t)
            if c["open"] is None:
                c["open"] = p
            c["close"] = p
            c["buy" if t.is_buy else "sell"] += t.sol_lamports
            c["trades"] += 1
            i += 1
        close = c["close"]
        out.append(c)
    return out


def _log_r2(closes: list[float]) -> float | None:
    pts = [(i, math.log(c)) for i, c in enumerate(closes) if c and c > 0]
    if len(pts) < 3:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    return 0.0 if sxy <= 0 else (sxy * sxy) / (sxx * syy)


def window_metrics(trades: list[Trade], now: datetime, window: int, cfg: MPConfig) -> dict[str, Any] | None:
    start = now - timedelta(seconds=window)
    before = [t for t in trades if t.at <= start]
    prev_close = _price(max(before, key=lambda t: t.at)) if before else None
    cs = candles(trades, start, now, cfg.candle_seconds, prev_close)
    active = [c for c in cs if c["trades"]]
    if len(active) < cfg.min_candles:
        return None
    rets = [c["close"] / c["open"] - 1 for c in cs if c["open"] and c["close"]]
    if len(rets) < 3:
        return None
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    shares = [c["buy"] / (c["buy"] + c["sell"]) for c in active if c["buy"] + c["sell"] > 0]
    first_open = next((c["open"] for c in cs if c["open"]), None)
    last_close = cs[-1]["close"]
    return {
        "window_seconds": window, "candles": len(cs), "active_candles": len(active),
        "positive_return_share": round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "return_coefficient_of_variation": round(sd / mean, 4) if mean > 0 else None,
        "buy_sell_ratio_variation": round(statistics.pstdev(shares), 4) if len(shares) >= 2 else None,
        "log_price_r2": None if (r2 := _log_r2([c["close"] for c in cs])) is None else round(r2, 4),
        "window_return": round(last_close / first_open - 1, 4) if first_open and last_close else None,
    }


def _conditions(m: dict[str, Any], cfg: MPConfig) -> dict[str, bool]:
    def ok(v, f):
        return v is not None and f(v)
    return {
        "positive_return_share": ok(m["positive_return_share"], lambda v: v >= cfg.min_positive_return_share),
        "return_coefficient_of_variation": ok(m["return_coefficient_of_variation"], lambda v: v <= cfg.max_return_cv),
        "buy_sell_ratio_variation": ok(m["buy_sell_ratio_variation"], lambda v: v <= cfg.max_buy_ratio_std),
        "log_price_r2": ok(m["log_price_r2"], lambda v: v >= cfg.min_log_price_r2),
        "window_return": ok(m["window_return"], lambda v: v >= cfg.min_window_return),
    }


def detect(trades: list[Trade], now: datetime, cfg: MPConfig | None = None) -> dict[str, Any]:
    """{risk: UNKNOWN / LOW / ELEVATED / HIGH, score 0..1, pattern_duration
    (longest trailing window meeting every condition), metrics, evidence}."""
    cfg = cfg or MPConfig()
    held = [t for t in trades if t.at <= now]
    out: dict[str, Any] = {"detector_version": DETECTOR_VERSION, "as_of": now.isoformat(), "thresholds": asdict(cfg),
                           "note": "identifies a documented price/flow pattern associated with elevated manipulation risk; "
                                   "a defensive signal to be evaluated, not a prediction of a dump"}
    best: tuple[int, int, dict, dict] | None = None  # (met, window, metrics, conditions)
    full: list[int] = []
    for w in sorted(cfg.windows):
        m = window_metrics(held, now, w, cfg)
        if m is None:
            continue
        cond = _conditions(m, cfg)
        met = sum(cond.values())
        if met == len(cond):
            full.append(w)
        if best is None or met > best[0] or (met == best[0] and w > best[1]):
            best = (met, w, m, cond)
    if best is None:
        return {**out, "risk": "UNKNOWN", "score": None, "pattern_duration_seconds": 0,
                "reason": f"fewer than {cfg.min_candles} active {cfg.candle_seconds}s candles in every window: "
                          "the pattern has not developed (this detector is not an early-entry signal)"}
    met, w, m, cond = best
    risk = "HIGH" if full else "ELEVATED" if met == len(cond) - 1 else "LOW"
    return {**out, "risk": risk, "score": round(met / len(cond), 4),
            "pattern_duration_seconds": max(full) if full else 0, "best_window_seconds": w,
            "metrics": m, "conditions": cond,
            "evidence": [f"{k}: {m[k]}" for k, v in cond.items() if v]}
