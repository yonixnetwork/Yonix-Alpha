"""Trading Wallet balances (master §56-58): Total / Available / Reserved /
Gas reserve / Trading balance per chain, LIVE and PAPER apart; a value
that cannot be read is None with the reason, never 0."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core import balances

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def test_live_rows_follow_the_definitions_and_freshness():
    w = {"sol": "2.5", "at": (NOW - timedelta(seconds=30)).isoformat(), "pubkey": "So1Pub"}
    r = balances.solana_live(w, Decimal("0.05"), Decimal("0.4"), NOW, "150").to_dict()
    assert (r["total"], r["reserved"], r["available"], r["gas_reserve"], r["trading_balance"]) == (
        "2.5", "0.4", "2.1", "0.05", "2.05")
    assert r["status"] == "LIVE" and r["usd"]["total"] == "375.00" and r["account"] == "Solana account"
    stale = balances.solana_live({**w, "at": (NOW - timedelta(minutes=10)).isoformat()}, Decimal("0.05"), Decimal(0),
                                 NOW, None).to_dict()
    assert stale["status"] == "STALE" and stale["usd"]["total"] is None  # no fresh rate: no USD, never a guess
    none = balances.solana_live(None, Decimal("0.05"), Decimal(0), NOW, "150").to_dict()
    assert none["status"] == "UNAVAILABLE" and none["total"] is None and none["trading_balance"] is None

    evm = balances.evm_live("bsc", "0xabc", {"balance": "0.001", "at": NOW.isoformat()}, Decimal("0.002"), NOW, "600")
    d = evm.to_dict()
    assert d["account"] == "EVM account" and d["currency"] == "BNB" and d["trading_balance"] == "0"  # below the reserve
    assert balances.evm_live("robinhood", None, None, Decimal("0.0005"), NOW, None).to_dict()["status"] == "NOT_CONFIGURED"


def test_paper_rows_reserve_open_positions():
    r = balances.paper("robinhood", Decimal("0.2"), Decimal("0.05"), Decimal("0.0005"), None).to_dict()
    assert (r["total"], r["reserved"], r["available"], r["trading_balance"]) == ("0.25", "0.05", "0.20", "0.1995")
    assert r["mode"] == "PAPER" and r["currency"] == "ETH"
    sol = balances.paper("solana", Decimal("10"), Decimal("0"), None, "150", "no fee reserve in paper").to_dict()
    assert sol["gas_reserve"] is None and sol["trading_balance"] is None and "no fee reserve in paper" in sol["notes"]
