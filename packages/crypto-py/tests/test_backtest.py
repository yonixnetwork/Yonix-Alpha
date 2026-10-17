"""Backtest engine, metrics, validation and the job queue. All data here is
SYNTHETIC and only tests mechanics; nothing here is a performance result."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from yonixalpha_crypto.backtest import data, jobs, metrics, validation, worker
from yonixalpha_crypto.backtest.engine import Costs, RiskConfig, simulate
from yonixalpha_crypto.candles import Candle
from yonixalpha_crypto.db import BacktestBatch, BacktestRun, Ohlcv
from yonixalpha_crypto.strategies.library import BY_ID, Entry, Module, Series

from tests.test_library import walk

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Stub(Module):
    """Enters on the bars listed in `at`, with a fixed stop / target distance."""

    def __init__(self, at, stop_d=2.0, target_d=None, exit_at=()):
        super().__init__("stub", "stub", "test", "", "", "", {}, "1h", {})
        self.at, self.stop_d, self.target_d, self.exit_at = set(at), stop_d, target_d, set(exit_at)

    def lookback(self, p):
        return 0

    def entry(self, s, i, p):
        if i in self.at:
            c = s.c[i]
            return Entry(c - self.stop_d, c + self.target_d if self.target_d else None, ["stub"], {"close": c}), "e"
        return None, "no"

    def exit(self, s, i, p):
        return "stub exit" if i in self.exit_at else None


def bars(rows):
    """rows: (open, high, low, close[, volume])"""
    o, h, lo, c, v = [], [], [], [], []
    for r in rows:
        o.append(r[0]), h.append(r[1]), lo.append(r[2]), c.append(r[3]), v.append(r[4] if len(r) > 4 else 1e9)
    return Series(o, h, lo, c, v, [T0 + timedelta(hours=k) for k in range(len(c))])


ZERO = Costs(taker_fee_bps=0, maker_fee_bps=0, spread_bps=0, slippage_bps=0, min_notional=0)
RISK = RiskConfig(mode="FIXED", value=10, starting_equity=10_000)


def test_signal_fills_at_the_next_open_with_spread_and_slippage():
    s = bars([(100, 101, 99, 100)] * 3 + [(102, 103, 101, 102)] * 5)
    costs = Costs(taker_fee_bps=10, spread_bps=4, slippage_bps=5, min_notional=0)
    r = simulate(s, Stub([2]), {}, None, costs, RISK)
    t = r.trades[0]
    assert t.entry_i == 3 and t.entry_px == pytest.approx(102 * (1 + 0.0002 + 0.0005))
    assert t.end_of_data and t.exit_reason == "end of data"


def test_a_bar_touching_stop_and_target_counts_as_a_stop():
    s = bars([(100, 100, 100, 100)] * 2 + [(100, 100, 100, 100), (100, 120, 90, 100), (100, 100, 100, 100)])
    t = simulate(s, Stub([1], stop_d=5, target_d=5), {}, None, ZERO, RISK).trades[0]
    assert t.exit_reason == "stop" and t.exit_px == pytest.approx(95)


def test_a_gap_through_the_stop_fills_at_the_open():
    s = bars([(100, 100, 100, 100)] * 3 + [(90, 91, 89, 90), (90, 90, 90, 90)])
    t = simulate(s, Stub([1], stop_d=5), {}, None, ZERO, RISK).trades[0]
    assert t.exit_px == pytest.approx(90) and "gap" in t.exit_reason
    assert t.pnl < -10  # a gap loses more than the planned 10: shown, not hidden


def test_size_risks_the_budget_and_fees_are_charged_on_both_legs():
    s = bars([(100, 100, 100, 100)] * 3 + [(100, 100, 94, 95), (95, 95, 95, 95)])
    costs = Costs(taker_fee_bps=10, spread_bps=0, slippage_bps=0, min_notional=0)
    t = simulate(s, Stub([1], stop_d=5), {}, None, costs, RISK).trades[0]
    assert t.risk_quote == pytest.approx(10, rel=1e-9)
    assert -t.pnl == pytest.approx(t.risk_quote, rel=1e-6)  # the stop loses exactly the budget, fees included
    assert t.fees == pytest.approx(t.qty * 100 * 0.001 + t.qty * 95 * 0.001)


def test_volume_caps_the_fill_as_a_partial_fill():
    # the signal bar (1) traded 0.5; the fill bar (2) traded far more, but its
    # volume is not known when the order is placed, so it must not be used
    s = bars([(100, 100, 100, 100, 1e9), (100, 100, 100, 100, 0.5)] + [(100, 100, 100, 100, 1e9)] * 4)
    t = simulate(s, Stub([1], stop_d=1), {}, None, Costs(max_participation=0.1, min_notional=0), RISK).trades[0]
    assert t.partial_fill and t.qty == pytest.approx(0.05)


def test_the_fill_bar_volume_is_not_used_for_the_cap():
    s = bars([(100, 100, 100, 100, 1e9)] * 2 + [(100, 100, 100, 100, 0.5)] + [(100, 100, 100, 100, 1e9)] * 3)
    t = simulate(s, Stub([1], stop_d=1), {}, None, Costs(max_participation=0.1, min_notional=0), RISK).trades[0]
    assert not t.partial_fill and t.qty > 0.05


def test_the_entry_is_skipped_when_the_next_open_is_below_the_stop():
    s = bars([(100, 100, 100, 100)] * 2 + [(90, 90, 90, 90)] * 3)
    r = simulate(s, Stub([1], stop_d=5), {}, None, ZERO, RISK)
    assert r.trades == [] and r.skipped == {"next open at or below the stop": 1}


def test_module_exit_and_trailing_execute_at_the_next_open():
    s = bars([(100, 100, 100, 100)] * 2 + [(100 + k, 101 + k, 99.5 + k, 100 + k) for k in range(1, 12)])
    r = simulate(s, Stub([1], stop_d=2, exit_at=[8]), {}, {"trailing": {"method": "percent", "activate_r": 1,
                                                                       "percent": 1}}, ZERO, RISK)
    t = r.trades[0]
    assert t.exit_i == 9 and t.exit_reason == "stub exit" and t.exit_px == pytest.approx(s.o[9])


def test_simulation_never_reads_bars_after_the_one_it_is_on():
    s = walk([(400, 0.002), (300, -0.002), (300, 0.001)], seed=3)
    m = BY_ID["donchian_breakout"]
    full = simulate(s, m, {}, None, Costs(), RiskConfig())
    cut = Series(s.o[:700], s.h[:700], s.l[:700], s.c[:700], s.v[:700], s.times[:700])
    part = simulate(cut, m, {}, None, Costs(), RiskConfig())
    done = [t.as_dict() for t in full.trades if t.exit_i is not None and t.exit_i < 699]
    assert done and done == [t.as_dict() for t in part.trades if t.exit_i is not None and t.exit_i < 699]


def test_metrics_are_computed_from_the_trades_and_flag_small_samples():
    s = walk([(600, 0.001), (600, -0.001)], seed=4)
    r = simulate(s, BY_ID["sma_cross"], {}, None, Costs(), RiskConfig())
    m = metrics.compute(r, 10_000)
    n = m["trades"]
    assert n == len(r.trades) and m["insufficient_evidence"] == (n < metrics.MIN_TRADES)
    assert m["net_profit"] == pytest.approx(sum(t.pnl for t in r.trades), abs=1e-5)
    # cash accounting and trade accounting agree: equity change == sum of trade P&L
    assert r.equity[-1] - 10_000 == pytest.approx(sum(t.pnl for t in r.trades), abs=1e-6)
    pr = simulate(s, BY_ID["sr_breakout"], {}, BY_ID["sr_breakout"].default_exits, Costs(), RiskConfig())
    assert any(t.partials for t in pr.trades) or not pr.trades
    assert pr.equity[-1] - 10_000 == pytest.approx(sum(t.pnl for t in pr.trades), abs=1e-6)
    wins = [t.pnl for t in r.trades if t.pnl > 0]
    if wins and n:
        assert m["win_rate"] == round(len(wins) / n, 4)
    assert m["max_drawdown_pct"] >= 0 and m["sharpe"] is not None  # 50 days of equity
    tiny = simulate(bars([(100, 100, 100, 100)] * 30), Stub([]), {}, None, ZERO, RISK)
    mt = metrics.compute(tiny, 10_000)
    assert mt["trades"] == 0 and mt["win_rate"] is None and mt["sharpe"] is None and mt["ratio_unavailable_reason"]


def test_validation_is_chronological_and_reports_every_section():
    s = walk([(800, 0.0015), (800, -0.001), (800, 0.001)], seed=6)
    out = validation.study(s, BY_ID["donchian_breakout"], {}, None, Costs(), RiskConfig(), "1h")
    sp = out["splits"]
    assert sp["train"]["to"] < sp["validation"]["from"] <= sp["validation"]["to"] < sp["out_of_sample"]["from"]
    for k in ("default_params", "selected_on_train", "walk_forward", "sensitivity", "benchmark_buy_and_hold_oos", "verdict"):
        assert k in out
    assert "train" not in out["selected_on_train"]["metrics"]  # chosen on train, reported elsewhere
    assert len(out["walk_forward"]["folds"]) == validation.FOLDS
    assert out["verdict"]["tier"] in validation.TIERS and out["oos_curve"]["equity"]


def test_the_verdict_never_rewards_a_high_win_rate_with_negative_expectancy():
    oos = {"trades": 80, "expectancy_r": -0.1, "profit_factor": 0.8, "max_drawdown_pct": 5, "win_rate": 0.9}
    v = validation.verdict(oos, {"positive_folds": 4}, {"robust": True, "positive_points": 9, "points": 9})
    assert v["tier"] == "failed"
    v = validation.verdict({**oos, "trades": 10, "expectancy_r": 2}, {"positive_folds": 4},
                           {"robust": True, "positive_points": 9, "points": 9})
    assert v["tier"] == "insufficient evidence" and not v["approved_for_consideration"]


class FakeMD:
    exchange = "binance_us"

    def __init__(self, series_by_symbol):
        self.s = series_by_symbol
        self.calls = 0

    async def history(self, symbol, interval, start_ms, end_ms):
        self.calls += 1
        s = self.s[symbol]
        D = lambda x: Decimal(str(round(x, 8)))  # noqa: E731
        return [Candle(s.times[i], D(s.o[i]), D(s.h[i]), D(s.l[i]), D(s.c[i]), D(s.v[i]), "1h") for i in range(len(s))
                if start_ms <= s.times[i].timestamp() * 1000 < end_ms]

    async def aclose(self):
        pass


async def test_the_queue_runs_a_batch_records_provenance_and_survives_failures(sf):
    # The queue code is kept (it shares backtest.run.execute with the local
    # runner); only its server entry point is refused by policy.
    btc = walk([(700, 0.001), (700, -0.001)], seed=8)
    eth = walk([(700, -0.001), (700, 0.0012)], seed=9)
    eth.times = btc.times
    md = FakeMD({"BTC/USDT": btc, "ETH/USDT": eth})
    start, end = btc.times[0], btc.times[-1] + timedelta(hours=1)
    now = end + timedelta(days=1)
    async with sf() as s:
        b = await jobs.create_batch(s, strategies=["donchian_breakout", "rsi_reversion", "meta_muse"],
                                    symbols=["BTC/USDT", "ETH/USDT"], interval="1h", exchange="binance_us",
                                    start=start, end=end, costs={"taker_fee_bps": 2}, risk={"mode": "PERCENT", "value": 0.5},
                                    optimize=True, actor="test")
        lone = await jobs.create_batch(s, strategies=["meta_muse"], symbols=["BTC/USDT"], interval="1h",
                                       exchange="binance_us", start=start, end=end, costs={}, risk={}, optimize=False,
                                       actor="test")
        await s.commit()
    statuses = []
    while (st := await worker.run_one(sf, md_factory=lambda ex: md, now=now)) is not None:
        statuses.append(st)
    async with sf() as s:
        runs = (await s.execute(select(BacktestRun).where(BacktestRun.batch_id == b.id))).scalars().all()
        assert len(runs) == 5 and {r.status for r in runs} == {"DONE"}, [(r.strategy_id, r.status, r.error) for r in runs]
        for r in runs:
            assert r.code_version.startswith("src-") and r.data["bars"] == 1400 and r.summary["tier"]
            assert r.config["costs_effective"]["taker_fee_bps"] == 2
        mm = next(r for r in runs if r.strategy_id == "meta_muse")
        assert mm.symbol == "ETH/USDT" and mm.data["lead"]["symbol"] == "BTC/USDT"
        assert (await s.get(BacktestBatch, b.id)).status == "DONE"
        skipped = (await s.execute(select(BacktestRun).where(BacktestRun.batch_id == lone.id))).scalar_one()
        assert skipped.status == "SKIPPED" and "pair strategy" in skipped.error
        stored = (await s.execute(select(Ohlcv).limit(1))).scalar_one()
        assert stored.source and stored.exchange == "binance_us"
    assert md.calls == 2  # each symbol fetched once, then read from the store


async def test_cancel_and_stale_recovery(sf):
    start = T0
    async with sf() as s:
        b = await jobs.create_batch(s, strategies=["sma_cross", "ema_cross"], symbols=["BTC/USDT"], interval="1h",
                                    exchange="binance_us", start=start, end=start + timedelta(days=30), costs={},
                                    risk={}, optimize=False, actor="t")
        await s.commit()
        r = await jobs.claim(s)
        await s.commit()
        assert r.status == "RUNNING"
        assert await jobs.cancel_batch(s, b.id) == 1  # the queued one; the running one finishes its study
        r.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await s.commit()
        assert await jobs.recover_stale(s) == 1
        await s.commit()
        await s.refresh(r)
        assert r.status == "QUEUED"
        assert await jobs.claim(s) is None  # the batch was cancelled: nothing more is claimed
    with pytest.raises(jobs.BatchError):
        async with sf() as s:
            await jobs.create_batch(s, strategies=["nope"], symbols=["BTC/USDT"], interval="1h", exchange="binance_us",
                                    start=start, end=start + timedelta(days=1), costs={}, risk={}, optimize=False, actor="t")


async def test_the_server_worker_and_switch_refuse_by_policy(sf, capsys, monkeypatch):
    import os

    from yonixalpha_crypto import portfolio
    from yonixalpha_crypto.backtest import policy
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    assert policy.SERVER_BACKTESTS_ENABLED is False and policy.RESULT_IMPORT_ENABLED is False
    assert await worker.main_async(True, 1) == 2
    assert "run locally" in capsys.readouterr().out
    async with sf() as s:
        with pytest.raises(portfolio.PortfolioError, match="run locally"):
            await portfolio.save(s, {"backtests_enabled": True}, 1, "t")
        assert (await portfolio.save(s, {"backtests_enabled": False}, 1, "t"))["backtests_enabled"] is False


async def test_data_store_is_bounded_and_keeps_only_closed_candles(sf):
    s = walk([(50, 0.001)], seed=1)
    md = FakeMD({"BTC/USDT": s})
    now = s.times[-1] + timedelta(minutes=30)  # the last candle is still forming
    out = await data.ensure_history(sf, md, "binance_us", "BTC/USDT", "1h", s.times[0], now, "test", now)
    assert out["fetched"] == 49
    with pytest.raises(ValueError, match="limit"):
        await data.ensure_history(sf, md, "binance_us", "BTC/USDT", "1m", T0 - timedelta(days=60), T0, "test", T0)
    ser, prov = await data.load(sf, "binance_us", "BTC/USDT", "1h", s.times[0], now)
    assert len(ser) == 49 and prov["gaps"] == 0 and prov["sources"] == ["test"]
    assert uuid.UUID(int=0)  # keep uuid import used


@pytest.fixture(autouse=True)
def _fresh_fetch_memory():
    data._last_fetch.clear()
    yield
    data._last_fetch.clear()


async def test_a_range_the_exchange_has_gaps_in_is_not_downloaded_again_on_every_run(sf):
    full = walk([(100, 0.001)], seed=3)
    keep = [i for i in range(len(full)) if not 40 <= i < 50]  # the exchange itself is missing 10 candles
    gapped = Series(*[[getattr(full, f)[i] for i in keep] for f in ("o", "h", "l", "c", "v", "times")])
    md = FakeMD({"BTC/USDT": gapped})
    now = full.times[-1] + timedelta(hours=1)
    first = await data.ensure_history(sf, md, "binance_us", "BTC/USDT", "1h", full.times[0], now, "test", now)
    assert first["fetched"] == 90 and md.calls == 1
    again = await data.ensure_history(sf, md, "binance_us", "BTC/USDT", "1h", full.times[0], now, "test",
                                      now + timedelta(minutes=5))
    assert md.calls == 1 and again["fetched"] == 0 and "not fetched again" in again["note"]
    later = await data.ensure_history(sf, md, "binance_us", "BTC/USDT", "1h", full.times[0], now, "test",
                                      now + timedelta(hours=2))
    assert md.calls == 2 and later["already_stored"] == 90  # retried after the throttle window


def test_data_gaps_are_reported_and_lower_a_candidate():
    v = {"tier": "candidate", "reasons": ["ok"], "rank_key": [0, -0.2, 5.0], "approved_for_consideration": True}
    result = {"verdict": dict(v)}
    q = data.apply_data_quality(result, {"bars": 900, "gaps": 3}, 1000)
    assert q["coverage_pct"] == 90.0 and result["data_quality"]["gaps"] == 3
    assert result["verdict"]["tier"] == "weak" and result["verdict"]["rank_key"][0] == 1
    assert not result["verdict"]["approved_for_consideration"]
    assert any("3 gap(s)" in r for r in result["verdict"]["reasons"])
    ok = {"verdict": dict(v)}
    data.apply_data_quality(ok, {"bars": 995, "gaps": 1}, 1000)  # 99.5%: noted, tier kept
    assert ok["verdict"]["tier"] == "candidate" and any("gap" in r for r in ok["verdict"]["reasons"])
