from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import Notification, PaperPosition, RiskAssessment
from yonixalpha_core.safety import store
from yonixalpha_core.safety.liquidity import book_from_levels
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.venues.common import Candle

from app.futures_eval import run_strategy

NOW = datetime.now(timezone.utc).replace(second=30, microsecond=0)
ENV = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)


def series(start, pct, n=100, step=timedelta(minutes=5)):
    t0 = NOW - step * n
    out = []
    for i in range(n):
        c = Decimal(str(start * (1 + pct) ** i))
        out.append(Candle(t0 + step * i, c, c * Decimal("1.001"), c * Decimal("0.999"), c, Decimal(1), True))
    return out


class FakeVenue:
    def __init__(self, btc_pct=-0.002, eth_pct=0.002, depth=Decimal("50")):
        self.btc_pct, self.eth_pct, self.depth = btc_pct, eth_pct, depth

    async def klines(self, symbol, interval, limit=200, now=None):
        return series(60000, self.btc_pct) if symbol == "BTCUSDT" else series(3000, self.eth_pct)

    async def book(self, symbol, limit=100):
        mid = series(3000, self.eth_pct)[-1].close
        bids = [(mid - Decimal("0.05") - Decimal("0.1") * i, self.depth) for i in range(100)]
        asks = [(mid + Decimal("0.05") + Decimal("0.1") * i, self.depth) for i in range(100)]
        return book_from_levels(bids, asks, Decimal(5))


async def test_meta_muse_signal_goes_through_gate_into_paper(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    venues = {"binance": FakeVenue()}
    r = await run_strategy(sf, redis_client, ENV, venues, "meta_muse", NOW)
    assert r["status"] == "EXECUTE" and r["side"] == "LONG", r
    async with sf() as s:
        pos = (await s.execute(select(PaperPosition))).scalar_one()
        a = (await s.execute(select(RiskAssessment))).scalar_one()
    assert pos.side == "LONG" and pos.engine == "binance_futures" and pos.plan["venue"]["kind"] == "futures"
    assert a.strategy == "meta_muse" and a.assessment["plan"]["stop_loss"]["provenance"] == "STRATEGY"
    # The same closed candle is never evaluated twice.
    again = await run_strategy(sf, redis_client, ENV, venues, "meta_muse", NOW)
    assert again["status"] == "candle already evaluated"


async def test_meta_muse_exit_rule_requests_exit_on_next_candle(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    weak = {"binance": FakeVenue(btc_pct=-0.00001)}
    later = NOW + timedelta(minutes=5)

    async def shifted(symbol, interval, limit=200, now=None):
        base = series(60000, -0.00001) if symbol == "BTCUSDT" else series(3000, 0.002)
        return [Candle(c.open_time + timedelta(minutes=5), c.open, c.high, c.low, c.close, c.volume, True) for c in base]

    weak["binance"].klines = shifted
    r = await run_strategy(sf, redis_client, ENV, weak, "meta_muse", later)
    assert r["status"].startswith("exit requested: trend weakened")
    async with sf() as s:
        assert (await s.execute(select(PaperPosition))).scalar_one().exit_requested


async def test_venue_mode_off_disables_strategy(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        await store.set_strategy_mode(s, "binance_futures", StrategyMode.OFF, None)
        await s.commit()
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r == {"strategy": "meta_muse", "status": "off"}


async def test_manual_mode_asks_for_approval_and_notifies(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        await store.set_strategy_mode(s, "meta_muse", StrategyMode.MANUAL, None)
        await s.commit()
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "REQUIRE_MANUAL_APPROVAL"
    async with sf() as s:
        n = (await s.execute(select(Notification))).scalar_one()
        assert n.kind == "approval_required" and (await s.execute(select(PaperPosition))).first() is None


async def test_thin_book_blocks_or_fails_the_entry(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue(depth=Decimal("0.0001"))}, "meta_muse", NOW)
    assert r["status"] in ("NO_TRADE",) or r["status"].startswith("entry failed"), r
    async with sf() as s:
        assert (await s.execute(select(PaperPosition))).first() is None


async def test_no_divergence_records_nothing(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue(btc_pct=0.002)}, "meta_muse", NOW)
    assert r["status"] == "no signal"
    async with sf() as s:
        assert (await s.execute(select(RiskAssessment))).first() is None


class ConfluenceVenue:
    """Gold-like zigzag with a confirmed pivot high, then a breakout bar."""

    def __init__(self):
        closes = []
        for _ in range(6):
            closes += [2000 + i * 2 for i in range(13)] + [2022 - i * 2 for i in range(13)]
        closes += [2002 + i for i in range(20)] + [2026]
        step = timedelta(minutes=15)
        t0 = NOW - step * len(closes)
        self.candles = [Candle(t0 + step * i, Decimal(c), Decimal(c) + Decimal("0.5"), Decimal(c) - Decimal("0.5"),
                               Decimal(c), Decimal(1), True) for i, c in enumerate(closes)]

    async def klines(self, symbol, interval, limit=200, now=None):
        return self.candles

    async def book(self, symbol, limit=100):
        mid = self.candles[-1].close
        bids = [(mid - Decimal("0.05") - Decimal("0.1") * i, Decimal(50)) for i in range(100)]
        asks = [(mid + Decimal("0.05") + Decimal("0.1") * i, Decimal(50)) for i in range(100)]
        return book_from_levels(bids, asks, Decimal(5))


async def test_confluence_breakout_uses_pivot_levels(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    async with sf() as s:
        await store.set_strategy_mode(s, "meta_muse", StrategyMode.OFF, None)
        await s.commit()
    r = await run_strategy(sf, redis_client, ENV, {"binance": ConfluenceVenue()}, "confluence_matrix", NOW)
    async with sf() as s:
        a = (await s.execute(select(RiskAssessment))).scalar_one()
    plan = a.assessment["plan"]
    assert a.strategy == "confluence_matrix" and plan["side"] == "LONG"
    assert plan["stop_loss"]["provenance"] == "STRATEGY" and plan["move_stop_to_breakeven_at_tp1"] is True
    assert [tp["exit_fraction"] for tp in plan["take_profits"]] == ["0.5", "0.5"]
    assert r["status"] in ("EXECUTE", "REDUCE_SIZE", "NO_TRADE"), r
