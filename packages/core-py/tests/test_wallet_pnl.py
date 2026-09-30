"""Wallet P/L (master upgrade §19-27): FIFO cost basis, wins and losses
shown separately, profit factor, drawdown, the outlier test, windows that
never pretend to cover more history than exists, and no profit from
tokens that were never bought."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from yonixalpha_core.wallet_pnl import TradeIn, fifo, max_drawdown, profile, stats

T = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def buy(tok, h, qty, quote):
    return TradeIn(tok, T + h * H, True, D(qty), D(quote))


def sell(tok, h, qty, quote):
    return TradeIn(tok, T + h * H, False, D(qty), D(quote))


def test_fifo_matches_oldest_lots_first_and_never_invents_a_cost_basis():
    led = fifo([buy("A", 0, 100, 1), buy("A", 1, 100, 2), sell("A", 2, 150, 3), sell("B", 3, 10, 5)])
    (a,) = led.closed
    # 100 @0.01 + 50 @0.02 = 2.0 cost for 150 tokens sold for 3.0
    assert a.cost == D(2) and a.proceeds == D(3) and a.pnl == D(1) and a.opened_at == T
    assert led.open_tokens == 1  # 50 A still held
    assert led.unknown_basis_sells == 1 and led.unknown_basis_proceeds == D(5)  # B never bought: not profit


def test_wins_and_losses_are_reported_separately_with_profit_factor():
    trades = []
    for i, (cost, back) in enumerate([(1, 3), (1, 2), (1, D("0.5")), (1, D("0.2")), (2, 3)]):
        trades += [buy(f"T{i}", i, 100, cost), sell(f"T{i}", i + 0.5, 100, back)]
    s = stats(fifo(trades).closed)
    assert s["status"] == "OK" and (s["winning_trades"], s["losing_trades"]) == (3, 2)
    assert s["usually_earns"]["avg"] == "1.333333333" and s["usually_earns"]["median"] == "1.000000000"
    assert s["usually_earns"]["median_pct"] == 100.0
    assert s["usually_loses"]["avg"] == "-0.650000000" and s["usually_loses"]["avg_pct"] == -65.0
    assert s["profit_factor"] == round(4 / 1.3, 3) and s["win_rate"] == 0.6
    assert s["outliers"]["dependence"].startswith("MEDIUM")  # +2.7 total, +0.7 without the best, -0.3 without top 3


def test_one_lucky_trade_is_exposed_by_the_outlier_test():
    trades = [buy("L", 0, 1, 1), sell("L", 1, 1, 30)]
    for i in range(6):
        trades += [buy(f"X{i}", i, 1, 1), sell(f"X{i}", i + 1, 1, D("0.5"))]
    o = stats(fifo(trades).closed)["outliers"]
    assert o["dependence"].startswith("HIGH") and o["total_pnl"] == "26.000000000" and o["without_best"] == "-3.000000000"


def test_drawdown_follows_exit_order():
    trades = [buy("A", 0, 1, 1), sell("A", 1, 1, 3), buy("B", 1, 1, 1), sell("B", 2, 1, D("0.2")),
              buy("C", 2, 1, 1), sell("C", 3, 1, D("0.5")), buy("D", 3, 1, 1), sell("D", 4, 1, 4)]
    assert max_drawdown(fifo(trades).closed) == D("1.3")


def test_missing_data_is_named_never_shown_as_zero():
    empty = stats([])
    assert empty["status"] == "INSUFFICIENT_DATA" and empty["win_rate"] is None and empty["realized_pnl"] is None
    assert empty["usually_earns"] is None and "no closed trades" in empty["reasons"][0]
    few = stats(fifo([buy("A", 0, 1, 1), sell("A", 1, 1, 2)]).closed)
    assert few["status"] == "INSUFFICIENT_DATA" and few["win_rate"] == 1.0 and "only 1 closed" in few["reasons"][0]
    p = profile([buy("A", 0, 1, 1), sell("A", 1, 1, 2), buy("Z", 1, 5, 1)], T + 2 * H, history_days=7)
    assert p["windows"]["30D"]["status"] == "INSUFFICIENT_DATA" and "7 days" in p["windows"]["30D"]["reasons"][0]
    assert p["windows"]["24H"]["closed_trades"] == 1 and p["unrealized_pnl"] is None
    assert any("still held" in n for n in p["notes"]) and any("gas" in n for n in p["notes"])
