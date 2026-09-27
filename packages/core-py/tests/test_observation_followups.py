"""T+5m / T+10m / T+30m / T+60m snapshots, migration and final outcome for
observed tokens (traded or not), from real stream / pool data only."""

import os
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import TokenObservation  # noqa: E402
from yonixalpha_core.solana import pump_stream  # noqa: E402
from yonixalpha_core.solana.followups import track_observation_followups  # noqa: E402
from yonixalpha_core.testing.pump import MINT, seed_healthy_launch, wallet  # noqa: E402

from tests.test_pump_pipeline import NOW  # noqa: E402

LAUNCH = NOW - timedelta(minutes=20)  # seed_healthy_launch creates the token 20 min before NOW


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def _obs(mint: str, price_raw: str, outcome: str = "NO_TRADE") -> TokenObservation:
    return TokenObservation(mint=mint, launched_at=LAUNCH, outcome=outcome, reasons=[], decided_at=LAUNCH + timedelta(minutes=1),
                            report={"checkpoints": [{"label": "T0", "price_raw": None}, {"label": "T+10s", "price_raw": price_raw}]})


async def test_snapshots_fill_as_they_fall_due_and_finish_at_t60(db, redis):
    curve = await seed_healthy_launch(redis, NOW)
    now_price = Decimal(curve.vsol) / Decimal(curve.vtok)
    decision = now_price / 2  # the token doubled since the (rejected) decision
    db.add(_obs(MINT, str(decision)))
    await db.commit()

    assert await track_observation_followups(db, redis, None, NOW) == 1
    row = (await db.execute(TokenObservation.__table__.select())).first()
    f = row.followups
    assert set(f) == {"T+5m", "T+10m"} and f["T+5m"]["late"] is True  # first run 20 min after launch: 15 min late
    assert f["T+5m"]["source"] == "pump_stream curve (last trade)" and f["T+5m"]["change_vs_decision_pct"] == "100.00"
    assert f["T+5m"]["migrated"] is False and f["T+5m"]["liquidity_sol"]

    # Nothing new is due until T+30m.
    assert await track_observation_followups(db, redis, None, NOW + timedelta(minutes=5)) == 0
    assert await track_observation_followups(db, redis, None, LAUNCH + timedelta(minutes=31)) == 1
    assert await track_observation_followups(db, redis, None, LAUNCH + timedelta(minutes=61)) == 1
    await db.commit()
    row = (await db.execute(TokenObservation.__table__.select())).first()
    f = row.followups
    assert {"T+30m", "T+60m", "final"} <= set(f) and f["T+60m"]["late"] is False
    assert f["final"]["outcome_at_decision"] == "NO_TRADE" and f["final"]["change_vs_decision_pct_60m"] == "100.00"
    assert f["final"]["migrated"] is False
    # Finished rows are not touched again.
    assert await track_observation_followups(db, redis, None, LAUNCH + timedelta(minutes=70)) == 0


async def test_migrated_token_records_the_migration_and_why_it_was_not_priced(db, redis):
    other = wallet(88)
    await redis.zadd(pump_stream.MIGRATED, {other: int((LAUNCH + timedelta(minutes=3)).timestamp())})
    db.add(_obs(other, "1", outcome="MIGRATION_DETECTED"))
    await db.commit()
    assert await track_observation_followups(db, redis, None, NOW) == 1
    row = (await db.execute(TokenObservation.__table__.select())).first()
    f = row.followups
    assert f["T+5m"]["migrated"] is True and "no RPC" in f["T+5m"]["unavailable"] and "price_raw" not in f["T+5m"]
    assert f["migration"]["migrated_at"].startswith((LAUNCH + timedelta(minutes=3)).isoformat()[:16])
