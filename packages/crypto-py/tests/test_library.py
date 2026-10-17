"""The 20-module strategy library: every module's entry and exit rule fires
where its rule says and only there, no module can see the future, and the
live adapter decides exactly like the backtest path.

Scenarios are SYNTHETIC (seeded random walks with a chosen shape). They
test rule mechanics only and say nothing about profitability."""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from yonixalpha_crypto import registry
from yonixalpha_crypto.candles import Candle
from yonixalpha_crypto.strategies import IMPLEMENTATIONS
from yonixalpha_crypto.strategies import indicators as ind
from yonixalpha_crypto.strategies.library import BY_ID, MODULES, REJECTED, LibraryAdapter, Series


def walk(shape: list[tuple[int, float]], seed: int = 1, start: float = 100.0, vol: float = 0.004,
         volume_spikes: bool = True) -> Series:
    """shape: [(bars, drift per bar)]; returns OHLCV with intrabar range."""
    rnd = random.Random(seed)
    o, h, lo, c, v = [], [], [], [], []
    p = start
    for n, drift in shape:
        for _ in range(n):
            op = p
            p = max(1.0, p * (1 + drift + rnd.gauss(0, vol)))
            hi = max(op, p) * (1 + abs(rnd.gauss(0, vol / 2)))
            low = min(op, p) * (1 - abs(rnd.gauss(0, vol / 2)))
            o.append(op), h.append(hi), lo.append(low), c.append(p)
            base = 100 + rnd.random() * 20
            v.append(base * (3 if volume_spikes and abs(p / op - 1) > 2 * vol else 1))
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Series(o, h, lo, c, v, [t0 + timedelta(hours=k) for k in range(len(c))])


TREND = [(300, -0.0005), (250, 0.003), (150, -0.004)]  # base, up-trend, crash
PULLBACK = [(300, 0.0015), (20, -0.004), (60, 0.003), (25, -0.004), (60, 0.003), (150, -0.004)]
RANGE = [(500, 0.0), (10, -0.012), (20, 0.004), (200, 0.0), (10, -0.012), (30, 0.004), (150, 0.0)]
SCENARIO = {
    "sma_cross": TREND, "ema_cross": PULLBACK, "adx_trend": [(250, -0.004), (250, 0.004), (150, -0.004)], "mtf_trend": TREND, "rsi_momentum": TREND,
    "macd_momentum": TREND, "roc_momentum": TREND, "donchian_breakout": TREND, "atr_breakout": TREND,
    "sr_breakout": [(200, 0.0), (5, 0.006), (40, -0.001), (60, 0.004), (150, -0.004)],
    "breakout_retest": [(200, 0.0), (8, 0.006), (6, -0.002), (60, 0.002), (150, -0.004)],
    "ema_pullback": PULLBACK, "trend_pullback": PULLBACK, "vwap_pullback": PULLBACK, "obv_trend": TREND,
    "bollinger_reversion": [(300, 0.0012)] + RANGE[1:], "rsi_reversion": [(300, 0.0015), (4, -0.01), (30, 0.002),
                                                                          (6, -0.01), (100, 0.002), (150, -0.004)],
    "zscore_reversion": RANGE, "regime_switch": RANGE,
}
LIB = [m for m in MODULES if m.id != "meta_muse"]


def entries(m, s, params=None):
    out = []
    for i in range(len(s)):
        e, _ = m.decide(s, i, params or {})
        if e is not None:
            out.append((i, e))
    return out


def test_registry_has_twenty_distinct_modules_with_complete_research_records():
    assert len(MODULES) == 20 and len({m.id for m in MODULES}) == 20
    for m in MODULES:
        spec = registry.get(m.id)
        assert spec is not None and spec.library and spec.status == registry.IMPLEMENTED
        assert m.entry_rules and m.exit_rules and m.description
        rr = m.research
        for k in ("family", "designed_for", "sources", "licence", "evidence", "limitations", "works_when",
                  "fails_when", "short_rule", "validation_tasks"):
            assert rr.get(k), (m.id, k)
        assert "not executed" in rr["short_rule"]
        assert IMPLEMENTATIONS.get(m.id) is not None
    assert {x["status"] for x in REJECTED} >= {"MERGED", "REJECTED", "BLOCKED"}


@pytest.mark.parametrize("m", LIB, ids=lambda m: m.id)
def test_every_module_enters_on_its_rule_with_a_stop_below_the_close_and_exits_on_its_rule(m):
    s = walk(SCENARIO[m.id], seed=7)
    got = entries(m, s)
    assert got, f"{m.id} never entered on its own scenario"
    p = m.p({})
    for i, e in got:
        assert 0 < e.stop < s.c[i], (m.id, i)
        assert e.reasons and e.values["close"] == round(s.c[i], 8)
        if e.target is not None:
            assert e.target > s.c[i]
    exits = [i for i in range(got[0][0] + 1, len(s)) if m.should_exit(s, i, {})]
    assert exits, f"{m.id}: the exit rule never fired after the first entry"
    assert p["atr_period"] == 14


