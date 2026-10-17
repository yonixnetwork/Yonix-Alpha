"""Causal technical indicators on plain float lists.

Every function returns a list aligned with its input, with None until the
indicator is warmed up, and the value at index i uses ONLY inputs at
indices <= i (tests/test_library.py checks this for every indicator by
recomputing on truncated data). Definitions follow the original authors:
Wilder (1978) for RSI, ATR and ADX (Wilder smoothing), Appel for MACD,
Bollinger for the bands, Donchian channels over the PREVIOUS n bars (so a
breakout compares the current close with levels that already existed),
Granville for OBV.
"""

from __future__ import annotations

import math

Num = float | None


def sma(x: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x: list[float], n: int) -> list[Num]:
    """Seeded with the SMA of the first n values (no dependence on how far
    back the data starts once warmed up beyond that)."""
    out: list[Num] = [None] * len(x)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    prev = sum(x[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(x)):
        prev = a * x[i] + (1 - a) * prev
        out[i] = prev
    return out


def ema_of(x: list[Num], n: int) -> list[Num]:
    """EMA of a series that starts with None values."""
    start = next((i for i, v in enumerate(x) if v is not None), None)
    out: list[Num] = [None] * len(x)
    if start is None:
        return out
    tail = ema([float(v) for v in x[start:]], n)  # type: ignore[arg-type]
    out[start:] = tail
    return out


def stdev(x: list[float], n: int) -> list[Num]:
    """Population standard deviation over the last n values (as Bollinger)."""
    out: list[Num] = [None] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def wilder(x: list[float], n: int, start: int = 0) -> list[Num]:
    """Wilder smoothing: first value = mean of n inputs, then (prev*(n-1)+v)/n."""
    out: list[Num] = [None] * len(x)
    if len(x) - start < n:
        return out
    prev = sum(x[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(x)):
        prev = (prev * (n - 1) + x[i]) / n
        out[i] = prev
    return out


def true_range(h: list[float], lo: list[float], c: list[float]) -> list[float]:
    tr = [h[0] - lo[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    return tr


def atr(h: list[float], lo: list[float], c: list[float], n: int = 14) -> list[Num]:
    return wilder(true_range(h, lo, c), n)


def rsi(c: list[float], n: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(c)
    if len(c) <= n:
        return out
    gains = [0.0] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    losses = [0.0] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    ag, al = wilder(gains, n, 1), wilder(losses, n, 1)
    for i in range(len(c)):
        if ag[i] is not None and al[i] is not None:
            out[i] = 100.0 if al[i] == 0 else 100.0 - 100.0 / (1.0 + ag[i] / al[i])
    return out


def adx(h: list[float], lo: list[float], c: list[float], n: int = 14) -> tuple[list[Num], list[Num], list[Num]]:
    """(ADX, +DI, -DI), Wilder."""
    k = len(c)
    pdm, ndm = [0.0] * k, [0.0] * k
    for i in range(1, k):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0
    tr = true_range(h, lo, c)
    tr[0] = 0.0
    s_tr, s_p, s_n = wilder(tr, n, 1), wilder(pdm, n, 1), wilder(ndm, n, 1)
    pdi: list[Num] = [None] * k
    ndi: list[Num] = [None] * k
    dx = [0.0] * k
    first = None
    for i in range(k):
        if s_tr[i]:
            pdi[i] = 100.0 * s_p[i] / s_tr[i]  # type: ignore[operator]
            ndi[i] = 100.0 * s_n[i] / s_tr[i]  # type: ignore[operator]
            tot = pdi[i] + ndi[i]  # type: ignore[operator]
            dx[i] = 100.0 * abs(pdi[i] - ndi[i]) / tot if tot else 0.0  # type: ignore[operator]
            first = i if first is None else first
    a: list[Num] = [None] * k
    if first is not None:
        a = wilder(dx, n, first)
    return a, pdi, ndi


def macd(c: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[Num], list[Num]]:
    f, s = ema(c, fast), ema(c, slow)
    line: list[Num] = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    return line, ema_of(line, signal)


def roc(c: list[float], n: int) -> list[Num]:
    return [None if i < n or c[i - n] == 0 else (c[i] / c[i - n] - 1.0) * 100.0 for i in range(len(c))]


def highest_prev(x: list[float], n: int) -> list[Num]:
    """Highest of the n bars BEFORE i (excludes bar i)."""
    return [max(x[i - n:i]) if i >= n else None for i in range(len(x))]


def lowest_prev(x: list[float], n: int) -> list[Num]:
    return [min(x[i - n:i]) if i >= n else None for i in range(len(x))]


def obv(c: list[float], v: list[float]) -> list[float]:
    out = [0.0]
    for i in range(1, len(c)):
        out.append(out[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0.0))
    return out


def rolling_vwap(h: list[float], lo: list[float], c: list[float], v: list[float], n: int) -> list[Num]:
    """Volume-weighted typical price over the last n bars (crypto trades 24/7,
    so a rolling window replaces the session anchor of equity VWAP)."""
    out: list[Num] = [None] * len(c)
    pv = [(h[i] + lo[i] + c[i]) / 3.0 * v[i] for i in range(len(c))]
    for i in range(n - 1, len(c)):
        vol = sum(v[i - n + 1:i + 1])
        out[i] = sum(pv[i - n + 1:i + 1]) / vol if vol > 0 else None
    return out


def swing_high_level(h: list[float], k: int) -> list[Num]:
    """The most recent CONFIRMED swing high known at i: a bar j whose high is
    the maximum of j-k..j+k, usable only from i >= j+k (when the k bars
    after it have closed)."""
    out: list[Num] = [None] * len(h)
    last: Num = None
    for i in range(len(h)):
        j = i - k
        if j - k >= 0 and h[j] == max(h[j - k:j + k + 1]):
            last = h[j]
        out[i] = last
    return out


def resample_last_closed(c: list[float], m: int) -> tuple[list[float], list[int]]:
    """Group every m bars (from index 0) into one higher-timeframe close.
    Returns (closes of the groups, for each base index i the index of the
    last group COMPLETED at or before i, or -1)."""
    groups = [c[g + m - 1] for g in range(0, len(c) - m + 1, m)]
    done = [((i + 1) // m) - 1 for i in range(len(c))]
    return groups, done
