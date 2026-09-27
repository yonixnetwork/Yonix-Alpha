"""A manual BUY (operator_request) replaces only the strategy entry signal
and the ML confidence floor. Every safety finding still blocks."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from yonixalpha_core.safety import SafetySettings
from yonixalpha_core.safety.models import FinalDecision, MLInput, Observation, StrategySignal

from tests.test_safety_gate import NOW, codes, decide, healthy

NO_SIGNAL = StrategySignal("solana_momentum", "1", False, 0.1, ["no acceleration"])


def test_without_signal_the_gate_waits():
    a = decide(healthy(signal=NO_SIGNAL))
    assert not a.executable and "SIGNAL_NOT_QUALIFIED" in codes(a)


def test_operator_request_replaces_only_the_signal():
    a = decide(healthy(signal=NO_SIGNAL, operator_request=True))
    assert a.executable and a.decision == FinalDecision.EXECUTE, a.reasons
    assert "OPERATOR_BUY_REQUEST" in codes(a) and "SIGNAL_NOT_QUALIFIED" not in codes(a)
    b = decide(healthy(signal=None, operator_request=True))
    assert b.executable and "NO_SIGNAL" not in codes(b)


def test_operator_request_lifts_the_ml_floor_only():
    low_ml = MLInput("m", 1, 0.1)
    s = SafetySettings(min_ml_confidence=0.6)
    assert not decide(healthy(ml=low_ml), s).executable
    assert decide(healthy(ml=low_ml, operator_request=True), s).executable


def test_operator_request_never_bypasses_token_risk():
    a = decide(healthy(token=replace(healthy().token, freeze_authority="F" * 32), operator_request=True))
    assert not a.executable and a.decision == FinalDecision.REJECT


def test_operator_request_never_bypasses_stale_critical_data():
    stale = Observation("rpc", NOW - timedelta(seconds=600))
    a = decide(healthy(market=replace(healthy().market, observation=stale), operator_request=True))
    assert not a.executable and "MARKET_STALE" in codes(a)


def test_operator_request_never_bypasses_missing_liquidity_model_or_wallet():
    a = decide(healthy(liquidity_model=None, operator_request=True))
    assert not a.executable
    broke = replace(healthy().account, available_balance=Decimal("0"), equity=Decimal("0"))
    b = decide(healthy(account=broke, operator_request=True))
    assert not b.executable
