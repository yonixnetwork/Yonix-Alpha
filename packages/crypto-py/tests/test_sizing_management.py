"""Risk-based sizing and the pure trade-management decisions."""

from decimal import Decimal

import pytest

from yonixalpha_crypto.exchanges.base import InstrumentSpec
from yonixalpha_crypto.private import management as mg
from yonixalpha_crypto.private.sizing import SizingRules, size_long

SPEC = InstrumentSpec("binance", "BTC/USDT", "BTCUSDT", "BTC", "USDT", "TRADING", True, Decimal("0.01"),
                      Decimal("0.00001"), Decimal("0.00001"), Decimal("5"))
RULES = SizingRules(Decimal(1), Decimal(10), Decimal(500), Decimal(10), Decimal(5), Decimal(20))


def size(**kw):
    args = dict(equity_quote=Decimal(1000), free_quote=Decimal(1000), entry_price=Decimal("100"), stop_price=Decimal("98"))
    args.update(kw)
    return size_long(kw.pop("rules", RULES), SPEC, **{k: v for k, v in args.items() if k != "rules"})


def test_size_comes_from_equity_risk_and_stop_distance_after_costs():
    z = size()
    assert z.approved and z.budget == Decimal(10)
    assert z.planned_loss <= z.budget and z.planned_loss > Decimal("9.99")
    assert z.qty == (z.qty / SPEC.qty_step).to_integral_value() * SPEC.qty_step  # on the exchange step
    assert z.stop == Decimal("98") and z.stop_limit == Decimal("97.80")


def test_a_wider_stop_gives_a_smaller_size_never_a_larger_one():
    assert size(stop_price=Decimal("95")).qty < size(stop_price=Decimal("98")).qty


def test_caps_by_position_and_free_balance():
    z = size(stop_price=Decimal("99.5"))
    assert z.approved and z.notional <= Decimal(500) and "maximum position" in z.reasons[0]
    z = size(free_quote=Decimal(50))
    assert z.approved and z.notional <= Decimal(50)


@pytest.mark.parametrize("kw,why", [
    ({"stop_price": Decimal("100")}, "two ticks"),
    ({"stop_price": Decimal("99.99")}, "two ticks"),
    ({"stop_price": Decimal("-1")}, "positive"),
    ({"equity_quote": Decimal(0)}, "no equity"),
    ({"free_quote": Decimal(4)}, "below the minimum"),
    ({"entry_price": Decimal("NaN")}, "finite"),
])
def test_unreliable_inputs_are_rejected(kw, why):
    z = size(**kw)
    assert not z.approved and why in " ".join(z.reasons)


def test_unknown_precision_is_rejected():
    bad = InstrumentSpec("bybit", "BTC/USDT", "BTCUSDT", "BTC", "USDT", "Trading", True, Decimal(0), Decimal(0),
                         Decimal(0), Decimal(0))
    z = size_long(RULES, bad, equity_quote=Decimal(1000), free_quote=Decimal(1000), entry_price=Decimal(100),
                  stop_price=Decimal(98))
    assert not z.approved and "precision unknown" in z.reasons[0]


def snap(stop="98", highest="100", plan=None, bars_held=1, tp=None):
    return mg.TradeSnapshot(Decimal("100"), Decimal("98"), Decimal(stop), Decimal(1), Decimal(tp) if tp else None,
                            Decimal(highest), bars_held, mg.validate_plan(plan), Decimal(10))


def test_plan_validation_rejects_unsafe_plans():
    for bad in [{"partials": [{"at_r": 1, "fraction": 0.6}, {"at_r": 2, "fraction": 0.5}]},
                {"trailing": {"method": "martingale"}}, {"trailing": {"method": "percent", "percent": 50}},
                {"partials": [{"at_r": 1, "fraction": 0.5, "stop_to": "below_entry"}]}, {"time_stop_bars": 0}]:
        with pytest.raises(mg.PlanError):
            mg.validate_plan(bad)


def test_hold_states_its_evidence():
    d = mg.decide(snap(), mg.MarketSnapshot(Decimal("100.5"), Decimal("100.6")), Decimal("0.01"))
    assert [x.action for x in d] == ["HOLD"] and "0.25 R" in d[0].reason


def test_trailing_only_tightens_and_keeps_a_gap_below_the_bid():
    plan = {"trailing": {"method": "percent", "activate_r": 1, "percent": 1}}
    d = mg.decide(snap(highest="104", plan=plan), mg.MarketSnapshot(Decimal("103"), Decimal("103.1")), Decimal("0.01"))
    assert d[0].action == "MOVE_STOP" and Decimal(d[0].detail["new_stop"]) == Decimal("102.79")
    d = mg.decide(snap(stop="103", highest="104", plan=plan), mg.MarketSnapshot(Decimal("103.5"), Decimal("103.6")),
                  Decimal("0.01"))
    assert all(x.action != "MOVE_STOP" for x in d)  # 1% below 104 = 102.96 < current stop 103: no widening


def test_swing_and_atr_trailing():
    bars = [mg.Bar(Decimal(100 + i), Decimal(99 + i), Decimal("99.5") + i) for i in range(20)]
    d = mg.decide(snap(highest="120", plan={"trailing": {"method": "swing", "activate_r": 1, "swing_bars": 3}}),
                  mg.MarketSnapshot(Decimal("119"), Decimal("119.1"), bars), Decimal("0.01"))
    assert d[0].action == "MOVE_STOP" and Decimal(d[0].detail["new_stop"]) == Decimal("116")
    d = mg.decide(snap(highest="120", plan={"trailing": {"method": "atr", "activate_r": 1, "atr_mult": 2}}),
                  mg.MarketSnapshot(Decimal("119"), Decimal("119.1"), bars), Decimal("0.01"))
    assert d[0].action == "MOVE_STOP" and Decimal(d[0].detail["new_stop"]) == Decimal("117")  # TR 1.5, 2 x ATR = 3


def test_exits_need_a_stated_reason():
    m = mg.MarketSnapshot(Decimal("100.2"), Decimal("100.3"), strategy_exit="close below EMA 21")
    assert mg.decide(snap(), m, Decimal("0.01"))[0].action == "CLOSE"
    d = mg.decide(snap(plan={"time_stop_bars": 10}, bars_held=10), mg.MarketSnapshot(Decimal("100.1"), Decimal("100.2")),
                  Decimal("0.01"))
    assert d[0].action == "CLOSE" and "time stop" in d[0].reason
    d = mg.decide(snap(), mg.MarketSnapshot(Decimal("97.9"), Decimal("98")), Decimal("0.01"))
    assert d[0].action == "ALERT"  # at the stop the exchange order acts; the engine does not sell twice
