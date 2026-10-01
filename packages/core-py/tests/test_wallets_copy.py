"""Wallet profile metrics / score and the copy-trading rules (pure)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from yonixalpha_core import copy_trading as ct
from yonixalpha_core import wallet_profiles as wp

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
E18 = 10 ** 18


def tr(token, sec, buy, quote, tokens=10 ** 21):
    return SimpleNamespace(token=token, at=T0 + timedelta(seconds=sec), is_buy=buy, quote_amount=Decimal(quote),
                           token_amount=Decimal(tokens))


def test_evm_metrics_labels_and_shrunk_score():
    trades = []
    for i in range(6):  # 6 round trips, 4 winners, bought within 30 s of launch, held 2 min
        tok = f"T{i}"
        trades += [tr(tok, i * 1000 + 30, True, E18 // 10), tr(tok, i * 1000 + 150, False, (E18 // 10) * (2 if i < 4 else 0.5))]
    launches = {f"T{i}": T0 + timedelta(seconds=i * 1000) for i in range(6)}
    m = wp.evm_metrics(trades, launches, wp.ScoreConfig())
    assert (m["closed_tokens"], m["wins"], m["early_entry_share"], m["avg_hold_s"]) == (6, 4, 1.0, 120.0)
    assert m["realized_pnl"] == Decimal("0.3")  # 4 x +0.1 and 2 x -0.05 BNB
    assert set(wp._labels(m)) == {"SNIPER", "SCALPER"}
    score, detail = wp.score(m, wp.ScoreConfig())
    assert 0 < score < 1 and detail["components"]["win_rate_shrunk"] < 4 / 6  # shrunk toward the base rate
    few = wp.evm_metrics(trades[:4], launches, wp.ScoreConfig())
    assert wp.score(few, wp.ScoreConfig()) == (None, {"status": "INSUFFICIENT_DATA", "closed": 2, "min_closed": 5})


def test_copy_rules():
    s, errors = ct.parse_settings({"size_mode": "proportional", "proportional_pct": "0.2", "max_size": "0.03"})
    assert not errors and ct.size_for(s, "bsc", Decimal("1")) == Decimal("0.03")
    assert ct.size_for(ct.CopySettings(), "solana", Decimal("9")) == ct.DEFAULT_SIZE["solana"]
    assert len(ct.parse_settings({"size_mode": "ALL_IN", "max_delay_seconds": -1, "proportional_pct": 2})[1]) == 3
    assert ct.chase_guard(Decimal("1"), Decimal("1.1"), Decimal("0.15")) is None
    assert "above the target" in ct.chase_guard(Decimal("1"), Decimal("1.3"), Decimal("0.15"))
    assert ct.chase_guard(None, Decimal("1"), Decimal("0.15")) is not None  # unknown is never a pass
    assert ct.sell_fraction(Decimal(5), Decimal(10)) == Decimal("0.5") and ct.sell_fraction(Decimal(20), Decimal(10)) == 1
    lat = ct.latency(T0, T0 + timedelta(seconds=2), T0 + timedelta(seconds=2.3), T0 + timedelta(seconds=2.4),
                     T0 + timedelta(seconds=2.5))
    assert lat == {"detection": 2000, "analysis": 300, "risk": 100, "decision": 400, "execution": 100,
                   "build": None, "sign": None, "submission": None, "landing": None, "confirmation": None,
                   "live_only": ["build", "sign", "submission", "landing", "confirmation"], "total": 2500}
