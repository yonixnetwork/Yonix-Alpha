"""Volatility confidence and entry deterioration.

Volatility is never invented: AVAILABLE keeps the existing measurements,
LOW_CONFIDENCE is measured from few trades and needs approval, UNAVAILABLE
produces no number and says why. Deterioration needs two independent,
multi-window signs; a short lull alone is only reported."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.safety.models import FinalDecision
from yonixalpha_core.solana import entry_quality as eq
from yonixalpha_core.solana.flow import Trade

from tests.test_safety_gate import codes, decide, healthy

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
DEC = 6
VS, VT = 30 * 10**9, 1_000_000_000 * 10**6


def trade(sec_ago: float, who: str, buy: bool, sol: float, price_mult: float = 1.0) -> Trade:
    """A trade `sec_ago` seconds before NOW at price VS/VT × price_mult."""
    return Trade(NOW - timedelta(seconds=sec_ago), who, buy, int(sol * 10**9), 10**9,
                 int(VS * price_mult), VT)


# --- volatility confidence -------------------------------------------------------------

def test_existing_measurement_stays_available():
    trades = [trade(900 - 60 * i, f"w{i}", i % 2 == 0, 0.1, 1 + 0.02 * (i % 3)) for i in range(14)]
    v = eq.volatility_estimate(trades, NOW, 900, DEC)
    assert v["confidence"] == eq.AVAILABLE and v["value"] is not None and "1-minute" in v["source"]


def test_young_token_with_a_few_trades_is_low_confidence_not_unavailable():
    trades = [trade(20 - 2 * i, f"w{i}", True, 0.1, 1 + 0.03 * i) for i in range(8)]  # 8 trades in 16 s
    v = eq.volatility_estimate(trades, NOW, 900, DEC)
    assert v["confidence"] == eq.LOW_CONFIDENCE and v["value"] > 0 and v["samples"] == 7
    assert "trade-to-trade" in v["source"]


def test_too_few_trades_is_unavailable_with_the_reason_and_no_number():
    trades = [trade(10, "a", True, 0.1), trade(5, "b", True, 0.1, 1.1)]
    v = eq.volatility_estimate(trades, NOW, 900, DEC)
    assert v["value"] is None and v["confidence"] == eq.UNAVAILABLE and "2 trades" in v["source"]


def test_gate_low_confidence_volatility_needs_approval_but_a_manual_buy_proceeds():
    m = replace(healthy().market, volatility=Decimal("0.04"), volatility_confidence=eq.LOW_CONFIDENCE,
                volatility_note="trade-to-trade returns (7)")
    auto = decide(healthy(market=m))
    assert not auto.executable and auto.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert "VOLATILITY_LOW_CONFIDENCE" in codes(auto)
    manual = decide(healthy(market=m, operator_request=True, manual_approval_granted=True))
    assert manual.executable, manual.reasons


def test_gate_says_why_volatility_is_unavailable_and_still_needs_a_stop():
    m = replace(healthy().market, volatility=None, volatility_confidence=eq.UNAVAILABLE,
                volatility_note="unavailable: 2 trades in the last 5 min (need 6)")
    a = decide(healthy(market=m, operator_request=True))
    assert not a.executable and {"VOLATILITY_UNAVAILABLE", "AUTO_SL_NO_VOLATILITY"} <= codes(a)
    assert any("2 trades" in r for r in a.reasons) or any("2 trades" in f.message for f in a.findings)


# --- deterioration ---------------------------------------------------------------------

def growing() -> list[Trade]:
    out = []
    for i in range(6):  # context: steady trading 2-6 minutes ago
        out += [trade(300 - 40 * i, f"c{i}", True, 0.2, 1 + 0.01 * i)]
    out += [trade(110 - 10 * i, f"p{i}", True, 0.2, 1.06 + 0.01 * i) for i in range(4)]  # previous minute: 4 buyers
    out += [trade(50 - 8 * i, f"n{i}", True, 0.2, 1.10 + 0.01 * i) for i in range(6)]  # this minute: 6 buyers
    return out


def test_healthy_growth_shows_no_deterioration():
    d = eq.deterioration(growing(), NOW, 60, DEC)
    assert d["indicators"] == [] and not d["strong"]


def test_a_single_lull_after_a_spike_is_not_a_collapse():
    trades = [trade(300 - 40 * i, f"c{i}", True, 0.05) for i in range(6)]  # quiet context
    trades += [trade(110 - 5 * i, f"p{i}", True, 1.0, 1.2) for i in range(8)]  # one busy minute
    trades += [trade(40, "n0", True, 0.1, 1.2), trade(20, "n1", True, 0.1, 1.21)]  # back to normal
    d = eq.deterioration(trades, NOW, 60, DEC)
    assert "volume_collapse" not in d["indicators"]  # low vs the spike, but normal vs its own context
    assert not d["strong"]


def test_sellers_accelerating_while_buyers_stall_is_strong():
    trades = [trade(300 - 40 * i, f"c{i}", True, 0.2) for i in range(6)]
    trades += [trade(110 - 10 * i, f"p{i}", True, 0.3, 1.2) for i in range(6)]  # 6 buyers, 0 sellers
    trades += [trade(50 - 8 * i, f"s{i}", False, 0.4, 1.15 - 0.02 * i) for i in range(5)]  # 5 sellers
    trades += [trade(10, "p0", True, 0.1, 1.05)]  # 1 buyer
    d = eq.deterioration(trades, NOW, 60, DEC)
    assert {"seller_acceleration", "buyer_stall"} <= set(d["indicators"]) and d["strong"]
    auto = decide(healthy(entry_quality=d))
    assert not auto.executable and auto.decision == FinalDecision.WAIT and "ENTRY_DETERIORATION" in codes(auto)
    manual = decide(healthy(entry_quality=d, operator_request=True))
    assert "ENTRY_DETERIORATION" in codes(manual)
    assert manual.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL  # shown and confirmed, not silently blocked


def test_a_one_wallet_price_spike_is_unsupported():
    trades = [trade(300 - 40 * i, f"c{i}", True, 0.2) for i in range(6)]
    trades += [trade(100, "p0", True, 0.2)]
    trades += [trade(50 - 10 * i, "whale", True, 2.0, 1.0 + 0.15 * i) for i in range(4)]  # +45% from one wallet
    d = eq.deterioration(trades, NOW, 60, DEC)
    assert "unsupported_spike" in d["indicators"] and "1 buyer" in " ".join(d["evidence"])


def test_one_indicator_alone_is_reported_but_does_not_block():
    d = {"strong": False, "indicators": ["buyer_stall"], "evidence": ["buyers 6 → 2"], "metrics": {}}
    a = decide(healthy(entry_quality=d))
    assert a.executable and "ENTRY_WEAKENING" in codes(a)


def test_a_real_collapse_against_the_baseline_with_buyers_gone_is_strong():
    trades = []
    for k in range(4):  # baseline: ~1 SOL per minute from several buyers, 2-6 minutes ago
        trades += [trade(130 + 60 * k + 10 * i, f"b{k}{i}", True, 0.25) for i in range(4)]
    trades += [trade(110 - 10 * i, f"p{i}", True, 0.3, 1.1) for i in range(5)]  # previous minute: 5 buyers
    trades += [trade(30, "late", True, 0.05, 1.1)]  # this minute: one small buy
    d = eq.deterioration(trades, NOW, 60, DEC)
    assert {"volume_collapse", "buyer_stall"} <= set(d["indicators"]) and d["strong"], d
