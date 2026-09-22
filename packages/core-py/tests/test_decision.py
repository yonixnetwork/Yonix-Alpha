from decimal import Decimal

from yonixalpha_core.decision import Decision, DecisionType, EntryType, no_trade
from yonixalpha_core.risk import DataQuality


def test_no_trade_helper_produces_correct_shape():
    decision = no_trade("no price feed available")
    assert decision.decision == DecisionType.NO_TRADE
    assert decision.confidence == 0.0
    assert decision.entry_type is None
    assert decision.entry is None
    assert decision.stop_loss is None
    assert decision.take_profit == []
    assert decision.risk_score == 0.0
    assert decision.reason == ["no price feed available"]
    assert decision.data_quality == DataQuality.UNAVAILABLE


def test_no_trade_accepts_custom_data_quality():
    decision = no_trade("data too old", data_quality=DataQuality.STALE)
    assert decision.data_quality == DataQuality.STALE


def test_to_dict_serializes_decimals_as_strings():
    decision = Decision(
        decision=DecisionType.LONG,
        confidence=0.75,
        entry_type=EntryType.LIMIT,
        entry=Decimal("50000.5"),
        stop_loss=Decimal("49000"),
        take_profit=[Decimal("51000"), Decimal("52000")],
        risk_score=0.2,
        reason=["accelerating volume"],
        data_quality=DataQuality.HEALTHY,
    )
    body = decision.to_dict()
    assert body["decision"] == "LONG"
    assert body["entry"] == "50000.5"
    assert body["take_profit"] == ["51000", "52000"]
    assert body["data_quality"] == "healthy"


def test_to_dict_handles_none_fields():
    decision = no_trade("insufficient data")
    body = decision.to_dict()
    assert body["entry_type"] is None
    assert body["entry"] is None
    assert body["stop_loss"] is None
    assert body["take_profit"] == []
