"""The YonixAlpha strategy library: one shared interface, 20 modules.

A module only reads closed bars and returns a decision; it never places an
order. The SAME functions serve the backtester (yonixalpha_crypto.backtest)
and the live paper / exchange-demo runner (through LibraryAdapter, which
implements the engine's Strategy protocol), so a backtest and a live
evaluation of the same bars give the same decision.

Rules for every module:
- evaluation at bar i uses indicators computed from bars <= i only
  (strategies.indicators is causal; tests recompute on truncated data);
- spot is LONG only: a module's short rule is documented in its research
  record but never executed here;
- every entry carries a stop (ATR-based unless the module defines a
  structural one) and, when target_r > 0, a target at target_r x the risk;
- optional shared filter: min_volume_ratio (volume >= ratio x 20-bar
  average volume; 0 = off);
- a decision that cannot be computed (indicator not warmed up, ATR zero)
  is NO TRADE with the reason, never a guess.

Nothing here is evidence of profitability. Research records cite the
published work each rule comes from and its known limits; results produced
by YonixAlpha live only in the backtest / paper / demo tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from yonixalpha_crypto import registry
from yonixalpha_crypto.candles import Candle
from yonixalpha_crypto.strategies import IMPLEMENTATIONS, Decision
from yonixalpha_crypto.strategies import indicators as ind

D = Decimal
COMMON_PARAMS: dict[str, tuple[Any, str, Any, Any]] = {
    "atr_period": (14, "int", 5, 50),
    "stop_atr": (D("2"), "dec", "0.5", "10"),
    "target_r": (D("0"), "dec", "0", "10"),
    "min_volume_ratio": (D("0"), "dec", "0", "5"),
}
LIB_INTERVALS = ("15m", "1h", "4h", "1d")


# ---- series and decisions -------------------------------------------------

class Series:
    """Closed bars as floats plus a cache of indicator arrays."""

    def __init__(self, o: list[float], h: list[float], lo: list[float], c: list[float], v: list[float],
                 times: list[datetime] | None = None, lead: "Series | None" = None):
        self.o, self.h, self.l, self.c, self.v = o, h, lo, c, v
        self.times = times or []
        self.lead = lead
        self._cache: dict[tuple, Any] = {}

    @classmethod
    def from_candles(cls, candles: list[Candle], lead: list[Candle] | None = None) -> "Series":
        f = lambda xs: [float(x) for x in xs]  # noqa: E731
        lead_s = cls.from_candles(lead) if lead else None
        return cls(f(c.open for c in candles), f(c.high for c in candles), f(c.low for c in candles),
                   f(c.close for c in candles), f(c.volume for c in candles), [c.open_time for c in candles], lead_s)

    def __len__(self) -> int:
        return len(self.c)

    def get(self, key: tuple, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # shortcuts
    def sma(self, n): return self.get(("sma", n), lambda: ind.sma(self.c, n))
    def ema(self, n): return self.get(("ema", n), lambda: ind.ema(self.c, n))
    def atr(self, n): return self.get(("atr", n), lambda: ind.atr(self.h, self.l, self.c, n))
    def rsi(self, n): return self.get(("rsi", n), lambda: ind.rsi(self.c, n))
    def adx(self, n): return self.get(("adx", n), lambda: ind.adx(self.h, self.l, self.c, n))
    def vol_sma(self, n): return self.get(("vsma", n), lambda: ind.sma(self.v, n))
    def hh(self, n): return self.get(("hh", n), lambda: ind.highest_prev(self.h, n))
    def ll(self, n): return self.get(("ll", n), lambda: ind.lowest_prev(self.l, n))


@dataclass
class Entry:
    stop: float
    target: float | None
    reasons: list[str]
    values: dict[str, Any] = field(default_factory=dict)


def _ok(*vals) -> bool:
    return all(v is not None for v in vals)


def crossed_above(a: list, b: list, i: int) -> bool:
    return i >= 1 and _ok(a[i], b[i], a[i - 1], b[i - 1]) and a[i] > b[i] and a[i - 1] <= b[i - 1]


def crossed_below(a: list, b: list, i: int) -> bool:
    return i >= 1 and _ok(a[i], b[i], a[i - 1], b[i - 1]) and a[i] < b[i] and a[i - 1] >= b[i - 1]


def r(x: float | None, d: int = 4):
    return None if x is None else round(x, d)


# ---- the module base class ------------------------------------------------

@dataclass
class Module:
    id: str
    name: str
    category: str
    description: str
    entry_rules: str
    exit_rules: str
    params: dict[str, tuple[Any, str, Any, Any]]
    default_interval: str
    research: dict[str, Any]
    default_exits: dict[str, Any] = field(default_factory=dict)
    version: str = "1"
    intervals: tuple[str, ...] = LIB_INTERVALS
    default_symbols: tuple[str, ...] = ("BTC/USDT",)
    needs_lead: bool = False

    def p(self, params: dict[str, Any]) -> dict[str, Any]:
        merged = {k: v[0] for k, v in {**self.params, **COMMON_PARAMS}.items()}
        merged.update({k: v for k, v in (params or {}).items() if k in merged})
        return {k: (float(v) if isinstance(v, (Decimal, str)) else v) for k, v in merged.items()}

    extra_lookback: int = 0  # bars an indicator needs beyond the module's own parameters (e.g. SMA 200)

    def lookback(self, p: dict[str, Any]) -> int:
        """Longest window any rule of this module reads, in bars."""
        own = [int(p[k]) for k, v in self.params.items() if v[1] == "int" and int(v[3]) > 1]  # skips 0/1 flags
        return max(own + [self.extra_lookback, int(p["atr_period"]) + 1, 21])  # 21: the 20-bar volume average

    def warmup(self, p: dict[str, Any]) -> int:
        """Closed bars requested for a live evaluation: three times the
        longest window, so seeded EMAs and Wilder averages have converged."""
        return 3 * self.lookback(p)

    # subclasses implement these two
    def entry(self, s: Series, i: int, p: dict[str, Any]) -> tuple[Entry | None, str]:
        raise NotImplementedError

    def exit(self, s: Series, i: int, p: dict[str, Any]) -> str | None:
        raise NotImplementedError

    # shared pieces
    def atr_stop(self, s: Series, i: int, p: dict[str, Any], reasons: list[str], values: dict,
                 stop: float | None = None) -> tuple[Entry | None, str]:
        a = s.atr(int(p["atr_period"]))[i]
        if a is None or a <= 0:
            return None, "ATR not available: no reliable stop"
        if p["min_volume_ratio"] > 0:
            vs = s.vol_sma(20)[i]
            if vs is None or s.v[i] < p["min_volume_ratio"] * vs:
                return None, f"volume filter: {s.v[i]:.4g} < {p['min_volume_ratio']} x 20-bar average"
        close = s.c[i]
        stop = stop if stop is not None else close - p["stop_atr"] * a
        if stop >= close or stop <= 0:
            return None, f"stop {stop:.6g} is not below the close {close:.6g}"
        target = close + p["target_r"] * (close - stop) if p["target_r"] > 0 else None
        values.update({"close": r(close, 8), "atr": r(a, 8), "stop": r(stop, 8), "target": r(target, 8)})
        return Entry(stop, target, reasons, values), "entry"

    def decide(self, s: Series, i: int, params: dict[str, Any]) -> tuple[Entry | None, str]:
        p = self.p(params)
        if i < self.lookback(p) or i >= len(s):
            return None, "not enough closed bars"
        try:
            return self.entry(s, i, p)
        except TypeError:  # an indicator value was None
            return None, "indicators not warmed up"

    def should_exit(self, s: Series, i: int, params: dict[str, Any]) -> str | None:
        try:
            return self.exit(s, i, self.p(params))
        except TypeError:
            return None


# ---- the 20 modules -------------------------------------------------------

SRC = {
    "bll": {"title": "Brock, Lakonishok & LeBaron (1992), Simple technical trading rules and the stochastic properties of "
                     "stock returns, Journal of Finance 47(5) 1731-1764", "url": "https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html"},
    "stw": {"title": "Sullivan, Timmermann & White (1999), Data-snooping, technical trading rule performance, and the "
                     "bootstrap, Journal of Finance 54(5) 1647-1691", "url": "https://ideas.repec.org/p/fmg/fmgdps/dp303.html"},
    "hu": {"title": "Hudson & Urquhart (2021), Technical trading and cryptocurrencies, Annals of Operations Research "
                    "297 191-220 (no out-of-sample predictability for Bitcoin; some for other coins)",
           "url": "https://hull-repository.worktribe.com/output/2328042"},
    "mop": {"title": "Moskowitz, Ooi & Pedersen (2012), Time series momentum, Journal of Financial Economics 104(2) "
                     "228-250", "url": "https://papers.ssrn.com/abstract=2089463"},
    "lt": {"title": "Liu & Tsyvinski (2021), Risks and returns of cryptocurrency, Review of Financial Studies 34(6) "
                    "2689-2727 (time-series momentum in crypto)", "url": "https://papers.ssrn.com/abstract=3226806"},
    "wilder": {"title": "Wilder (1978), New Concepts in Technical Trading Systems (RSI, ATR, ADX/DMI definitions)",
               "url": ""},
    "pbo": {"title": "Bailey, Borwein, Lopez de Prado & Zhu, The probability of backtest overfitting, Journal of "
                     "Computational Finance", "url": "https://papers.ssrn.com/abstract=2326253"},
    "turtle": {"title": "Donchian channel / Turtle rules (20-bar entry, 10-bar exit, 2N stop), as published by "
                        "C. Faith, Way of the Turtle (2007)", "url": ""},
    "connors": {"title": "Connors & Alvarez (2008), Short Term Trading Strategies That Work (RSI(2) < 10 above the "
                         "200-day average, exit above the 5-day average)", "url": ""},
    "bollinger": {"title": "J. Bollinger (2001), Bollinger on Bollinger Bands (20-period, 2 standard deviations)", "url": ""},
    "granville": {"title": "J. Granville (1963), On-balance volume", "url": ""},
    "appel": {"title": "G. Appel, MACD (12, 26, 9)", "url": ""},
    "freqtrade": {"title": "freqtrade/freqtrade-strategies (GPL-3.0): strategy catalogue, ideas only, no code copied",
                  "url": "https://github.com/freqtrade/freqtrade-strategies"},
    "sepp": {"title": "ArturSepp/TrendFollowingSystems (GPL-3.0): trend-following research code, ideas only",
             "url": "https://github.com/ArturSepp/TrendFollowingSystems"},
    "ea31337": {"title": "EA31337/EA31337-strategies (GPL-3.0, MQL): indicator strategy catalogue, ideas only",
                "url": "https://github.com/EA31337/EA31337-strategies"},
    "trendbot": {"title": "arwensorcenelli/trend-bot @ab75bb1 (no licence): 10/50 MA trend with a 3 x ATR stop, rule "
                          "idea only", "url": "https://github.com/arwensorcenelli/trend-bot"},
    "metamuse": {"title": "hoziertom44-arch/Meta_Muse_Crossover_strategy @a163ff7 (no licence): the original bot, rules "
                          "read and re-implemented", "url": "https://github.com/hoziertom44-arch/Meta_Muse_Crossover_strategy"},
    "confluence": {"title": "siriusforex-ai/confluence_matrix_forex @f3beff9 (no licence): pivot breakout, rule idea "
                            "only; its backtest charges no costs", "url": "https://github.com/siriusforex-ai/confluence_matrix_forex"},
}
COMMON_LIMITS = ("published evidence is for other markets, periods or rule variants and is not evidence for this "
                 "implementation; technical-rule returns are prone to data snooping (Sullivan, Timmermann & White 1999) "
                 "and to backtest overfitting (Bailey et al.)")


def research(family: str, designed: str, sources: list[str], evidence: str, works: str, fails: str, short: str,
             licence: str = "independent implementation; no third-party code copied") -> dict[str, Any]:
    return {"family": family, "designed_for": designed, "sources": [SRC[s] for s in sources], "licence": licence,
            "evidence": evidence, "limitations": COMMON_LIMITS, "works_when": works, "fails_when": fails,
            "short_rule": short + " (not executed: spot is long-only)",
            "validation_tasks": ["backtest with costs on train / validation / out-of-sample", "walk-forward",
                                 "parameter sensitivity", "paper trading", "exchange-demo trading"]}


TRAIL_ATR = {"trailing": {"method": "atr", "activate_r": 1, "atr_mult": 3}}
TRAIL_SWING = {"trailing": {"method": "swing", "activate_r": 1, "swing_bars": 10}}
MEANREV_EXITS = {"time_stop_bars": 24}


class SmaCross(Module):
    def entry(self, s, i, p):
        f, sl = s.sma(int(p["fast"])), s.sma(int(p["slow"]))
        if not crossed_above(f, sl, i):
            return None, f"no SMA {int(p['fast'])}/{int(p['slow'])} cross up"
        return self.atr_stop(s, i, p, [f"SMA {int(p['fast'])} crossed above SMA {int(p['slow'])}"],
                             {"sma_fast": r(f[i]), "sma_slow": r(sl[i])})

    def exit(self, s, i, p):
        f, sl = s.sma(int(p["fast"])), s.sma(int(p["slow"]))
        return f"SMA {int(p['fast'])} crossed below SMA {int(p['slow'])}" if crossed_below(f, sl, i) else None


class EmaCross(Module):
    def entry(self, s, i, p):
        f, sl, t = s.ema(int(p["fast"])), s.ema(int(p["slow"])), s.ema(int(p["trend"]))
        if not crossed_above(f, sl, i):
            return None, "no EMA cross up"
        if s.c[i] <= t[i]:
            return None, f"close below EMA {int(p['trend'])}: long-term trend filter"
        return self.atr_stop(s, i, p, [f"EMA {int(p['fast'])} crossed above EMA {int(p['slow'])}, close above EMA "
                                       f"{int(p['trend'])}"], {"ema_fast": r(f[i]), "ema_slow": r(sl[i]), "ema_trend": r(t[i])})

    def exit(self, s, i, p):
        f, sl = s.ema(int(p["fast"])), s.ema(int(p["slow"]))
        return "EMA fast crossed below EMA slow" if crossed_below(f, sl, i) else None


class AdxTrend(Module):
    def entry(self, s, i, p):
        a, pdi, ndi = s.adx(int(p["adx_period"]))
        if not crossed_above(pdi, ndi, i):
            return None, "no +DI cross above -DI"
        if a[i] < p["adx_min"]:
            return None, f"ADX {a[i]:.1f} below {p['adx_min']}: no trend strength"
        return self.atr_stop(s, i, p, [f"+DI crossed above -DI with ADX {a[i]:.1f} >= {p['adx_min']}"],
                             {"adx": r(a[i], 2), "plus_di": r(pdi[i], 2), "minus_di": r(ndi[i], 2)})

    def exit(self, s, i, p):
        a, pdi, ndi = s.adx(int(p["adx_period"]))
        if ndi[i] > pdi[i]:
            return "-DI above +DI"
        if a[i] < p["adx_exit"]:
            return f"ADX {a[i]:.1f} below {p['adx_exit']}: trend faded"
        return None


class MtfTrend(Module):
    def lookback(self, p):
        return max(super().lookback(p), int(p["htf_factor"]) * (int(p["htf_ema"]) + 2))

    def _htf(self, s, p):
        m = int(p["htf_factor"])
        def build():
            g, done = ind.resample_last_closed(s.c, m)
            return ind.ema(g, int(p["htf_ema"])), g, done
        return s.get(("htf", m, int(p["htf_ema"])), build)

    def entry(self, s, i, p):
        e, g, done = self._htf(s, p)
        k = done[i]
        if k < 1 or e[k] is None or e[k - 1] is None:
            return None, "higher timeframe not warmed up"
        if not (e[k] > e[k - 1] and g[k] > e[k]):
            return None, "higher-timeframe trend not up (last completed higher bar)"
        lt = s.ema(int(p["ltf_ema"]))
        if not (s.c[i] > lt[i] and s.c[i - 1] <= lt[i - 1]):
            return None, f"no close back above EMA {int(p['ltf_ema'])}"
        return self.atr_stop(s, i, p, [f"higher timeframe (x{int(p['htf_factor'])}) EMA rising and close above it; "
                                       f"close crossed above EMA {int(p['ltf_ema'])}"],
                             {"htf_ema": r(e[k]), "htf_close": r(g[k]), "ltf_ema": r(lt[i])})

    def exit(self, s, i, p):
        e, g, done = self._htf(s, p)
        k = done[i]
        if k >= 0 and e[k] is not None and g[k] < e[k]:
            return "higher-timeframe close below its EMA"
        lt = s.ema(int(p["ltf_ema"]))
        if s.c[i] < lt[i] and s.c[i - 1] < lt[i - 1]:
            return f"two closes below EMA {int(p['ltf_ema'])}"
        return None


class RsiMomentum(Module):
    def entry(self, s, i, p):
        x, t = s.rsi(int(p["rsi_period"])), s.ema(int(p["trend"]))
        if not (x[i] > p["rsi_entry"] >= x[i - 1]):
            return None, f"RSI did not cross above {p['rsi_entry']}"
        if s.c[i] <= t[i]:
            return None, f"close below EMA {int(p['trend'])}"
        return self.atr_stop(s, i, p, [f"RSI crossed above {p['rsi_entry']} with close above EMA {int(p['trend'])}"],
                             {"rsi": r(x[i], 2), "ema_trend": r(t[i])})

    def exit(self, s, i, p):
        x = s.rsi(int(p["rsi_period"]))
        return f"RSI {x[i]:.1f} below {p['rsi_exit']}" if x[i] < p["rsi_exit"] else None


class MacdMomentum(Module):
    def _m(self, s, p):
        return s.get(("macd", int(p["fast"]), int(p["slow"]), int(p["signal"])),
                     lambda: ind.macd(s.c, int(p["fast"]), int(p["slow"]), int(p["signal"])))

    def entry(self, s, i, p):
        line, sig = self._m(s, p)
        if not crossed_above(line, sig, i):
            return None, "no MACD cross above its signal line"
        if line[i] <= 0:
            return None, "MACD below zero: momentum not positive"
        return self.atr_stop(s, i, p, ["MACD crossed above its signal line above zero"],
                             {"macd": r(line[i], 6), "signal": r(sig[i], 6)})

    def exit(self, s, i, p):
        line, sig = self._m(s, p)
        return "MACD crossed below its signal line" if crossed_below(line, sig, i) else None


class RocMomentum(Module):
    def entry(self, s, i, p):
        n = int(p["roc_period"])
        x = s.get(("roc", n), lambda: ind.roc(s.c, n))
        t = s.sma(int(p["trend"]))
        if x[i] <= p["roc_min"] or x[i] <= x[i - 3]:
            return None, f"ROC({n}) {x[i]:.2f}% not above {p['roc_min']}% and rising"
        if x[i - 1] > p["roc_min"]:
            return None, "momentum already above the threshold on the previous bar (no fresh signal)"
        if s.c[i] <= t[i]:
            return None, f"close below SMA {int(p['trend'])}"
        return self.atr_stop(s, i, p, [f"ROC({n}) rose through {p['roc_min']}% with close above SMA {int(p['trend'])}"],
                             {"roc": r(x[i], 3), "sma_trend": r(t[i])})

    def exit(self, s, i, p):
        n = int(p["roc_period"])
        x = s.get(("roc", n), lambda: ind.roc(s.c, n))
        return f"ROC({n}) turned negative" if x[i] < 0 else None


class Donchian(Module):
    def entry(self, s, i, p):
        hh = s.hh(int(p["entry_len"]))
        if not (s.c[i] > hh[i]):
            return None, f"close not above the {int(p['entry_len'])}-bar high"
        return self.atr_stop(s, i, p, [f"close {s.c[i]:.6g} above the prior {int(p['entry_len'])}-bar high {hh[i]:.6g}"],
                             {"channel_high": r(hh[i], 8)})

    def exit(self, s, i, p):
        ll = s.ll(int(p["exit_len"]))
        return f"close below the prior {int(p['exit_len'])}-bar low" if s.c[i] < ll[i] else None


class AtrBreakout(Module):
    def entry(self, s, i, p):
        e, a = s.ema(int(p["ema_period"])), s.atr(int(p["atr_period"]))
        upper = e[i] + p["band_atr"] * a[i]
        prev_upper = e[i - 1] + p["band_atr"] * a[i - 1]
        if not (s.c[i] > upper and s.c[i - 1] <= prev_upper):
            return None, f"no close through EMA {int(p['ema_period'])} + {p['band_atr']} x ATR"
        return self.atr_stop(s, i, p, [f"close broke above EMA {int(p['ema_period'])} + {p['band_atr']} x ATR "
                                       f"({upper:.6g})"], {"ema": r(e[i]), "upper_band": r(upper, 8)})

    def exit(self, s, i, p):
        e = s.ema(int(p["ema_period"]))
        return f"close back below EMA {int(p['ema_period'])}" if s.c[i] < e[i] else None


class SrBreakout(Module):
    def entry(self, s, i, p):
        k = int(p["pivot_n"])
        lvl = s.get(("swing", k), lambda: ind.swing_high_level(s.h, k))
        a = s.atr(int(p["atr_period"]))
        if lvl[i] is None or lvl[i - 1] is None:
            return None, "no confirmed resistance yet"
        trig = lvl[i] + p["buffer_atr"] * a[i]
        if not (s.c[i] > trig and s.c[i - 1] <= lvl[i - 1] + p["buffer_atr"] * a[i - 1]):
            return None, "no close through the last confirmed swing high"
        vs = s.vol_sma(20)[i]
        if vs is None or s.v[i] < p["volume_mult"] * vs:
            return None, f"breakout without volume expansion (< {p['volume_mult']} x 20-bar average)"
        return self.atr_stop(s, i, p, [f"close broke the confirmed swing high {lvl[i]:.6g} with volume expansion"],
                             {"resistance": r(lvl[i], 8), "volume_ratio": r(s.v[i] / vs, 2)})

    def exit(self, s, i, p):
        k = int(p["pivot_n"])
        lvl = s.get(("swing", k), lambda: ind.swing_high_level(s.h, k))
        a = s.atr(int(p["atr_period"]))
        # a failed breakout: back under the broken level (the level known at i)
        if lvl[i] is not None and s.c[i] < lvl[i] - 0.5 * a[i] and s.c[i - 1] < lvl[i - 1] - 0.5 * a[i - 1]:
            return "failed breakout: two closes back below the broken level"
        return None


class BreakoutRetest(Module):
    def entry(self, s, i, p):
        n, w = int(p["entry_len"]), int(p["retest_window"])
        hh, a = s.hh(n), s.atr(int(p["atr_period"]))
        # the most recent breakout bar j in the window before i
        j = next((j for j in range(i - 1, max(i - w, n) - 1, -1) if hh[j] is not None and s.c[j] > hh[j]), None)
        if j is None:
            return None, f"no {n}-bar breakout in the last {w} bars"
        level = hh[j]
        tol = p["retest_atr"] * a[i]
        if not (s.l[i] <= level + tol and s.c[i] > level and s.c[i] > s.o[i]):
            return None, f"no retest of the broken level {level:.6g}"
        stop = min(s.l[i], level) - 0.5 * a[i]
        return self.atr_stop(s, i, p, [f"retest of the level {level:.6g} broken {i - j} bars ago, closed back above"],
                             {"level": r(level, 8), "breakout_bars_ago": i - j}, stop=stop)

    def exit(self, s, i, p):
        n = int(p["exit_len"])
        ll = s.ll(n)
        return f"close below the prior {n}-bar low" if s.c[i] < ll[i] else None


class EmaPullback(Module):
    def entry(self, s, i, p):
        f, m, sl = s.ema(int(p["pull_ema"])), s.ema(int(p["mid_ema"])), s.ema(int(p["slow_ema"]))
        if not (m[i] > sl[i] and s.c[i] > m[i]):
            return None, f"no uptrend (EMA {int(p['mid_ema'])} > EMA {int(p['slow_ema'])}, close above EMA {int(p['mid_ema'])})"
        if not (s.l[i] <= f[i] < s.c[i] and s.c[i] > s.o[i]):
            return None, f"no bounce off EMA {int(p['pull_ema'])}"
        return self.atr_stop(s, i, p, [f"uptrend; low touched EMA {int(p['pull_ema'])} and closed above it (bullish bar)"],
                             {"ema_pull": r(f[i]), "ema_mid": r(m[i]), "ema_slow": r(sl[i])})

    def exit(self, s, i, p):
        m = s.ema(int(p["mid_ema"]))
        return f"close below EMA {int(p['mid_ema'])}" if s.c[i] < m[i] else None


class TrendPullback(Module):
    def entry(self, s, i, p):
        f, sl = s.sma(int(p["fast"])), s.sma(int(p["slow"]))
        if not (f[i] > sl[i] and s.c[i] > sl[i]):
            return None, f"no regime: SMA {int(p['fast'])} > SMA {int(p['slow'])} and close above SMA {int(p['slow'])}"
        if not (s.c[i - 1] < f[i - 1] and s.c[i] > f[i]):
            return None, f"no close back above SMA {int(p['fast'])} after a close below it"
        return self.atr_stop(s, i, p, [f"pullback below SMA {int(p['fast'])} recovered in an SMA {int(p['fast'])}/"
                                       f"{int(p['slow'])} uptrend"], {"sma_fast": r(f[i]), "sma_slow": r(sl[i])})

    def exit(self, s, i, p):
        sl = s.sma(int(p["slow"]))
        return f"close below SMA {int(p['slow'])}: regime invalidated" if s.c[i] < sl[i] else None


class VwapPullback(Module):
    def _vw(self, s, p):
        n = int(p["vwap_window"])
        return s.get(("vwap", n), lambda: ind.rolling_vwap(s.h, s.l, s.c, s.v, n))

    def entry(self, s, i, p):
        vw = self._vw(s, p)
        k = int(p["slope_bars"])
        if vw[i] is None or vw[i - k] is None or vw[i] <= vw[i - k]:
            return None, "rolling VWAP not rising"
        if not (s.l[i] <= vw[i] < s.c[i]):
            return None, "no touch of the VWAP with a close above it"
        return self.atr_stop(s, i, p, [f"rising {int(p['vwap_window'])}-bar VWAP touched and held"],
                             {"vwap": r(vw[i], 8)})

    def exit(self, s, i, p):
        vw, a = self._vw(s, p), s.atr(int(p["atr_period"]))
        return "close below the VWAP by more than 0.5 x ATR" if s.c[i] < vw[i] - 0.5 * a[i] else None


class BollingerReversion(Module):
    def _bb(self, s, p):
        n, k = int(p["bb_period"]), p["bb_std"]
        def build():
            m, sd = ind.sma(s.c, n), ind.stdev(s.c, n)
            return m, [None if a is None else a - k * b for a, b in zip(m, sd)]
        return s.get(("bb", n, k), build)

    def entry(self, s, i, p):
        m, lower = self._bb(s, p)
        if not (s.c[i - 1] < lower[i - 1] and s.c[i] > lower[i]):
            return None, "no close back inside the lower band"
        if p["trend_filter"] and s.c[i] < s.sma(200)[i]:
            return None, "below the 200-bar average: falling-market filter"
        return self.atr_stop(s, i, p, ["close re-entered the band after closing below the lower band"],
                             {"middle": r(m[i], 8), "lower": r(lower[i], 8)})

    def exit(self, s, i, p):
        m, _ = self._bb(s, p)
        return "close reached the middle band (mean)" if s.c[i] >= m[i] else None


class RsiReversion(Module):
    def entry(self, s, i, p):
        x, t = s.rsi(int(p["rsi_period"])), s.sma(int(p["trend"]))
        if x[i] >= p["rsi_entry"]:
            return None, f"RSI({int(p['rsi_period'])}) {x[i]:.1f} not below {p['rsi_entry']}"
        if s.c[i] <= t[i]:
            return None, f"close below SMA {int(p['trend'])}"
        return self.atr_stop(s, i, p, [f"RSI({int(p['rsi_period'])}) {x[i]:.1f} below {p['rsi_entry']} above SMA "
                                       f"{int(p['trend'])}"], {"rsi": r(x[i], 2), "sma_trend": r(t[i])})

    def exit(self, s, i, p):
        e = s.sma(int(p["exit_sma"]))
        return f"close above SMA {int(p['exit_sma'])}" if s.c[i] > e[i] else None


class ZscoreReversion(Module):
    def _z(self, s, p):
        n = int(p["z_window"])
        def build():
            m, sd = ind.sma(s.c, n), ind.stdev(s.c, n)
            return [None if a is None or not b else (c - a) / b for c, a, b in zip(s.c, m, sd)]
        return s.get(("z", n), build)

    def entry(self, s, i, p):
        z = self._z(s, p)
        a, _, _ = s.adx(14)
        if not (z[i - 1] < -p["z_entry"] <= z[i]):
            return None, f"z-score did not cross back above -{p['z_entry']}"
        if a[i] >= p["adx_max"]:
            return None, f"ADX {a[i]:.1f} >= {p['adx_max']}: trending, not a range"
        return self.atr_stop(s, i, p, [f"z-score recovered through -{p['z_entry']} in a range (ADX {a[i]:.1f})"],
                             {"z": r(z[i], 3), "adx": r(a[i], 2)})

    def exit(self, s, i, p):
        z = self._z(s, p)
        return "z-score back to the mean" if z[i] >= p["z_exit"] else None


class ObvTrend(Module):
    def _o(self, s, p):
        n = int(p["obv_ema"])
        def build():
            o = ind.obv(s.c, s.v)
            return o, ind.ema(o, n)
        return s.get(("obv", n), build)

    def entry(self, s, i, p):
        o, oe = self._o(s, p)
        t = s.ema(int(p["trend"]))
        if not crossed_above(o, oe, i):
            return None, "OBV did not cross above its EMA"
        if not (s.c[i] > t[i] and t[i] > t[i - 5]):
            return None, f"price not above a rising EMA {int(p['trend'])}"
        return self.atr_stop(s, i, p, ["OBV crossed above its EMA with price above a rising EMA"],
                             {"obv": r(o[i], 2), "obv_ema": r(oe[i], 2), "ema_trend": r(t[i])})

    def exit(self, s, i, p):
        o, oe = self._o(s, p)
        t = s.ema(int(p["trend"]))
        return "OBV below its EMA and close below the trend EMA" if o[i] < oe[i] and s.c[i] < t[i] else None


class RegimeSwitch(Module):
    def entry(self, s, i, p):
        a, _, _ = s.adx(14)
        if a[i] >= p["trend_adx"]:
            hh = s.hh(20)
            if s.c[i] > hh[i]:
                return self.atr_stop(s, i, p, [f"trend regime (ADX {a[i]:.1f}): 20-bar breakout"],
                                     {"regime": "trend", "adx": r(a[i], 2), "channel_high": r(hh[i], 8)})
            return None, f"trend regime (ADX {a[i]:.1f}) but no 20-bar breakout"
        if a[i] <= p["range_adx"]:
            m, sd = s.sma(20), s.get(("sd", 20), lambda: ind.stdev(s.c, 20))
            lo_now, lo_prev = m[i] - 2 * sd[i], m[i - 1] - 2 * sd[i - 1]
            if s.c[i - 1] < lo_prev and s.c[i] > lo_now:
                return self.atr_stop(s, i, p, [f"range regime (ADX {a[i]:.1f}): back inside the lower band"],
                                     {"regime": "range", "adx": r(a[i], 2), "lower": r(lo_now, 8)})
            return None, f"range regime (ADX {a[i]:.1f}) but no band re-entry"
        return None, f"ADX {a[i]:.1f} between {p['range_adx']} and {p['trend_adx']}: no clear regime, no trade"

    def exit(self, s, i, p):
        ll = s.ll(10)
        if s.c[i] < ll[i]:
            return "close below the prior 10-bar low"
        a, _, _ = s.adx(14)
        if a[i] <= p["range_adx"] and s.c[i] >= s.sma(20)[i]:
            return "range regime: close back at the 20-bar mean"
        return None


class MetaMuseModule(Module):
    """Backtest form of the existing live Meta Muse (strategies/meta_muse.py):
    same EMA definition (pandas ewm adjust=False over the last `candles`
    closes), same rules; the live engine keeps using its original class."""

    def _trend(self, closes: list[float], p):
        from yonixalpha_crypto.strategies.meta_muse import trend
        return trend(closes, int(p["fast"]), int(p["slow"]), p["trend_threshold"])

    def _pair(self, s, i, p):
        n = int(p["candles"])
        if s.lead is None or i + 1 < n:
            return None
        return self._trend(s.lead.c[i + 1 - n:i + 1], p), self._trend(s.c[i + 1 - n:i + 1], p)

    def lookback(self, p):
        return int(p["candles"])

    def warmup(self, p):
        return int(p["candles"]) + 2

    def entry(self, s, i, p):
        pair = self._pair(s, i, p)
        if pair is None:
            return None, "needs the leading symbol's bars and `candles` of history"
        t1, t2 = pair
        if not (t1.strong and t2.strong and t1.direction == "down" and t2.direction == "up"):
            return None, "no divergence of two strong trends (LONG needs leader down, traded up)"
        close = s.c[i]
        stop = close * (1 - p["stop_pct"])
        target = close * (1 + p["target_pct"])
        return Entry(stop, target, ["leader strong down, traded strong up"],
                     {"lead_sep": r(t1.separation, 5), "traded_sep": r(t2.separation, 5), "close": r(close, 8),
                      "stop": r(stop, 8), "target": r(target, 8)}), "entry"

    def exit(self, s, i, p):
        pair = self._pair(s, i, p)
        if pair is None:
            return None
        t1, t2 = pair
        if not (t1.strong and t2.strong):
            return "trend weakened"
        if not (t1.direction == "down" and t2.direction == "up"):
            return "signal no longer LONG"
        return None


def P(**kw) -> dict[str, tuple[Any, str, Any, Any]]:
    return kw


MODULES: list[Module] = [
    MetaMuseModule(
        "meta_muse", "Meta Muse Crossover (BTC trend vs ETH trend)", "trend divergence",
        "Trades the second symbol when two strong EMA trends diverge (pair rule of the original bot).",
        "Long ETH when BTC's EMA 9/21 trend is strongly DOWN and ETH's is strongly UP (separation >= 0.15%).",
        "Stop 1%, target 2%; exit early when either trend is no longer strong or the signal is no longer LONG.",
        P(fast=(9, "int", 2, 100), slow=(21, "int", 3, 400), trend_threshold=(D("0.0015"), "dec", "0", "0.05"),
          stop_pct=(D("0.01"), "dec", "0.001", "0.2"), target_pct=(D("0.02"), "dec", "0.001", "0.5"),
          candles=(150, "int", 30, 1000)),
        "5m", research("trend divergence (pair)", "Binance USD-M futures, BTC/ETH, 5m", ["metamuse"],
                       "none published; the author's README makes no verified performance claim",
                       "the two assets decouple for a while", "correlation snaps back; whipsaws on 5m",
                       "short ETH when BTC strong UP and ETH strong DOWN",
                       licence="hoziertom44-arch/Meta_Muse_Crossover_strategy @a163ff7, no licence: rules re-implemented"),
        intervals=("5m", "15m", "1h"), default_symbols=("BTC/USDT", "ETH/USDT"), needs_lead=True),
    SmaCross(
        "sma_cross", "SMA crossover 20/50", "trend following",
        "Classic moving-average crossover: follows a trend once the faster average crosses the slower one.",
        "Long when SMA 20 crosses above SMA 50 on a closed bar.",
        "Exit when SMA 20 crosses below SMA 50, or at the 2.5 x ATR stop; ATR trailing stop after +1 R.",
        P(fast=(20, "int", 2, 200), slow=(50, "int", 5, 400), stop_atr=(D("2.5"), "dec", "0.5", "10")), "4h",
        research("trend following", "equity index (daily); applied here to crypto 4h", ["bll", "stw", "hu"],
                 "BLL found MA rules informative on the DJIA 1897-1986; STW showed that much of it does not survive "
                 "data-snooping corrections out of sample; Hudson & Urquhart report predictability in crypto with no "
                 "out-of-sample predictability for Bitcoin", "long, persistent trends", "sideways markets (many "
                 "small losses from whipsaws)", "short when SMA 20 crosses below SMA 50"),
        default_exits=TRAIL_ATR),
    EmaCross(
        "ema_cross", "EMA crossover 12/26 with 200 filter", "trend following",
        "Faster EMA crossover, taken only above the 200-bar EMA (long-term trend filter) to avoid counter-trend entries.",
        "Long when EMA 12 crosses above EMA 26 and the close is above EMA 200.",
        "Exit when EMA 12 crosses below EMA 26, or at the 2 x ATR stop; ATR trailing after +1 R.",
        P(fast=(12, "int", 2, 100), slow=(26, "int", 5, 300), trend=(200, "int", 50, 400)), "1h",
        research("trend following", "general; EMA variant of the BLL moving-average family", ["bll", "stw", "freqtrade"],
                 "same family evidence and caveats as the SMA crossover", "trending markets above the long average",
                 "range-bound markets; late entries after sharp moves", "short when EMA 12 < EMA 26 below EMA 200"),
        default_exits=TRAIL_ATR),
    AdxTrend(
        "adx_trend", "ADX / DMI trend strength", "trend following",
        "Wilder's directional movement: enter when buyers take control (+DI over -DI) while trend strength (ADX) is high.",
        "Long when +DI crosses above -DI and ADX(14) is at least 25.",
        "Exit when -DI rises above +DI or ADX falls below 20; 2 x ATR stop.",
        P(adx_period=(14, "int", 5, 50), adx_min=(D("25"), "dec", "10", "50"), adx_exit=(D("20"), "dec", "5", "40")), "4h",
        research("trend strength", "commodities (daily), Wilder 1978", ["wilder", "ea31337"],
                 "definition source; no robust independent crypto evidence found", "strong directional moves",
                 "choppy markets: DI crossovers flip often", "short when -DI crosses above +DI with ADX >= 25"),
        default_exits=TRAIL_ATR),
    MtfTrend(
        "mtf_trend", "Multi-timeframe trend confirmation", "trend following",
        "Trades the lower timeframe only in the direction of a rising higher-timeframe trend (built from the same bars).",
        "Long when the last COMPLETED higher-timeframe bar (4 x the interval) closes above its rising EMA 50 and the "
        "lower-timeframe close crosses above EMA 20.",
        "Exit on two closes below EMA 20, a higher-timeframe close below its EMA, or the 2 x ATR stop.",
        P(htf_factor=(4, "int", 2, 12), htf_ema=(50, "int", 10, 200), ltf_ema=(20, "int", 5, 100)), "1h",
        research("trend following, multi-timeframe", "general practice", ["mop", "sepp"],
                 "time-series momentum evidence (MOP) is at monthly horizons; intraday transfer is unverified",
                 "aligned trends on both timeframes", "turning points (the higher timeframe lags)",
                 "short when the higher trend falls and the close crosses below EMA 20"),
        default_exits=TRAIL_ATR),
    RsiMomentum(
        "rsi_momentum", "RSI momentum continuation", "momentum",
        "Uses RSI as a momentum gauge (not as overbought/oversold): buy when momentum turns positive inside an uptrend.",
        "Long when RSI(14) crosses above 55 and the close is above EMA 50.",
        "Exit when RSI falls below 45, or at the 2 x ATR stop.",
        P(rsi_period=(14, "int", 2, 50), rsi_entry=(D("55"), "dec", "50", "80"), rsi_exit=(D("45"), "dec", "20", "50"),
          trend=(50, "int", 10, 300)), "1h",
        research("momentum", "general", ["wilder", "freqtrade"], "no robust independent evidence for this exact rule",
                 "persistent moves after consolidation", "fast reversals", "short when RSI crosses below 45 below EMA 50")),
    MacdMomentum(
        "macd_momentum", "MACD momentum", "momentum",
        "Appel's MACD: enter when short-term momentum accelerates above the signal line in positive territory.",
        "Long when the MACD line (12, 26) crosses above its 9-period signal line while MACD is above zero.",
        "Exit when MACD crosses below its signal line, or at the 2 x ATR stop.",
        P(fast=(12, "int", 2, 50), slow=(26, "int", 5, 100), signal=(9, "int", 2, 50)), "4h",
        research("momentum", "general", ["appel", "freqtrade", "ea31337"], "no robust independent evidence for this rule",
                 "accelerating trends", "flat markets around zero", "short when MACD crosses below the signal below zero")),
    RocMomentum(
        "roc_momentum", "Rate-of-change (time-series) momentum", "momentum",
        "Time-series momentum: buy when the asset's own return over the lookback turns strongly positive.",
        "Long when ROC(24) rises through +2% (was below on the previous bar), is higher than 3 bars ago, and the close "
        "is above SMA 100.",
        "Exit when ROC(24) turns negative, or at the 2 x ATR stop.",
        P(roc_period=(24, "int", 3, 200), roc_min=(D("2"), "dec", "0", "50"), trend=(100, "int", 20, 400)), "4h",
        research("momentum", "futures, monthly (MOP); crypto, weekly (Liu & Tsyvinski)", ["mop", "lt"],
                 "time-series momentum is documented in futures and in crypto at weekly-monthly horizons; a critique "
                 "attributes much of the futures result to volatility scaling; intraday horizons are not covered",
                 "persistent trends", "sharp reversals after extended moves", "short when ROC falls through -2%")),
    Donchian(
        "donchian_breakout", "Donchian channel breakout (20/10)", "breakout",
        "Turtle-style channel breakout: buy a new 20-bar high, exit on a 10-bar low.",
        "Long when the close is above the highest high of the previous 20 bars.",
        "Exit when the close is below the lowest low of the previous 10 bars, or at the 2 x ATR stop.",
        P(entry_len=(20, "int", 5, 200), exit_len=(10, "int", 3, 100)), "4h",
        research("breakout / trend following", "futures (daily), the Turtle rules", ["turtle", "bll", "sepp", "hu"],
                 "trading-range-break rules are part of BLL's study (same snooping caveats); channel breakouts are "
                 "standard in trend-following research", "trend starts after ranges", "false breakouts in ranges",
                 "short below the 20-bar low")),
    AtrBreakout(
        "atr_breakout", "ATR volatility breakout (Keltner-style)", "breakout",
        "Buys when price moves more than k ATRs above its average: a volatility-adjusted breakout.",
        "Long when the close crosses above EMA 20 + 2 x ATR(14).",
        "Exit when the close falls back below EMA 20, or at the 2 x ATR stop.",
        P(ema_period=(20, "int", 5, 200), band_atr=(D("2"), "dec", "0.5", "5")), "1h",
        research("volatility breakout", "general (Keltner channels)", ["wilder", "ea31337"],
                 "no robust independent evidence for this rule", "volatility expansion into a trend",
                 "spikes that revert", "short below EMA 20 - 2 x ATR")),
    SrBreakout(
        "sr_breakout", "Resistance breakout with volume expansion", "breakout",
        "Breaks the last CONFIRMED swing high (a pivot known only after it formed) and requires a volume surge.",
        "Long when the close breaks above the last confirmed swing high (5 bars each side) + 0.1 x ATR, with volume "
        "at least 1.5 x its 20-bar average.",
        "Exit after two closes back below the broken level - 0.5 x ATR (failed breakout), at 2 R, or the 2 x ATR stop.",
        P(pivot_n=(5, "int", 2, 20), buffer_atr=(D("0.1"), "dec", "0", "2"), volume_mult=(D("1.5"), "dec", "1", "5"),
          target_r=(D("2"), "dec", "0", "10")), "1h",
        research("breakout with volume", "forex M15 (confluence repository's pivot breakout)", ["confluence", "granville"],
                 "the source repository's backtest charges no spread or commission: not evidence",
                 "range ends with participation", "low-volume fake-outs", "short below the last confirmed swing low",
                 licence="rule idea from siriusforex-ai/confluence_matrix_forex @f3beff9 (no licence); independent "
                         "implementation"),
        default_exits={"partials": [{"at_r": 1, "fraction": 0.5, "stop_to": "breakeven"}]}),
    BreakoutRetest(
        "breakout_retest", "Breakout retest", "breakout",
        "Waits for a broken level to be retested and hold before buying, instead of chasing the breakout bar.",
        "After a close above the previous 20-bar high, within 10 bars: a bar whose low comes back to the broken level "
        "(within 0.25 x ATR) and closes above it and above its open.",
        "Stop 0.5 x ATR below the retest low / level; exit below the prior 10-bar low; target 2 R.",
        P(entry_len=(20, "int", 5, 200), retest_window=(10, "int", 2, 50), retest_atr=(D("0.25"), "dec", "0", "2"),
          exit_len=(10, "int", 3, 100), target_r=(D("2"), "dec", "0", "10")), "1h",
        research("breakout", "general practice", ["turtle", "freqtrade"], "no robust independent evidence",
                 "breakouts that become support", "breakouts that fail without a retest (missed) or fail after it",
                 "short on a retest of a broken low")),
    EmaPullback(
        "ema_pullback", "EMA pullback in an uptrend", "trend pullback",
        "Buys a dip to the fast EMA while the medium EMA is above the slow EMA.",
        "Long when EMA 50 > EMA 200, the close is above EMA 50, the bar's low touches EMA 20 and it closes above EMA 20 "
        "as a bullish bar.",
        "Exit when the close falls below EMA 50, or at the 2 x ATR stop; ATR trailing after +1 R.",
        P(pull_ema=(20, "int", 5, 100), mid_ema=(50, "int", 10, 200), slow_ema=(200, "int", 50, 400)), "1h",
        research("trend pullback", "general practice", ["freqtrade", "ea31337"], "no robust independent evidence",
                 "orderly trends with shallow pullbacks", "trend ends (the pullback keeps going)",
                 "short on a rally to EMA 20 in a downtrend"),
        default_exits=TRAIL_ATR),
    TrendPullback(
        "trend_pullback", "Trend pullback SMA 10/50", "trend pullback",
        "Buys the first recovery above SMA 10 after a dip, while SMA 10 is above SMA 50 (the rule selected earlier).",
        "Long when SMA 10 > SMA 50, the close is above SMA 50, the previous close was below SMA 10 and this close is "
        "above it.",
        "Stop 3 x ATR; exit on a close below SMA 50; ATR trailing after +1 R; time stop 48 bars below +0.5 R.",
        P(fast=(10, "int", 2, 100), slow=(50, "int", 10, 300), stop_atr=(D("3"), "dec", "0.5", "10")), "1h",
        research("trend pullback", "index futures, daily (trend-bot)", ["trendbot", "bll"],
                 "the source repository publishes no verifiable results", "steady trends", "trend reversals",
                 "short on a recovery below SMA 10 in an SMA 10 < SMA 50 downtrend",
                 licence="rule idea from arwensorcenelli/trend-bot @ab75bb1 (no licence); independent implementation"),
        default_exits={**TRAIL_ATR, "time_stop_bars": 48}),
    VwapPullback(
        "vwap_pullback", "Rolling VWAP pullback", "trend pullback",
        "Buys a touch of a rising volume-weighted average price (24-bar rolling, since crypto has no session).",
        "Long when the 24-bar VWAP is higher than 6 bars ago and the bar's low touches the VWAP while it closes above.",
        "Exit when the close is more than 0.5 x ATR below the VWAP, or at the 2 x ATR stop.",
        P(vwap_window=(24, "int", 6, 200), slope_bars=(6, "int", 1, 50)), "1h",
        research("trend pullback, volume-weighted", "equity intraday (session VWAP); here a rolling window",
                 ["freqtrade"], "VWAP is an execution benchmark; no robust evidence for this entry rule",
                 "orderly up-moves with volume", "high-volatility sell-offs", "short on a touch of a falling VWAP")),
    BollingerReversion(
        "bollinger_reversion", "Bollinger Band reversion", "mean reversion",
        "Buys when price closes back inside the lower band after an excursion below it, expecting a return to the mean.",
        "Long when the previous close was below the lower band (20, 2 sd) and this close is back above it (optionally "
        "only above the 200-bar average).",
        "Exit at the middle band, at the 2 x ATR stop, or after 24 bars (time stop).",
        P(bb_period=(20, "int", 5, 200), bb_std=(D("2"), "dec", "1", "4"), trend_filter=(1, "int", 0, 1)), "1h",
        research("mean reversion", "general", ["bollinger", "freqtrade"], "no robust independent evidence for crypto",
                 "ranges and orderly pullbacks", "strong down-trends (bands walk down)",
                 "short when price closes back inside the upper band"),
        default_exits=MEANREV_EXITS, extra_lookback=200),
    RsiReversion(
        "rsi_reversion", "RSI(2) reversion above the 200 average", "mean reversion",
        "Connors' short-term reversion: buy a deeply oversold 2-period RSI while the long-term trend is up.",
        "Long when RSI(2) is below 10 and the close is above SMA 200.",
        "Exit when the close is above SMA 5; 3 x ATR stop (a protective addition: the published rule has no stop); "
        "time stop 24 bars.",
        P(rsi_period=(2, "int", 2, 14), rsi_entry=(D("10"), "dec", "1", "40"), trend=(200, "int", 50, 400),
          exit_sma=(5, "int", 2, 50), stop_atr=(D("3"), "dec", "0.5", "10")), "4h",
        research("mean reversion", "US equities and ETFs, daily (Connors & Alvarez)", ["connors"],
                 "the authors' published statistics are their own, on equities; not independently verified for crypto",
                 "pullbacks inside long uptrends", "crashes (the published rule has no stop)",
                 "short when RSI(2) > 90 below SMA 200"),
        default_exits=MEANREV_EXITS),
    ZscoreReversion(
        "zscore_reversion", "Rolling z-score reversion (range filter)", "mean reversion",
        "Statistical reversion: buy when price recovers from more than 2 standard deviations below its 50-bar mean, only "
        "when ADX says the market is ranging.",
        "Long when the 50-bar z-score crosses back above -2 and ADX(14) is below 25.",
        "Exit when the z-score returns to 0, at the 2 x ATR stop, or after 24 bars.",
        P(z_window=(50, "int", 10, 300), z_entry=(D("2"), "dec", "0.5", "4"), z_exit=(D("0"), "dec", "-1", "2"),
          adx_max=(D("25"), "dec", "10", "50")), "1h",
        research("mean reversion, statistical", "general (pairs / stat-arb practice)", ["freqtrade"],
                 "no robust independent evidence for single-asset crypto", "ranges", "regime change into a trend",
                 "short when the z-score falls back below +2"),
        default_exits=MEANREV_EXITS, extra_lookback=40),
    ObvTrend(
        "obv_trend", "OBV-confirmed trend", "volume",
        "Granville's on-balance volume: enter when accumulation (OBV above its average) confirms a rising price trend.",
        "Long when OBV crosses above its 20-bar EMA while the close is above a rising EMA 50.",
        "Exit when OBV is below its EMA and the close is below EMA 50, or at the 2 x ATR stop.",
        P(obv_ema=(20, "int", 5, 100), trend=(50, "int", 10, 300)), "4h",
        research("volume confirmation", "equities (Granville)", ["granville", "ea31337"],
                 "no robust independent evidence", "trends with participation",
                 "volume spikes from liquidations or wash trading", "short when OBV crosses below its EMA under a falling EMA 50"),
        default_exits=TRAIL_ATR),
    RegimeSwitch(
        "regime_switch", "Volatility / trend regime switch", "hybrid",
        "Chooses its rule by regime: breakouts when ADX shows a trend, band reversion when ADX shows a range, nothing in "
        "between.",
        "ADX(14) >= 25: long on a close above the prior 20-bar high. ADX <= 20: long when the close re-enters the lower "
        "Bollinger band (20, 2). Otherwise no trade.",
        "Exit below the prior 10-bar low; in a range regime also at the 20-bar mean; 2 x ATR stop.",
        P(trend_adx=(D("25"), "dec", "15", "50"), range_adx=(D("20"), "dec", "5", "40")), "1h",
        research("hybrid / adaptive", "general", ["wilder", "bollinger", "turtle"],
                 "no robust independent evidence; regime filters reduce but do not remove whipsaws",
                 "clear regimes", "transitions between regimes", "mirror rules for shorts"),
        default_exits={"time_stop_bars": 48}, extra_lookback=40),
]

REJECTED = [
    {"id": "volume_momentum", "name": "Momentum with volume confirmation", "status": "MERGED",
     "reason": "not a distinct rule: every module has the shared min_volume_ratio filter, and sr_breakout requires "
               "volume expansion"},
    {"id": "volume_expansion_breakout", "name": "Volume expansion breakout", "status": "MERGED",
     "reason": "implemented as sr_breakout (breakout of a confirmed level with a volume surge)"},
    {"id": "atr_regime", "name": "ATR / volatility regime strategy", "status": "MERGED",
     "reason": "covered by regime_switch (ADX regime) and atr_breakout (ATR bands)"},
    {"id": "trend_momentum_confluence", "name": "Trend plus momentum confluence", "status": "REJECTED",
     "reason": "cosmetic duplicate of rsi_momentum / macd_momentum with a trend filter (both already filter on trend)"},
    {"id": "mtf_pullback", "name": "Multi-timeframe pullback confirmation", "status": "MERGED",
     "reason": "the higher-timeframe filter of mtf_trend applied to a pullback duplicates ema_pullback's EMA 50 > 200 "
               "regime"},
    {"id": "mtf_confluence", "name": "Multi-timeframe confluence", "status": "REJECTED",
     "reason": "no distinct, testable rule beyond mtf_trend"},
    {"id": "hyperliquid_grid", "name": "Hyperliquid grid", "status": "NOT IMPLEMENTED",
     "reason": "separate venue; the source pauses (does not close) on drawdown, leaving inventory exposed; its trade log "
               "cannot be verified"},
    {"id": "confluence_matrix", "name": "Confluence Matrix (forex)", "status": "BLOCKED",
     "reason": "no forex data source; MetaTrader 5 only; its backtest charges no costs"},
    {"id": "gold_vs_btc", "name": "Gold vs BTC", "status": "BLOCKED", "reason": "gold source not chosen"},
]

BY_ID: dict[str, Module] = {m.id: m for m in MODULES}


# ---- live adapter (the engine's Strategy protocol) -----------------------

class LibraryAdapter:
    """Lets the paper / exchange-demo runner use a library module exactly as
    the backtester does: build the series from the closed candles and
    evaluate the LAST bar."""

    def __init__(self, module: Module):
        self.module = module
        self.spec = registry.get(module.id)

    def required_candles(self, params: dict[str, Any]) -> int:
        return self.module.warmup(self.module.p(params))

    def traded_symbol(self, symbols: list[str]) -> str:
        return symbols[-1]

    def evaluate(self, candles: dict[str, list[Candle]], params: dict[str, Any], position_open: bool) -> Decision:
        syms = list(candles)
        lead = candles[syms[0]] if self.module.needs_lead and len(syms) > 1 else None
        s = Series.from_candles(candles[syms[-1]], lead)
        i = len(s) - 1
        if position_open:
            why = self.module.should_exit(s, i, params)
            return Decision("LONG", "EXIT", reasons=[why]) if why else Decision("LONG", "HOLD", reasons=["no exit rule met"])
        e, why = self.module.decide(s, i, params)
        if e is None:
            return Decision("NONE", "NONE", reasons=[why])
        return Decision("LONG", "ENTER", stop_price=Decimal(str(e.stop)),
                        target_price=Decimal(str(e.target)) if e.target is not None else None,
                        reasons=e.reasons, detail={"values": e.values, "module_version": self.module.version})


def _register_all() -> None:
    for m in MODULES:
        existing = registry.get(m.id)
        if m.id == "meta_muse" and existing is not None:
            registry.register(registry.replace(existing, description=m.description, entry_rules=m.entry_rules,
                                               exit_rules=m.exit_rules, research=m.research,
                                               default_exits=m.default_exits, library=True))
            continue  # the live engine keeps the original Meta Muse class
        registry.register(registry.StrategySpec(
            id=m.id, name=m.name, version=m.version, category=m.category, status=registry.IMPLEMENTED,
            status_reason="", provenance={"licence": m.research["licence"]}, exchanges=registry.SPOT_EXCHANGES,
            intervals=m.intervals, default_exchange="binance_us", default_symbols=m.default_symbols,
            default_interval=m.default_interval, params={**COMMON_PARAMS, **m.params},
            notes=("spot: LONG only; short rule documented, not executed",), max_symbols=1,
            description=m.description, entry_rules=m.entry_rules, exit_rules=m.exit_rules, research=m.research,
            default_exits=m.default_exits, library=True))
        IMPLEMENTATIONS[m.id] = LibraryAdapter(m)


_register_all()
