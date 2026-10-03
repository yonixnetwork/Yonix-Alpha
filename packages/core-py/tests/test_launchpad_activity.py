"""Launchpad activity health (master upgrade §5-6): statuses from recorded
launches / trades / migrations, the 7-day inactivity rule with a monitoring
minimum, a stalled monitor reported as DEGRADED, automatic reactivation,
and an idempotent daily rollup."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.chains import activity, verification  # noqa: E402
from yonixalpha_core.chains.evm import store  # noqa: E402
from yonixalpha_core.chains.registry import LAUNCHPADS  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import LaunchpadActivity  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def cls(last=None, since=None, monitor=None, disabled=None):
    return activity.classify(disabled_reason=disabled, last_activity=last, monitored_since=since, monitor_at=monitor,
                             now=NOW)[0]


def test_statuses_follow_the_recorded_activity_and_the_seven_day_rule():
    h, d = timedelta(hours=1), timedelta(days=1)
    assert cls(NOW - 2 * h, NOW - 30 * d, NOW) == "ACTIVE"
    assert cls(NOW - 3 * d, NOW - 30 * d, NOW) == "QUIET"
    assert cls(NOW - 8 * d, NOW - 30 * d, NOW) == "INACTIVE"
    assert cls(None, NOW - 8 * d, NOW) == "INACTIVE"
    # no activity but only 2 days of monitoring: inactivity cannot be claimed yet
    assert cls(None, NOW - 2 * d, NOW) == "UNVERIFIED"
    assert cls(None, None, None) == "UNVERIFIED"
    # the monitor stopped: recent activity is unknown, never reported ACTIVE
    assert cls(NOW - 2 * h, NOW - 30 * d, NOW - 20 * timedelta(minutes=1)) == "DEGRADED"
    assert cls(NOW - 2 * h, NOW - 30 * d, NOW, disabled="switched off by the operator") == "DISABLED"
    assert set(activity.LISTED) == {"ACTIVE", "QUIET", "DEGRADED"}


class _Res:
    def __init__(self, to_block, launches=(), trades=(), migrations=()):
        self.to_block, self.launches, self.trades, self.migrations, self.other = to_block, list(launches), list(trades), list(migrations), []


class _Launch:
    def __init__(self, token, at):
        self.token, self.created_at, self.block, self.tx_hash = token, at, 1, "0x" + "1" * 64
        self.creator, self.name, self.symbol, self.quote_token, self.extra = None, "N", "S", None, {}


class _Trade:
    def __init__(self, eid, token, at, quote):
        self.event_id, self.token, self.at, self.quote_amount = eid, token, at, quote
        self.trader, self.is_buy, self.token_amount, self.fee, self.block, self.tx_hash, self.extra = (
            "0x" + "2" * 40, True, 1, None, 1, "0x" + "3" * 64, {})


class _Adapter:
    def __init__(self, key):
        self.spec = LAUNCHPADS[key]


async def test_rollup_counts_only_new_events_and_drives_the_status(db):
    tok = "0x" + "a" * 40
    ad = _Adapter("pons_v1")
    res = _Res(100, launches=[_Launch(tok, NOW - timedelta(hours=3))],
               trades=[_Trade("e1", tok, NOW - timedelta(hours=2), 10 ** 18), _Trade("e2", tok, NOW - timedelta(hours=1), 5 * 10 ** 17)])
    await store.persist_scan(db, ad, res, NOW)
    await store.persist_scan(db, ad, res, NOW)  # a restart re-scanning the same blocks
    await db.commit()
    row = (await db.execute(select(LaunchpadActivity))).scalar_one()
    assert (row.launches, row.trades, row.volume) == (1, 2, 15 * 10 ** 17)

    spec = LAUNCHPADS["pons_v1"]
    st = await verification.status_for(db, None, spec, "PAPER", NOW)
    a = await activity.launchpad_activity(db, spec, "PAPER", st, NOW)
    assert a["activity_status"] == "ACTIVE" and a["listed"] and a["launches_7d"] == 1 and a["trades_7d"] == 2
    assert a["volume_7d"] == "1.5" and a["last_trade"] == NOW - timedelta(hours=1)
    assert a["buy_verified"] is False and a["execution_verified"] is False

    # 9 days later, nothing new: inactive (8 days of rollup history), archived, adapter kept
    later = NOW + timedelta(days=9)
    await store.set_cursor(db, "robinhood", "pons_v1", 200, later)
    await db.commit()
    a = await activity.launchpad_activity(db, spec, "PAPER", st, later)
    assert a["activity_status"] == "INACTIVE" and not a["listed"]

    # activity returns: back to ACTIVE with no operator action
    await store.persist_scan(db, ad, _Res(300, trades=[_Trade("e3", tok, later - timedelta(minutes=5), 1)]), later)
    await db.commit()
    a = await activity.launchpad_activity(db, spec, "PAPER", st, later)
    assert a["activity_status"] == "ACTIVE" and a["listed"]


async def test_a_venue_never_seen_active_is_unverified_until_seven_days_of_monitoring(db):
    spec = LAUNCHPADS["pons_v1"]
    await verification.record(db, "pons_v1", "ACTIVE", True, {}, "launchpad_verify", NOW - timedelta(days=1))
    await verification.record(db, "pons_v1", "DISCOVERY", False, {"launches": 0}, "launchpad_verify",
                              NOW - timedelta(days=1))
    await db.commit()
    a = await activity.launchpad_activity(db, spec, "PAPER", None, NOW)
    assert a["activity_status"] == "UNVERIFIED" and not a["listed"] and a["last_launch"] is None
    a = await activity.launchpad_activity(db, spec, "PAPER", None, NOW + timedelta(days=7))
    assert a["activity_status"] == "INACTIVE"


async def test_volume_counts_native_quote_trades_only(db):
    """Genius.fun pairs most launches with tokenized stocks: those amounts are
    in the stock token's units, so they count as trades but not as volume."""
    tok = "0x" + "b" * 40
    ad = _Adapter("genius_fun")
    stock = _Trade("g1", tok, NOW - timedelta(hours=1), 7 * 10 ** 18)
    stock.extra = {"native_quote": False}
    native = _Trade("g2", tok, NOW - timedelta(hours=1), 10 ** 18)
    native.extra = {"native_quote": True}
    unknown = _Trade("g3", tok, NOW - timedelta(hours=1), 2 * 10 ** 18)  # pair not known: counted as before
    await store.persist_scan(db, ad, _Res(10, trades=[stock, native, unknown]), NOW)
    await db.commit()
    row = (await db.execute(select(LaunchpadActivity))).scalar_one()
    assert (row.chain, row.trades, row.volume) == ("bsc", 3, 3 * 10 ** 18)
