"""Meta Muse rules against hand-checked numbers and constructed trends."""

from datetime import timedelta
from decimal import Decimal

import pytest

from yonixalpha_crypto import config_store, registry
from yonixalpha_crypto.candles import clean
from yonixalpha_crypto.strategies import implementation
from yonixalpha_crypto.exchanges.profiles import PROFILES
from yonixalpha_crypto.strategies.meta_muse import ema, signal, trend

from tests.conftest import NOW, candles

MM = implementation("meta_muse")
P = registry.get("meta_muse").default_params()


def series(start, step, n=150):
    return [str(round(start * (1 + step) ** i, 6)) for i in range(n)]


def test_ema_matches_pandas_adjust_false():
    # pandas: pd.Series([1, 2, 3]).ewm(span=2, adjust=False).mean() -> 1.0, 1.666667, 2.555556
    out = ema([1, 2, 3], 2)
    assert [round(x, 6) for x in out] == [1.0, 1.666667, 2.555556]


def test_trend_strength_uses_the_relative_ema_gap():
    up = [float(x) for x in series(100, 0.002)]
    t = trend(up, 9, 21, 0.0015)
    assert t.direction == "up" and t.strong and t.separation > 0.0015
    flat = [100.0] * 150
    t = trend(flat, 9, 21, 0.0015)
    assert not t.strong and t.direction == "down"  # fast == slow reads "down" in the original too


@pytest.mark.parametrize("d1,d2,want", [("up", "down", "SHORT"), ("down", "up", "LONG"), ("up", "up", None),
                                        ("down", "down", None)])
def test_divergence_rules(d1, d2, want):
    from yonixalpha_crypto.strategies.meta_muse import Trend

    assert signal(Trend(d1, True, 0.01, 0, 0), Trend(d2, True, 0.01, 0, 0)) == want
    assert signal(Trend(d1, False, 0.0001, 0, 0), Trend(d2, True, 0.01, 0, 0)) is None


def data(btc_step, eth_step):
    return {"BTC/USDT": candles(series(60000, btc_step)), "ETH/USDT": candles(series(3000, eth_step))}


def test_btc_down_eth_up_enters_long_eth_with_stop_and_target_percentages():
    d = MM.evaluate(data(-0.002, 0.002), P, position_open=False)
    assert (d.side, d.action) == ("LONG", "ENTER")
    assert d.detail["stop_pct"] == "0.01" and d.detail["target_pct"] == "0.02"
    last = Decimal(series(3000, 0.002)[-1])
    assert d.stop_price == last * Decimal("0.99") and d.target_price == last * Decimal("1.02")


def test_btc_up_eth_down_is_a_short_signal():
    d = MM.evaluate(data(0.002, -0.002), P, position_open=False)
    assert (d.side, d.action) == ("SHORT", "ENTER")  # the runner records it; spot cannot simulate it


def test_in_a_position_it_exits_when_a_trend_weakens_or_the_signal_flips():
    assert MM.evaluate(data(-0.002, 0.002), P, position_open=True).action == "HOLD"
    flat_btc = {"BTC/USDT": candles(["60000"] * 150), "ETH/USDT": candles(series(3000, 0.002))}
    d = MM.evaluate(flat_btc, P, position_open=True)
    assert d.action == "EXIT" and d.reasons == ["trend weakened"]
    d = MM.evaluate(data(0.002, -0.002), P, position_open=True)
    assert d.action == "EXIT" and d.reasons == ["signal no longer LONG"]


def test_only_closed_candles_reach_the_strategy():
    raw = candles(series(3000, 0.002, 151))
    forming = raw[-1].__class__(raw[-1].open_time + timedelta(minutes=5), *[Decimal("999999")] * 5, "5m")
    cs = clean(raw + [forming], "5m", NOW)
    assert cs.dropped_forming == 1 and Decimal("999999") not in [c.close for c in cs.candles]


def test_defaults_are_the_originals_and_validation_holds():
    assert P == {"fast": 9, "slow": 21, "trend_threshold": "0.0015", "stop_pct": "0.01", "target_pct": "0.02",
                 "candles": 150}
    assert MM.required_candles(P) == 150 and MM.traded_symbol(["BTC/USDT", "ETH/USDT"]) == "ETH/USDT"
    spec = registry.get("meta_muse")
    base = {"enabled": False, "mode": "DISABLED", "exchange": "bybit", "symbols": ["BTC/USDT", "ETH/USDT"],
            "interval": "5m", "params": P, "risk": {}}
    for bad in ({"symbols": ["BTC/USDT"]}, {"params": {"fast": 30}}, {"params": {"candles": 30}},
                {"exchange": "hyperliquid"}, {"interval": "1m"}):
        with pytest.raises(config_store.ConfigError):
            config_store.validate(spec, base, bad)
    ok = config_store.validate(spec, base, {"enabled": True, "mode": "DEMO", "exchange": "binance_us"})
    assert ok["enabled"] and ok["exchange"] == "binance_us"


def test_default_exchange_is_the_one_reachable_from_the_server():
    # 2026-10-15 server checks: Bybit HTTP 403, binance.com HTTP 451, Binance.US HTTP 200.
    assert registry.get("meta_muse").default_exchange == "binance_us"
    assert "403" in PROFILES["bybit"].note and "451" in PROFILES["binance"].note
