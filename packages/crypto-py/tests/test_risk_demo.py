"""Isolated risk validation (quote-denominated) and the demo fill model."""

from datetime import timedelta
from decimal import Decimal

from yonixalpha_crypto import demo
from yonixalpha_crypto.risk import Exposure, RiskLimits, size_entry, validate_risk

from tests.conftest import NOW, book, spec

FLAT = Exposure(0, Decimal(0), Decimal(0), Decimal(1000))


def test_size_keeps_the_loss_at_the_stop_within_the_risk_budget():
    lim = RiskLimits.from_dict({"risk_per_trade_quote": "5", "max_position_quote": "1000"})
    d = size_entry(lim, FLAT, book("99.95", "100.05"), spec(), Decimal("99"), Decimal(10), NOW)
    assert d.approved and d.qty > 0
    entry = Decimal("100.05") * Decimal("1.0005")
    exit_ = Decimal(99) * Decimal("0.9995")
    loss = d.qty * (entry - exit_) + d.qty * (entry + exit_) * Decimal("0.001")
    assert loss <= Decimal(5) and d.planned_loss_quote <= Decimal(5)
    assert d.qty == spec().round_qty(d.qty)  # precision respected (rounded down)


def test_position_cap_and_cash_cap():
    lim = RiskLimits.from_dict({"risk_per_trade_quote": "500", "max_position_quote": "50"})
    d = size_entry(lim, FLAT, book("99.95", "100.05"), spec(), Decimal("99"), Decimal(10), NOW)
    assert d.approved and d.quote_amount <= Decimal(50)
    poor = Exposure(0, Decimal(0), Decimal(0), Decimal(10))
    d = size_entry(lim, poor, book("99.95", "100.05"), spec(), Decimal("99"), Decimal(10), NOW)
    assert d.quote_amount <= Decimal(10)


def test_every_unsafe_condition_rejects():
    lim = RiskLimits.from_dict({})
    b = book("99.95", "100.05")
    cases = {
        "order book is": dict(book_=book("99.95", "100.05", at=NOW - timedelta(seconds=60))),
        "spread": dict(book_=book("90", "110")),
        "open position": dict(expo=Exposure(1, Decimal(50), Decimal(0), Decimal(950))),
        "daily loss": dict(expo=Exposure(0, Decimal(0), Decimal(-15), Decimal(985))),
        "stop is not below": dict(stop=Decimal("101")),
        "below the minimum": dict(inst=spec(min_notional="100000")),
        "not trading": dict(inst=spec().__class__(**{**spec().__dict__, "tradable": False, "status": "Halt"})),
        "emergency": dict(blocked="emergency stop"),
    }
    for want, kw in cases.items():
        d = size_entry(lim, kw.get("expo", FLAT), kw.get("book_", b), kw.get("inst", spec()), kw.get("stop", Decimal(99)),
                       Decimal(10), NOW, blocked=kw.get("blocked"))
        assert not d.approved and any(want in r for r in d.reasons), (want, d.reasons)


def test_risk_settings_validation():
    ok, err = validate_risk({"risk_per_trade_quote": "2.5", "max_open_positions": "2"})
    assert ok == {"risk_per_trade_quote": "2.5", "max_open_positions": "2"} and err == []
    _, err = validate_risk({"risk_per_trade_quote": "-1", "max_open_positions": "1.5", "leverage": "10"})
    assert len(err) == 3


def test_fill_model_buys_at_ask_sells_at_bid_with_slippage_fee_and_rounding():
    s = spec(tick="0.1")
    f, why = demo.simulate("BUY", book("99.9", "100.0"), s, Decimal("0.5"), Decimal(10), Decimal(5), Decimal(10))
    assert why is None and f.price == Decimal("100.1")  # 100.05 rounded up to the tick
    assert f.fee_quote == f.quote_amount * Decimal("0.001")
    f, _ = demo.simulate("SELL", book("99.9", "100.0"), s, Decimal("0.5"), Decimal(10), Decimal(5), None)
    assert f.price == Decimal("99.8")  # 99.85 rounded down
    f, why = demo.simulate("BUY", book("99.9", "100.0"), s, Decimal("20"), Decimal(10), Decimal(5), Decimal(10))
    assert f is None and "partial fills not simulated" in why
