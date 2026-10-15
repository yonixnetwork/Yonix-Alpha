import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/12")

from yonixalpha_crypto.candles import Candle  # noqa: E402
from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0, 30, tzinfo=timezone.utc)


async def migrate_fresh(url: str) -> None:
    from alembic import command

    from yonixalpha_crypto.migrate import alembic_config

    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA IF EXISTS crypto CASCADE"))
    await eng.dispose()
    # env.py runs its own event loop, so Alembic runs in a worker thread.
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")


@pytest_asyncio.fixture
async def sf():
    url = os.environ["DATABASE_URL"]
    await migrate_fresh(url)
    eng = create_async_engine(url)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def redis():
    r = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def candles(closes, interval="5m", end=NOW, step_s=300):
    """Closed candles ending at the last boundary before `end`, oldest first."""
    last_open = datetime.fromtimestamp((int(end.timestamp()) // step_s) * step_s - step_s, tz=timezone.utc)
    n = len(closes)
    return [Candle(last_open - timedelta(seconds=step_s * (n - 1 - i)), Decimal(c), Decimal(c), Decimal(c),
                   Decimal(c), Decimal(1), interval) for i, c in enumerate(closes)]


def spec(exchange="bybit", symbol="BTC/USDT", tick="0.01", step="0.000001", min_notional="1"):
    base, quote = symbol.split("/")
    return InstrumentSpec(exchange, symbol, base + quote, base, quote, "Trading", True, Decimal(tick), Decimal(step),
                          Decimal(step), Decimal(min_notional))


class FakeMarket:
    """In-memory MarketData: no network."""

    def __init__(self, exchange="bybit"):
        self.exchange = exchange
        self.series: dict[str, list[Candle]] = {}
        self.books: dict[str, TopOfBook] = {}
        self.specs: dict[str, InstrumentSpec] = {}
        self.calls: list[str] = []

    async def ping(self):
        self.calls.append("ping")

    async def instrument(self, symbol):
        self.calls.append(f"instrument {symbol}")
        return self.specs.get(symbol) or spec(self.exchange, symbol)

    async def candles(self, symbol, interval, limit):
        self.calls.append(f"candles {symbol}")
        return list(self.series.get(symbol, []))[-limit:]

    async def top_of_book(self, symbol):
        self.calls.append(f"book {symbol}")
        return self.books[symbol]

    async def aclose(self):
        pass


def book(bid, ask, at=NOW, exchange="bybit", symbol="BTC/USDT", size="10"):
    return TopOfBook(exchange, symbol, Decimal(bid), Decimal(ask), at, Decimal(size), Decimal(size))
