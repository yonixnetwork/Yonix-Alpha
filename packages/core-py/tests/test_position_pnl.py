"""Real-time PnL view (master §59-60)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from yonixalpha_core import position_pnl
from yonixalpha_core.db.models import PaperPosition

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _pos(**kw) -> PaperPosition:
    base = dict(symbol="T", provider="paper", side="LONG", entry_price=D("0.001"), quantity=D("1000"),
                initial_quantity=D("1000"), remaining_quantity=D("1000"), entry_cost_quote=D("1.01"),
                proceeds_quote=None, fees_paid_quote=D("0.01"), status="open", engine="evm_bsc",
                highest_price=D("0.0015"), lowest_price=D("0.0009"), last_price=D("0.0012"),
                last_marked_at=NOW - timedelta(seconds=5), entry_at=NOW - timedelta(minutes=3), plan={})
    base.update(kw)
    return PaperPosition(**base)


def test_an_open_position_in_profit_shows_profit_with_every_field():
    v = position_pnl.view(_pos(), NOW)
    assert (v["outcome"], v["tone"], v["price_status"]) == ("PROFIT", "positive", "LIVE")
    assert v["value"] == "1.2" and v["unrealized"] == "0.19" and v["realized"] == "0" and v["net"] == "0.19"
    assert v["net_pct"] == "18.81" and v["fees"] == "0.01"
    assert v["peak_pct"] == "50.00" and v["drawdown_pct"] == "-20.00"
    assert "executable sell quote" in v["basis"]


def test_a_partial_take_profit_counts_as_realized_and_the_rest_as_unrealized():
    v = position_pnl.view(_pos(remaining_quantity=D("500"), proceeds_quote=D("0.75"), last_price=D("0.0008")), NOW)
    # sold half: 0.75 - 0.505 = 0.245 realized; rest: 0.4 - 0.505 = -0.105 unrealized
    assert v["realized"] == "0.245" and v["unrealized"] == "-0.105" and v["net"] == "0.14"
    assert v["outcome"] == "PROFIT" and v["unrealized_pct"] == "-20.79"


def test_a_losing_position_is_a_loss_and_a_missing_mark_is_unavailable_not_zero():
    v = position_pnl.view(_pos(last_price=D("0.0005"), engine="solana_fresh"), NOW)
    assert (v["outcome"], v["tone"]) == ("LOSS", "negative") and v["net"].startswith("-")
    assert "exit costs not deducted" in v["basis"]
    v = position_pnl.view(_pos(last_price=None, last_marked_at=None), NOW)
    assert (v["outcome"], v["net"], v["price_status"]) == ("PNL_UNAVAILABLE", None, "UNAVAILABLE")
    assert v["reason"] == "no price mark yet"


def test_a_stale_mark_still_computes_but_says_stale():
    v = position_pnl.view(_pos(last_marked_at=NOW - timedelta(minutes=10)), NOW)
    assert v["price_status"] == "STALE" and v["outcome"] == "PROFIT"


def test_a_closed_position_shows_its_realized_result():
    v = position_pnl.view(_pos(status="closed", exit_price=D("0.0009"), realized_pnl=D("-0.12"), remaining_quantity=D(0)), NOW)
    assert (v["outcome"], v["net"], v["current"], v["quantity"], v["price_status"]) == ("LOSS", "-0.12", "0.0009", "1000", "CLOSED")
    assert v["net_pct"] == "-11.88"
    v = position_pnl.view(_pos(status="closed", realized_pnl=D(0), remaining_quantity=D(0)), NOW)
    assert (v["outcome"], v["tone"]) == ("BREAKEVEN", "neutral")


def test_a_short_peaks_at_its_lowest_price():
    v = position_pnl.view(_pos(side="SHORT", engine="binance_futures", last_price=D("0.00095")), NOW)
    assert v["peak_pct"] == "10.00" and v["drawdown_pct"] == "-5.56"