def test_specific_entry_conditions_hold_where_the_modules_entered():
    s = walk(TREND, seed=7)
    f, sl = ind.sma(s.c, 20), ind.sma(s.c, 50)
    for i, _ in entries(BY_ID["sma_cross"], s):
        assert f[i] > sl[i] and f[i - 1] <= sl[i - 1]
    hh = ind.highest_prev(s.h, 20)
    for i, _ in entries(BY_ID["donchian_breakout"], s):
        assert s.c[i] > hh[i]
    x = ind.rsi(s.c, 14)
    for i, _ in entries(BY_ID["rsi_momentum"], s):
        assert x[i] > 55 >= x[i - 1]
    s2 = walk(SCENARIO["adx_trend"], seed=7)
    a, pdi, ndi = ind.adx(s2.h, s2.l, s2.c, 14)
    for i, _ in entries(BY_ID["adx_trend"], s2):
        assert pdi[i] > ndi[i] and pdi[i - 1] <= ndi[i - 1] and a[i] >= 25
    r = walk(SCENARIO["rsi_reversion"], seed=7)
    x2, t = ind.rsi(r.c, 2), ind.sma(r.c, 200)
    for i, _ in entries(BY_ID["rsi_reversion"], r):
        assert x2[i] < 10 and r.c[i] > t[i]


def test_no_trade_when_conditions_are_absent():
    """A perfectly flat market: no cross, no breakout, no momentum, and an ATR
    of zero, so no stop could be computed anyway: every module stays out."""
    n = 900
    flat = Series([100.0] * n, [100.0] * n, [100.0] * n, [100.0] * n, [100.0] * n)
    for m in LIB:
        assert entries(m, flat) == [], m.id
    e, why = BY_ID["sma_cross"].decide(flat, 10, {})
    assert e is None and "not enough closed bars" in why
    tiny = walk([(800, 0.0)], seed=3, vol=0.0005, volume_spikes=False)
    e, why = BY_ID["donchian_breakout"].decide(tiny, 799, {"min_volume_ratio": "5"})
    assert e is None  # the shared volume filter (5 x average) refuses a normal-volume bar


@pytest.mark.parametrize("m", LIB, ids=lambda m: m.id)
def test_no_module_can_see_the_future(m):
    """The decision at bar i on the full series equals the decision on the
    series cut at i (nothing after i exists)."""
    s = walk(SCENARIO[m.id], seed=11)
    idx = [i for i, _ in entries(m, s)][:5] + list(range(m.lookback(m.p({})) + 1, len(s), 97))
    for i in idx:
        cut = Series(s.o[:i + 1], s.h[:i + 1], s.l[:i + 1], s.c[:i + 1], s.v[:i + 1])
        a, why_a = m.decide(s, i, {})
        b, why_b = m.decide(cut, i, {})
        assert (a is None) == (b is None) and why_a == why_b, (m.id, i)
        if a:
            assert abs(a.stop - b.stop) < 1e-9 * max(1, a.stop)
        assert m.should_exit(s, i, {}) == m.should_exit(cut, i, {})


@pytest.mark.parametrize("fn", [
    lambda s: ind.sma(s.c, 20), lambda s: ind.ema(s.c, 20), lambda s: ind.rsi(s.c, 14),
    lambda s: ind.atr(s.h, s.l, s.c, 14), lambda s: ind.adx(s.h, s.l, s.c, 14)[0], lambda s: ind.macd(s.c)[1],
    lambda s: ind.roc(s.c, 10), lambda s: ind.highest_prev(s.h, 20), lambda s: ind.stdev(s.c, 20),
    lambda s: ind.obv(s.c, s.v), lambda s: ind.rolling_vwap(s.h, s.l, s.c, s.v, 24), lambda s: ind.swing_high_level(s.h, 5),
])
def test_indicators_are_causal(fn):
    s = walk(TREND, seed=5)
    full = fn(s)
    for i in (60, 200, 431, len(s) - 1):
        cut = Series(s.o[:i + 1], s.h[:i + 1], s.l[:i + 1], s.c[:i + 1], s.v[:i + 1])
        assert fn(cut)[i] == full[i]


def test_indicator_definitions():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert ind.highest_prev([1, 5, 3, 2], 2) == [None, None, 5, 5]  # excludes the current bar
    assert ind.rsi([float(x) for x in range(1, 30)], 14)[-1] == 100.0  # only gains
    lvl = ind.swing_high_level([1, 2, 5, 2, 1, 1, 1], 2)
    assert lvl[3] is None and lvl[4] == 5  # confirmed only after two more bars closed
    groups, done = ind.resample_last_closed([1, 2, 3, 4, 5, 6, 7], 3)
    assert groups == [3, 6] and done == [-1, -1, 0, 0, 0, 1, 1]


