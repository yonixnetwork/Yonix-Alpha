"""Token terminal view: real trade events only; market cap, liquidity and
FDV are distinct and labelled; windows compare with the previous window."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from yonixalpha_core.solana.flow import Trade
from yonixalpha_core.token_market import market_view

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
VT = 10**15


def t(sec_ago, who, buy, sol, vsol):
    return Trade(NOW - timedelta(seconds=sec_ago), who, buy, int(sol * 10**9), 10**9, vsol, VT)


def test_view_from_stream_trades():
    trades = [t(200, "a", True, 0.5, 31 * 10**9), t(90, "b", True, 1.2, 32 * 10**9), t(30, "c", False, 0.3, 33 * 10**9),
              t(10, "creator", False, 0.2, 34 * 10**9)]
    curve = SimpleNamespace(pool=None, complete=False, rsol=4 * 10**9)
    meta = {"symbol": "TST", "name": "Test", "creator": "creator", "created_at": int((NOW - timedelta(minutes=5)).timestamp())}
    v = market_view("MintX", meta, curve, trades, NOW, {"decision": "WAIT", "overall_risk": "MODERATE", "reasons": ["x"],
                                                        "findings": [{"category": "STRATEGY", "code": "SIGNAL_NOT_QUALIFIED",
                                                                      "message": "no acceleration", "action": "WAIT"}]})
    h = v["header"]
    assert Decimal(h["price_sol"]) == Decimal(34 * 10**9) / Decimal(10**9) / (Decimal(VT) / Decimal(10**6))
    assert Decimal(h["market_cap_sol"]) == (Decimal(h["price_sol"]) * 10**9).quantize(Decimal("0.01"))
    assert h["liquidity"] == {"sol": "4", "basis": "bonding-curve real SOL reserve"} and h["migration_state"] == "BONDING CURVE"
    assert h["age_seconds"] == 300 and h["risk_status"] == "MODERATE"
    w1 = v["windows"]["1m"]
    assert w1["trades"] == 2 and w1["sellers"] == 2 and w1["buyers"] == 0 and w1["previous_trades"] == 1
    assert [e["kind"] for e in v["activity"]][:4] == ["SELL", "SELL", "BUY", "BUY"] and v["activity"][-1]["kind"] == "CREATED"
    assert v["activity"][0]["creator"] is True and v["activity"][2]["large"] is True
    assert v["decision"]["signal"][0]["code"] == "SIGNAL_NOT_QUALIFIED"
    assert v["links"]["dexscreener"].endswith("/solana/MintX") and v["links"]["telegram"] is None
    assert len(v["series"]) == 4 and v["series"][0]["liquidity_sol"] == "1"


def test_migrated_token_uses_labelled_pool_figures():
    curve = SimpleNamespace(pool="Pool1", complete=True, rsol=0)
    latest = {"inputs_snapshot": {"features": {"price": "0.0000005"}, "pool": {"quote_reserve_lamports": 90 * 10**9}}}
    v = market_view("MintY", {}, curve, [], NOW, latest, NOW - timedelta(minutes=3))
    assert v["header"]["price_source"].startswith("PumpSwap pool") and v["header"]["liquidity"]["sol"] == "90"
    assert v["header"]["migration_state"] == "MIGRATED (PumpSwap)" and v["activity"][0]["kind"] == "MIGRATION"
