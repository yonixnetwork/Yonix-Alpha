"""Market regimes (§28): hourly launchpad-market summaries computed once per
completed hour, classified against the window's medians, and a wallet that
only made money in one regime is REGIME_DEPENDENT."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import market_regimes as mr  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EvmTrade, MarketRegimeHour  # noqa: E402
from yonixalpha_core.wallet_pnl import ClosedTrade  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
H0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
E18 = 10 ** 18


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def tr(n, hour, minute, token, buy, quote, tokens=1000):
    return EvmTrade(event_id=f"e{n}", chain="bsc", launchpad="flap", token=token, trader="0x" + "1" * 40, is_buy=buy,
                    token_amount=Decimal(tokens), quote_amount=Decimal(int(quote * E18)),
                    at=H0 + timedelta(hours=hour, minutes=minute))


async def test_hours_are_summarised_once_and_classified(db):
    rows = [tr(1, 0, 1, "A", True, 1.0), tr(2, 0, 2, "A", True, 1.0), tr(3, 0, 3, "A", True, 2.0),  # 9h: buying, A ranges 100%
            tr(4, 1, 1, "B", False, 0.2), tr(5, 1, 2, "B", False, 0.2), tr(6, 1, 3, "B", True, 0.21),  # 10h: selling, quiet
            tr(7, 2, 5, "C", True, 3.0), tr(8, 2, 6, "C", False, 3.0), tr(9, 2, 7, "C", True, 3.0)]  # 11h: big, flat
    db.add_all(rows)
    await db.commit()
    assert await mr.update(db, "bsc", NOW, timedelta(hours=4)) == 3  # 9h..11h: starts at the first traded hour; 12h is open
    await db.commit()
    assert await mr.update(db, "bsc", NOW, timedelta(hours=4)) == 0  # incremental: nothing recomputed
    hours = {h.hour.hour: h for h in (await db.execute(select(MarketRegimeHour))).scalars()}
    assert hours[9].trades == 3 and hours[9].volume == Decimal("4") and hours[9].net_flow == Decimal(1)
    assert hours[9].median_range == Decimal(1) and hours[10].net_flow < 0 and 8 not in hours
    empty = MarketRegimeHour(chain="bsc", hour=H0 - timedelta(hours=1), volume=Decimal(0), net_flow=None,
                             median_range=None, tokens=0, trades=0)
    reg = mr.classify([*hours.values(), empty])
    assert empty.hour not in reg  # an empty hour gets no invented regime
    assert reg[H0]["trend"] == "BULLISH" and reg[H0 + timedelta(hours=1)]["trend"] == "BEARISH"
    assert reg[H0 + timedelta(hours=2)]["volume"] == "HIGH_VOLUME" and reg[H0 + timedelta(hours=1)]["volume"] == "LOW_VOLUME"


def c(i, hour, pnl):
    opened = H0 + timedelta(hours=hour, minutes=i)
    return ClosedTrade(f"t{i}", Decimal(1), Decimal(1) + Decimal(str(pnl)), opened, opened + timedelta(minutes=5), Decimal(1))


def test_a_wallet_profitable_only_in_bullish_hours_is_regime_dependent():
    regimes = {H0: {"volume": "HIGH_VOLUME", "trend": "BULLISH", "volatility": "HIGH_VOLATILITY"},
               H0 + timedelta(hours=1): {"volume": "LOW_VOLUME", "trend": "BEARISH", "volatility": "LOW_VOLATILITY"}}
    lucky = [c(i, 0, 0.5) for i in range(4)] + [c(10 + i, 1, -0.3) for i in range(4)]
    r = mr.regime_test(lucky, regimes)
    assert r["status"] == "REGIME_DEPENDENT" and "BEARISH" in r["reason"]
    assert r["dimensions"]["trend"]["sides"]["BULLISH"]["wins"] == 4
    steady = [c(i, 0, 0.2) for i in range(4)] + [c(10 + i, 1, 0.1) for i in range(4)]
    assert mr.regime_test(steady, regimes)["status"] == "CONSISTENT"
    few = [c(i, 0, 0.2) for i in range(4)] + [c(10, 1, 0.1)]
    assert mr.regime_test(few, regimes)["status"] == "INSUFFICIENT_DATA"
    assert mr.regime_test(steady, {})["status"] == "INSUFFICIENT_DATA"
