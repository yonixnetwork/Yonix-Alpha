from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core import paper_engine
from yonixalpha_core.db.models import Notification, PaperAccount, PaperOrder, PaperPosition, StrategyState
from yonixalpha_core.exit_intel import solana_exit_decision
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.liquidity import book_from_levels
from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    MarketInfo,
    Observation,
    StrategyLevels,
    StrategyMode,
    StrategySignal,
)
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana.flow import Trade

from app.gate_manage import manage_gate_positions
from app.grid_engine import load_state, run_grid, stop_grid

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def book(mid, depth=Decimal(50)):
    bids = [(mid - Decimal("0.05") - Decimal("0.1") * i, depth) for i in range(100)]
    asks = [(mid + Decimal("0.05") + Decimal("0.1") * i, depth) for i in range(100)]
    return book_from_levels(bids, asks, Decimal(5))


class Venue:
    def __init__(self, mid):
        self.mid_price = Decimal(mid)

    async def book(self, symbol, limit=100):
        return book(self.mid_price)

    async def mid(self, coin):
        return self.mid_price


async def open_futures_short(session_factory):
    settings = default_settings_for("binance_futures")
    m = book(Decimal(3000))
    inp = AssessmentInput(
        engine="binance_futures", strategy_name="meta_muse", asset_id="ETHUSDT", symbol="ETHUSDT", now=NOW,
        market=MarketInfo(Observation("t", NOW), m.mid, Decimal("0.003"), m.liquidity_quote, None), token=None, holders=None,
        flow=None, account=AccountState(Decimal(1000), Decimal(1000), 0, Decimal(0), Decimal(0), None, Decimal(0), False),
        liquidity_model=m, signal=StrategySignal("meta_muse", "1", True, 1.0), side="SHORT",
        strategy_levels=StrategyLevels(stop_loss=Decimal(3030), take_profits=[Decimal(2940)], source="meta_muse"),
    )
    a = assess(inp, settings)
    assert a.executable, a.reasons
    async with session_factory() as s:
        acct = await store.get_paper_account(s, "binance_futures")
        row, _ = await store.persist_assessment(s, a, None, "fut-1")
        pos = await paper_engine.open_position(s, acct, a, row.id, None, m, None, None, NOW,
                                               venue={"kind": "futures", "venue": "binance", "symbol": "ETHUSDT"})
        await s.commit()
        return pos.id


async def test_futures_short_takes_profit_against_live_book(session_factory, redis_client):
    pid = await open_futures_short(session_factory)
    counts = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(minutes=5),
                                         {"binance": Venue("2930")}, None)
    assert counts["closed"] == 1
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        acct = await s.get(PaperAccount, p.account_id)
        kinds = [n.kind for n in (await s.execute(select(Notification))).scalars()]
    assert p.exit_reason == "take_profit_1" and p.realized_pnl > 0
    assert acct.cash_balance == Decimal(1000) + p.realized_pnl
    assert "tp1" in kinds and "close" in kinds


async def test_futures_position_left_alone_when_book_unavailable(session_factory, redis_client):
    pid = await open_futures_short(session_factory)
    counts = await manage_gate_positions(session_factory, redis_client, None, NOW, {}, None)
    assert counts["unpriced"] == 1
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"


def test_exit_intelligence_needs_two_pieces_of_evidence():
    def tr(sec, who, buy, sol):
        return Trade(NOW - timedelta(seconds=sec), who, buy, sol, 1, 1, 1)

    selling = [tr(60, f"s{i}", False, 10**9) for i in range(6)] + [tr(50, "b0", True, 10**8)]
    assert solana_exit_decision(selling, NOW, None, None, None).action == "REDUCE"
    # Heavy selling by one wallet only: sell pressure without seller dominance -> hold.
    one = [tr(60, "whale", False, 5 * 10**9), tr(50, "b0", True, 10**8), tr(40, "b1", True, 10**8)]
    assert solana_exit_decision(one, NOW, None, None, None).action == "HOLD"
    assert solana_exit_decision(one, NOW, None, Decimal(30), Decimal(15)).action == "EXIT"
    assert solana_exit_decision(one, NOW, "whale", None, None).action == "EXIT"
    quiet = [tr(60, "b0", True, 10**8)]
    assert solana_exit_decision(quiet, NOW, None, Decimal(30), Decimal(10)).action == "HOLD"


async def test_grid_starts_fills_and_stops_returning_capital(session_factory, redis_client):
    venues = {"hyperliquid": Venue("100")}
    async with session_factory() as s:
        await store.save_strategy_config(s, "hyperliquid_grid", {"coin": "BTC", "grid_levels": 4, "range_pct": "1",
                                                                "capital": "100", "maker_fee_bps": "0"}, None)
        await s.commit()
    assert (await run_grid(session_factory, redis_client, None, venues, NOW))["status"] == "running"
    async with session_factory() as s:
        acct = await store.get_paper_account(s, "hyperliquid")
        assert acct.cash_balance == Decimal(900)
    venues["hyperliquid"].mid_price = Decimal("99.4")
    r = await run_grid(session_factory, redis_client, None, venues, NOW + timedelta(seconds=15))
    assert r["fills"] == 1
    venues["hyperliquid"].mid_price = Decimal("100.1")
    await run_grid(session_factory, redis_client, None, venues, NOW + timedelta(seconds=30))
    async with session_factory() as s:
        orders = (await s.execute(select(PaperOrder))).scalars().all()
        assert [o.side for o in orders] == ["BUY", "SELL"]
        row = await load_state(s, "BTC")
        await stop_grid(s, redis_client, row, Decimal("100.1"), "test")
        await s.commit()
        acct = await store.get_paper_account(s, "hyperliquid")
    assert acct.cash_balance > Decimal(1000)  # one completed grid round trip at zero fee


async def test_grid_refuses_when_worst_case_exceeds_risk_budget(session_factory, redis_client):
    async with session_factory() as s:
        await store.save_strategy_config(s, "hyperliquid_grid", {"coin": "ETH", "capital": "900", "range_pct": "20", "range_break_pct": "10",
                                                                "grid_levels": 10}, None)
        await s.commit()
    r = await run_grid(session_factory, redis_client, None, {"hyperliquid": Venue("3000")}, NOW)
    assert r["status"] == "refused"
    async with session_factory() as s:
        row = (await s.execute(select(StrategyState))).scalar_one()
        assert "worst-case loss" in row.state["reason"]
        assert (await store.get_paper_account(s, "hyperliquid")).cash_balance == Decimal(1000)


async def test_grid_stops_when_strategy_turned_off(session_factory, redis_client):
    venues = {"hyperliquid": Venue("100")}
    async with session_factory() as s:
        await store.save_strategy_config(s, "hyperliquid_grid", {"coin": "BTC", "capital": "50"}, None)
        await s.commit()
    await run_grid(session_factory, redis_client, None, venues, NOW)
    async with session_factory() as s:
        await store.set_strategy_mode(s, "hyperliquid_grid", StrategyMode.OFF, None)
        await s.commit()
    assert (await run_grid(session_factory, redis_client, None, venues, NOW))["status"] == "stopped"
    async with session_factory() as s:
        assert (await store.get_paper_account(s, "hyperliquid")).cash_balance == Decimal(1000)
