"""Strategy ports: indicator parity with the reference repos' pandas formulas,
and each strategy's rules on constructed scenarios."""

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from yonixalpha_core.solana.flow import Trade, early_buy_share, round_trip_volume_share, synchronized_buy_cluster
from yonixalpha_core.strategies import confluence, gold_btc, grid, meta_muse
from yonixalpha_core.strategies.indicators import atr, ema, rolling_std, rsi
from yonixalpha_core.strategies.solana import momentum_signal
from yonixalpha_core.venues.common import Candle

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def candles(closes, step=timedelta(minutes=5), spread=0.001, last_open=False, highs=None, lows=None):
    out = []
    t0 = NOW - step * len(closes)
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * (1 + spread)
        lo = lows[i] if lows else c * (1 - spread)
        out.append(Candle(t0 + step * i, Decimal(str(c)), Decimal(str(h)), Decimal(str(lo)), Decimal(str(c)), Decimal(1),
                          closed=not (last_open and i == len(closes) - 1)))
    return out


# --- indicators -------------------------------------------------------------------

def test_ema_first_value_is_seed_and_converges():
    e = ema([10, 10, 10, 20], 3)
    assert e[:3] == [10, 10, 10] and e[3] == 15.0


def test_indicators_match_pandas_reference():
    pd = pytest.importorskip("pandas")
    import random

    random.seed(7)
    closes = [100.0]
    for _ in range(200):
        closes.append(closes[-1] * (1 + random.uniform(-0.01, 0.01)))
    highs = [c * 1.004 for c in closes]
    lows = [c * 0.996 for c in closes]
    s = pd.Series(closes)
    assert ema(closes, 21)[-1] == pytest.approx(s.ewm(span=21, adjust=False).mean().iloc[-1])
    delta = s.diff()
    gain, loss = delta.where(delta > 0, 0.0), -delta.where(delta < 0, 0.0)
    ag, al = gain.ewm(alpha=1 / 14, adjust=False).mean(), loss.ewm(alpha=1 / 14, adjust=False).mean()
    ref_rsi = (100 - 100 / (1 + ag / al.replace(0, float("nan")))).fillna(50.0)
    assert rsi(closes)[-1] == pytest.approx(ref_rsi.iloc[-1])
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    tr = pd.concat([df.high - df.low, (df.high - df.close.shift()).abs(), (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    assert atr(highs, lows, closes)[-1] == pytest.approx(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    assert rolling_std(closes, 20)[-1] == pytest.approx(s.rolling(20).std().iloc[-1])


def test_rsi_with_no_losses_reads_50_like_pandas():
    assert rsi([1, 2, 3, 4, 5])[-1] == 50.0


# --- Meta Muse ----------------------------------------------------------------------

def trending(start, pct, n=80):
    return [start * (1 + pct) ** i for i in range(n)]


def test_meta_muse_long_when_btc_down_strong_and_eth_up_strong():
    sig = meta_muse.evaluate(candles(trending(60000, -0.002)), candles(trending(3000, 0.002)))
    assert sig.side == "LONG" and sig.trend1.direction == "down" and sig.trend2.direction == "up"


def test_meta_muse_short_on_the_mirror_case():
    assert meta_muse.evaluate(candles(trending(60000, 0.002)), candles(trending(3000, -0.002))).side == "SHORT"


def test_meta_muse_no_signal_when_same_direction_or_weak():
    assert meta_muse.evaluate(candles(trending(60000, 0.002)), candles(trending(3000, 0.002))).side is None
    assert meta_muse.evaluate(candles(trending(60000, -0.00001)), candles(trending(3000, 0.002))).side is None


def test_meta_muse_ignores_the_forming_candle():
    closes_eth = trending(3000, 0.002)
    base = meta_muse.evaluate(candles(trending(60000, -0.002)), candles(closes_eth))
    spiked = closes_eth[:-1] + [closes_eth[-1] * 0.5]
    with_open = meta_muse.evaluate(candles(trending(60000, -0.002) + [1]), candles(closes_eth + [1], last_open=True))
    assert with_open.side == base.side
    assert meta_muse.evaluate(candles(trending(60000, -0.002)), candles(spiked)).trend2.fast != base.trend2.fast


def test_meta_muse_exit_rule_matches_repository():
    sig = meta_muse.evaluate(candles(trending(60000, -0.002)), candles(trending(3000, 0.002)))
    assert meta_muse.should_exit("LONG", sig) == (False, "divergence intact")
    assert meta_muse.should_exit("SHORT", sig)[0]
    weak = meta_muse.evaluate(candles(trending(60000, -0.00001)), candles(trending(3000, 0.002)))
    assert meta_muse.should_exit("LONG", weak) == (True, "trend weakened")


# --- Confluence -----------------------------------------------------------------------

def zigzag_breakout():
    """Oscillation that forms a clear pivot high, then a strong breakout."""
    closes = []
    for cycle in range(6):
        closes += [100 + i for i in range(13)] + [111 - i for i in range(13)]
    closes += [101 + i * 0.5 for i in range(20)] + [113]  # break above ~112
    return closes


def test_confluence_long_breakout_with_pivot_based_levels():
    closes = zigzag_breakout()
    sig = confluence.evaluate(candles(closes, step=timedelta(minutes=15), spread=0.0005))
    assert sig.side == "LONG", sig.reason
    assert sig.stop < sig.pivot_low < sig.pivot_high < sig.entry < sig.tp1 < sig.tp2
    wave = sig.pivot_high - sig.pivot_low
    assert sig.tp1 == pytest.approx(sig.pivot_high + wave * 0.618)
    assert 0 <= sig.score <= 100 and sig.score % 5 == 0
    lv = confluence.levels(sig)
    assert lv.move_stop_to_breakeven_at_tp1 and len(lv.take_profits) == 2


def test_confluence_v2_filters_on_score():
    sig = confluence.evaluate(candles(zigzag_breakout(), step=timedelta(minutes=15)), {"mode": "V2", "threshold": 101})
    assert sig.side is None and "threshold" in sig.reason


def test_confluence_pivot_is_used_only_after_confirmation():
    highs = [1.0] * 30
    highs[10] = 5.0
    piv = confluence.pivot_levels(highs, 12, True)
    assert piv[10] is None  # needs 12 bars on the left
    highs[10] = 1.0
    highs[15] = 5.0
    piv = confluence.pivot_levels(highs, 12, True)
    conf = confluence.last_confirmed(piv, 12)
    assert piv[15] == 5.0 and conf[26] is None and conf[27] == 5.0


def test_confluence_no_signal_without_break():
    flat = [100.0 + math.sin(i / 3) for i in range(120)]
    assert confluence.evaluate(candles(flat, step=timedelta(minutes=15))).side is None


# --- Grid -------------------------------------------------------------------------------

def test_grid_build_matches_repository_math():
    s = grid.build({"grid_levels": 4, "range_pct": "1", "capital": "100", "leverage": "1"}, Decimal(100), Decimal(100), Decimal(5))
    assert s.levels == [Decimal(99), Decimal("99.5"), Decimal(100), Decimal("100.5"), Decimal(101)]
    assert Decimal(100) not in [o.price for o in s.orders]  # level at mid skipped
    assert all(o.is_buy for o in s.orders if o.price < 100) and s.size_per_level == Decimal(25) / Decimal(100)


def test_grid_round_trip_books_spacing_profit_minus_fees():
    params = {"grid_levels": 4, "range_pct": "1", "capital": "100", "leverage": "1", "maker_fee_bps": "0"}
    s = grid.build(params, Decimal(100), Decimal(100), Decimal(5))
    ev = grid.step(s, Decimal("99.4"), params)  # buy at 99.5 fills
    assert [e["side"] for e in ev] == ["BUY"] and s.net_position == s.size_per_level
    grid.step(s, Decimal("100.1"), params)  # replacement sell at 100 fills
    assert s.net_position == 0 and s.realized_pnl == Decimal("0.5") * s.size_per_level


def test_grid_worst_case_loss_is_bounded_and_positive():
    params = {"grid_levels": 6, "range_pct": "2", "capital": "100"}
    s = grid.build(params, Decimal(100), Decimal(100), Decimal(1))
    loss = grid.worst_case_loss(s, params)
    assert 0 < loss < Decimal(100)


def test_grid_range_break_pauses_and_flattens():
    params = {"grid_levels": 4, "range_pct": "1", "capital": "100", "range_break_pct": "1"}
    s = grid.build(params, Decimal(100), Decimal(100), Decimal(1))
    ev = grid.step(s, Decimal(97), params)
    assert s.paused == "pause_range_break" and s.net_position == 0 and s.orders == []
    assert any(e["type"] == "flatten" for e in ev)
    assert grid.step(s, Decimal(90), params) == []  # paused grids don't trade
    restored = grid.from_json(s.to_json())
    assert restored.paused == s.paused and restored.realized_pnl == s.realized_pnl


# --- Gold vs BTC -------------------------------------------------------------------------

def test_gold_btc_ratio_and_zscore():
    btc = candles([60000 + i * 10 for i in range(50)], step=timedelta(hours=1))
    gold = candles([2000.0] * 50, step=timedelta(hours=1))
    a = gold_btc.analyse(btc, gold, {"z_window": 20, "corr_window": 20})
    assert a.ratio == pytest.approx((60000 + 49 * 10) / 2000)
    assert a.zscore is not None and a.zscore > 1 and a.corr is None  # gold flat: correlation undefined
    assert a.btc_change_pct > 0 and a.gold_change_pct == 0


# --- Solana momentum and wallet indicators -------------------------------------------------

def t(sec, trader, buy, sol=1_000_000_000, vs=40_000_000_000, vt=800_000_000_000_000, tok=10**12):
    return Trade(NOW - timedelta(seconds=sec), trader, buy, sol, tok, vs, vt)


def test_momentum_requires_every_axis_not_just_volume():
    prev = [t(590 - i * 30, f"p{i % 3}", i % 2 == 0, vs=40_000_000_000 + i) for i in range(6)]
    cur = [t(290 - i * 10, f"c{i}", True, vs=41_000_000_000 + i * 10**8) for i in range(20)]
    sig = momentum_signal(prev + cur, NOW, 300, 6)
    assert sig.qualified, sig.reasons
    # Same volume spike but all from one wallet, price falling: not momentum.
    dump = [t(290 - i * 10, "whale", i % 2 == 0, vs=41_000_000_000 - i * 10**8) for i in range(20)]
    assert not momentum_signal(prev + dump, NOW, 300, 6).qualified


def test_wallet_indicators():
    created = NOW - timedelta(minutes=10)
    trades = [Trade(created + timedelta(seconds=5), f"s{i}", True, 10**9, 50 * 10**12, 1, 1) for i in range(4)]
    assert early_buy_share(trades, created, 10**15) == Decimal("0.2")
    assert early_buy_share(trades, None, 10**15) is None
    sync = [Trade(NOW - timedelta(seconds=30), f"w{i}", True, 1_000_000_000 + i * 1_000_000, 1, 1, 1) for i in range(5)]
    assert synchronized_buy_cluster(sync, NOW, 300) == 5
    rt = [t(100, "a", True), t(90, "a", False), t(80, "b", True)]
    assert round_trip_volume_share(rt, NOW, 300) == Decimal(2) / Decimal(3)


def test_gold_btc_trend_uses_per_asset_thresholds_and_repo_exit_rules():
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from yonixalpha_core.strategies import gold_btc_trend
    from yonixalpha_core.venues.common import Candle

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def s(start, pct, n=80):
        return [Candle(t0 + timedelta(minutes=15 * i), *(Decimal(str(start * (1 + pct) ** i)),) * 4, Decimal(1), True)
                for i in range(n)]

    # Gold creeping up 0.002%/bar is STRONG for gold (0.005% gap) though it would be
    # flat by BTC's 0.03% threshold; BTC falling 0.1%/bar is strong -> SHORT BTC.
    sig = gold_btc_trend.evaluate(s(2000, 0.00002), s(60000, -0.001))
    assert sig.trend1.strong and sig.trend2.strong and sig.side == "SHORT"
    assert gold_btc_trend.should_exit("SHORT", sig) == (False, "divergence intact")
    assert gold_btc_trend.should_exit("LONG", sig) == (True, "signal reversal")
    same = gold_btc_trend.evaluate(s(2000, 0.00002), s(60000, 0.001))
    assert same.side is None and gold_btc_trend.should_exit("SHORT", same) == (True, "inverse correlation broke")
    weak = gold_btc_trend.evaluate(s(2000, 0.0000001), s(60000, -0.001))
    assert gold_btc_trend.should_exit("SHORT", weak) == (True, "trend weakened")
    assert gold_btc_trend.as_strategy_signal(sig).name == "gold_btc_trend"
