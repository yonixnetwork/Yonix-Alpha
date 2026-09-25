"""The bridge against the fake terminal, driven through YonixAlpha's own
MT5BridgeProvider and MT5Market over an in-process ASGI transport — the
same code path as production, minus the Windows terminal."""

from decimal import Decimal

import httpx
import pytest

from app.main import comment_for, create_app
from tests import fake_mt5
from yonixalpha_core.execution.base import ExecutionError
from yonixalpha_core.execution.mt5_bridge import MT5BridgeProvider
from yonixalpha_core.venues.common import VenueError
from yonixalpha_core.venues.mt5 import MT5Market

TOKEN = "t" * 40


@pytest.fixture
def app(tmp_path):
    fake_mt5.reset()
    return create_app(fake_mt5, {"MT5_BRIDGE_TOKEN": TOKEN, "MT5_LOGIN": "123", "MT5_PASSWORD": "pw", "MT5_SERVER": "Demo",
                                 "MT5_BRIDGE_STATE_PATH": str(tmp_path / "state.json")})


def client(app, token=TOKEN):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge"), token


def test_refuses_to_start_without_a_strong_token():
    with pytest.raises(RuntimeError, match="at least 32"):
        create_app(fake_mt5, {"MT5_BRIDGE_TOKEN": "short"})


async def test_every_endpoint_requires_the_token(app):
    c, _ = client(app)
    for path in ("/health", "/balance", "/symbols/EURUSD", "/positions/EURUSD", "/fills?symbol=EURUSD&start_ms=0"):
        assert (await c.get(path)).status_code == 401
        assert (await c.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await c.get("/docs")).status_code == 404  # no public API docs
    body = (await c.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})).json()
    assert body["connected"] and "123" not in str(body) and "pw" not in str(body)  # credentials never returned


async def test_full_trade_through_the_provider(app):
    c, token = client(app)
    p = MT5BridgeProvider(c, "http://bridge", token)
    rules = await p.instrument("EURUSD")
    assert rules.qty_step == Decimal("1000.00") and rules.min_qty == Decimal("1000.00")
    st = await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "yxabc")
    assert st.status == "FILLED" and st.filled_qty == Decimal("10000.00") and st.avg_price == Decimal("1.08002")
    assert st.fee == Decimal("0.35")
    sent = fake_mt5.state["sent"][-1]
    assert sent["volume"] == 0.1 and sent["comment"] == comment_for("yxabc") and len(sent["comment"]) <= 31
    # Idempotent: the same client id never sends twice.
    again = await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "yxabc")
    assert again.filled_qty == st.filled_qty and len(fake_mt5.state["sent"]) == 1
    assert (await p.order_status("EURUSD", "yxabc")).status == "FILLED"
    pos = await p.position("EURUSD")
    assert pos.size == Decimal("10000.0") and pos.entry_price == Decimal("1.08002")
    await p.set_stop("EURUSD", "LONG", Decimal("1.07500"), pos.size, "sl1")
    assert (await p.open_protection("EURUSD"))[0]["trigger_price"] == "1.075"
    assert await p.balance() == Decimal("9500.0") and p.quote_currency == "USD"
    fake_mt5.state["tick"].bid = 1.08100
    close = await p.market_order("EURUSD", "SELL", Decimal("10000"), True, "yxclose")
    assert close.status == "FILLED" and fake_mt5.state["sent"][-1]["position"] > 0  # closes OUR ticket
    assert await p.position("EURUSD") is None
    fills = await p.fills_since("EURUSD", 0)
    assert [f.side for f in fills] == ["BUY", "SELL"] and fills[1].realized_pnl > 0


async def test_foreign_positions_and_currency_mismatch_are_left_alone(app):
    c, token = client(app)
    p = MT5BridgeProvider(c, "http://bridge", token)
    fake_mt5.state["positions"].append(fake_mt5.NS(ticket=1, symbol="EURUSD", type=0, magic=999, volume=1.0,
                                                   price_open=1.0, sl=0.0, tp=0.0, price_current=1.0, profit=0.0, comment="manual"))
    assert await p.position("EURUSD") is None  # the manual trade is not ours
    st = await p.market_order("EURUSD", "SELL", Decimal("10000"), True, "yxreduce")
    assert st.status == "REJECTED" and "no bridge-owned position" in st.error
    with pytest.raises(ExecutionError, match="profits in JPY"):
        await p.instrument("USDJPY")


async def test_rejected_stop_is_an_error(app):
    c, token = client(app)
    p = MT5BridgeProvider(c, "http://bridge", token)
    await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "yx1")
    fake_mt5.state["reject_sl"] = True
    with pytest.raises(ExecutionError, match="Invalid stops"):
        await p.set_stop("EURUSD", "LONG", Decimal("1.2"), Decimal("10000"), "sl")


async def test_crash_after_send_is_recovered_from_deal_history(app, tmp_path):
    c, token = client(app)
    p = MT5BridgeProvider(c, "http://bridge", token)
    await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "yxcrash")
    bridge = app.state.bridge
    bridge.state["yxcrash"] = {"status": "SENDING"}  # the result was never written
    st = await p.order_status("EURUSD", "yxcrash")
    assert st.status == "FILLED" and st.filled_qty == Decimal("10000.00")
    again = await p.market_order("EURUSD", "BUY", Decimal("10000"), False, "yxcrash")
    assert again.status == "FILLED" and len([s for s in fake_mt5.state["sent"] if s["action"] == 1]) == 1


async def test_market_data_and_missing_depth(app):
    c, token = client(app)
    m = MT5Market(c, "http://bridge", token)
    candles = await m.klines("EURUSD", "15m", 50)
    assert len(candles) == 50 and all(x.closed for x in candles)
    book = await m.book("EURUSD")
    assert book.mid == Decimal("1.08001")
    assert await m.mid("EURUSD") == Decimal("1.08001")
    fake_mt5.state["book"] = False
    with pytest.raises(VenueError, match="no depth of market"):
        await m.book("EURUSD")  # never invented from bid/ask