def candles(s: Series) -> list[Candle]:
    D = lambda x: Decimal(str(x))  # noqa: E731
    return [Candle(s.times[i], D(s.o[i]), D(s.h[i]), D(s.l[i]), D(s.c[i]), D(s.v[i]), "1h") for i in range(len(s))]


@pytest.mark.parametrize("mid", ["donchian_breakout", "trend_pullback", "bollinger_reversion", "mtf_trend"])
def test_the_live_adapter_decides_like_the_backtest_path(mid):
    m = BY_ID[mid]
    s = walk(SCENARIO[mid], seed=7)
    cs = candles(s)
    adapter = IMPLEMENTATIONS[mid]
    assert isinstance(adapter, LibraryAdapter)
    i = entries(m, s)[0][0]
    d = adapter.evaluate({"BTC/USDT": cs[:i + 1]}, {}, position_open=False)
    e, _ = m.decide(s, i, {})
    assert d.action == "ENTER" and d.side == "LONG" and abs(float(d.stop_price) - e.stop) < 1e-6
    held = adapter.evaluate({"BTC/USDT": cs[:i + 1]}, {}, position_open=True)
    assert held.action in ("HOLD", "EXIT")


def test_meta_muse_library_form_matches_the_live_rules():
    from yonixalpha_crypto.strategies.meta_muse import trend
    m = BY_ID["meta_muse"]
    lead = walk([(200, -0.003)], seed=2)
    traded = walk([(200, 0.003)], seed=4)
    traded.lead = lead
    e, why = m.decide(traded, 199, {})
    t1 = trend(lead.c[50:200], 9, 21, 0.0015)
    t2 = trend(traded.c[50:200], 9, 21, 0.0015)
    assert (e is not None) == (t1.strong and t2.strong and t1.direction == "down" and t2.direction == "up")
    if e:
        assert abs(e.stop - traded.c[199] * 0.99) < 1e-9


def test_sr_breakout_exit_uses_the_level_that_was_broken_not_a_newer_swing_high():
    # flat range (ATR about 2), one swing high at 105, a volume breakout above
    # it, a rally to a NEW confirmed swing high at 115, then two closes at 110:
    # below 115 - 0.5 ATR but well above the broken 105 - 0.5 ATR. Not a
    # failed breakout, so no exit; two closes below 104 are.
    rows = [(100, 101, 99, 100, 10)] * 30
    rows += [(100, 105, 99, 100, 10), (100, 101, 99, 100, 10), (100, 101, 99, 100, 10)]  # pivot 105, confirmed
    rows += [(100, 107, 105.5, 106.5, 40)]  # breakout bar: close 106.5 > 105 + 0.1 x ATR, volume 4x
    rows += [(106.5 + 2 * k, 108.5 + 2 * k, 106.5 + 2 * k, 108 + 2 * k, 10) for k in range(3)]  # rally to ~113
    rows += [(113, 115, 112, 113, 10), (112, 113, 111, 112, 10), (111, 112, 110, 111, 10)]  # new pivot 115
    rows += [(111, 111.5, 109.5, 110, 10), (110, 110.5, 109.5, 110, 10)]  # two closes at 110
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = Series(*[list(x) for x in zip(*rows)], [t0 + timedelta(hours=k) for k in range(len(rows))])
    m, p = BY_ID["sr_breakout"], {"pivot_n": 2}
    i = len(rows) - 1
    lvl = ind.swing_high_level(s.h, 2)
    assert lvl[i] == 115 and m.entry(s, 33, m.p(p))[0] is not None  # the newer pivot exists; the entry was real
    assert m.should_exit(s, i, p) is None
    s2 = Series(*[list(x) for x in zip(*(rows + [(110, 110, 103, 103.5, 10), (103.5, 104, 103, 103.2, 10)]))],
                [t0 + timedelta(hours=k) for k in range(len(rows) + 2)])
    assert "failed breakout" in (m.should_exit(s2, len(rows) + 1, p) or "")


@pytest.mark.parametrize("m", MODULES, ids=lambda m: m.id)
def test_backtest_defaults_equal_the_defaults_shown_and_saved(m):
    # A module's own default (e.g. sma_cross stop_atr 2.5, sr_breakout target_r 2)
    # must win over the shared default, in the backtester AND in the registry
    # the settings page and the live adapter read.
    spec = registry.get(m.id)
    used = m.p({})
    for k, (default, kind, _lo, _hi) in spec.params.items():
        if k in used and kind in ("int", "dec"):
            assert used[k] == pytest.approx(float(default)), k
