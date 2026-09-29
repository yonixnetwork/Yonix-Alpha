"""Solana strategy rules and wallet indicators on constructed scenarios.
(The futures / FX / grid strategy ports were removed with those features;
their tests are on branch archive/legacy-futures-forex-grid-2026-09-29.)"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.solana.flow import Trade, early_buy_share, round_trip_volume_share, synchronized_buy_cluster
from yonixalpha_core.strategies.solana import momentum_signal

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def t(sec, trader, buy, sol=1_000_000_000, vs=40_000_000_000, vt=800_000_000_000_000, tok=10**12):
    return Trade(NOW - timedelta(seconds=sec), trader, buy, sol, tok, vs, vt)


def test_momentum_requires_every_axis_not_just_volume():
    prev = [t(590 - i * 30, f"p{i % 3}", i % 2 == 0, vs=40_000_000_000 + i) for i in range(6)]
    cur = [t(290 - i * 10, f"c{i}", True, vs=41_000_000_000 + i * 10**8) for i in range(20)]
    sig = momentum_signal(prev + cur, NOW, 300, 6)
    assert sig.qualified, sig.reasons
    # Same volume spike but all from one wallet, price falling: not momentum.
    dump = [t(290 - i * 10, "whale", i % 2 == 0, vs=41_000_000_000 - i * 10**8) for i in range(20)]
    assert not momentum_signal(prev + dump, NOW, 300, 6).qualified


def test_wallet_indicators():
    created = NOW - timedelta(minutes=10)
    trades = [Trade(created + timedelta(seconds=5), f"s{i}", True, 10**9, 50 * 10**12, 1, 1) for i in range(4)]
    assert early_buy_share(trades, created, 10**15) == Decimal("0.2")
    assert early_buy_share(trades, None, 10**15) is None
    sync = [Trade(NOW - timedelta(seconds=30), f"w{i}", True, 1_000_000_000 + i * 1_000_000, 1, 1, 1) for i in range(5)]
    assert synchronized_buy_cluster(sync, NOW, 300) == 5
    rt = [t(100, "a", True), t(90, "a", False), t(80, "b", True)]
    assert round_trip_volume_share(rt, NOW, 300) == Decimal(2) / Decimal(3)
