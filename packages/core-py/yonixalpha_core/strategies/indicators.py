"""Indicator math in plain Python, matching the pandas formulas used by the
reference repositories exactly (so ported strategies produce the same
numbers without pulling pandas into every service):

- ema: pandas `ewm(span=n, adjust=False).mean()`
- rsi: Wilder, `ewm(alpha=1/n, adjust=False)` on gains/losses, first diff 0
- atr: Wilder smoothing of true range, first TR = high - low
- rolling_std: sample standard deviation (ddof=1), None until full window
"""

import math
from collections.abc import Sequence


def ema(values: Sequence[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def _wilder(values: Sequence[float], period: int) -> list[float]:
    alpha = 1 / period
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    if not closes:
        return []
    gains, losses = [0.0], [0.0]
    for a, b in zip(closes, closes[1:]):
        d = float(b) - float(a)
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    ag, al = _wilder(gains, period), _wilder(losses, period)
    # pandas: rs = avg_gain / avg_loss.replace(0, NaN); RSI = 100 - 100/(1+rs);
    # fillna(50). A zero average loss therefore reads 50, not 100.
    return [50.0 if lo == 0 else 100 - 100 / (1 + g / lo) for g, lo in zip(ag, al)]


def true_range(highs, lows, closes) -> list[float]:
    tr = []
    for i, (h, lo) in enumerate(zip(highs, lows)):
        if i == 0:
            tr.append(float(h) - float(lo))
        else:
            pc = float(closes[i - 1])
            tr.append(max(float(h) - float(lo), abs(float(h) - pc), abs(float(lo) - pc)))
    return tr


def atr(highs, lows, closes, period: int = 14) -> list[float]:
    tr = true_range(highs, lows, closes)
    return _wilder(tr, period) if tr else []


def rolling_std(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        w = [float(v) for v in values[i + 1 - window:i + 1]]
        m = sum(w) / window
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / (window - 1)))
    return out


def log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(float(b) / float(a)) for a, b in zip(closes, closes[1:]) if float(a) > 0 and float(b) > 0]


def stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb) if va > 0 and vb > 0 else None
