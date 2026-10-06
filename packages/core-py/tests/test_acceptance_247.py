"""24/7 acceptance report (master §62, §81): continuity per duty from what the
workers wrote, measured on the real schema; a stopped source is FAIL, a quiet
market is NO EVENT, a long silence is GAP."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core.db import models  # noqa: F401
from yonixalpha_core.db.base import Base, make_session_factory
from yonixalpha_core.db.models import (EvmToken, EvmTrade, LaunchpadActivity, PaperPosition, SystemEvent,
                                       TokenObservation)
from yonixalpha_core.tools import acceptance_247 as acc

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=1)
M = timedelta(minutes=1)


class FakeRedis:
    def __init__(self, values):
        self.values = values

    async def mget(self, keys):
        return [self.values.get(k) for k in keys]

    async def get(self, key):
        return self.values.get(key)


def hb(age_s, status="ok"):
    return json.dumps({"at": (NOW - timedelta(seconds=age_s)).isoformat(), "status": status})


def test_verdicts():
    cont = acc.Duty("x", "x", "t", "c", max_gap_minutes=10)
    market = acc.Duty("y", "y", "t", "c", continuous=False, max_gap_minutes=10)
    assert acc.verdict(cont, 0, None) == "FAIL" and acc.verdict(market, 0, None) == "NO EVENT"
    assert acc.verdict(cont, 5, 601) == "GAP" and acc.verdict(cont, 5, 600) == "ACTIVE"
    assert acc.heartbeat_state(None, NOW) == ("NONE", None)
    assert acc.heartbeat_state(json.loads(hb(30)), NOW)[0] == "OK" and acc.heartbeat_state(json.loads(hb(91)), NOW)[0] == "STALE"
    assert acc.heartbeat_state(json.loads(hb(5, "disabled")), NOW)[0] == "DISABLED"


async def test_report_on_the_real_schema():
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with make_session_factory(engine)() as s:
            for i in range(-30, 60, 2):  # Solana: a launch every 2 minutes, half an hour of it before the window
                at = SINCE + i * M
                s.add(TokenObservation(mint=f"m{i}", outcome="NO_TRADE", report={}, launched_at=at, decided_at=at + M))
            for i in range(0, 60, 3):
                s.add(EvmToken(chain="bsc", token=f"0x{i:040x}", launchpad="fourmeme", created_at=SINCE + i * M))
            for i in [*range(0, 20), *range(40, 60)]:  # the BSC trade feed went silent for 20 minutes
                s.add(EvmTrade(event_id=f"t{i}", chain="bsc", launchpad="fourmeme", token="0x1", trader="0x2", is_buy=True,
                               token_amount=Decimal(1), quote_amount=Decimal(1), at=SINCE + i * M + 30 * timedelta(seconds=1)))
            s.add(PaperPosition(symbol="X", provider="paper", side="long", entry_price=Decimal(1), quantity=Decimal(1),
                                entry_at=SINCE + 5 * M, exit_at=SINCE + 50 * M, status="closed", realized_pnl=Decimal("-0.1")))
            s.add(SystemEvent(service="paper-trading", event_type="service_started", severity="info", detail={},
                              created_at=SINCE + 30 * M))
            for i in range(4):  # a crash loop: four starts in one hour
                s.add(SystemEvent(service="copy-engine", event_type="service_started", severity="info", detail={},
                                  created_at=SINCE + (10 + 12 * i) * M))
            # the daily rollup: one row per launchpad and day, overwritten on each write; flap's
            # last write was 50 minutes ago (it is quiet), fourmeme's 2 minutes ago
            s.add(LaunchpadActivity(chain="bsc", launchpad="flap", day=NOW.date(), updated_at=NOW - 50 * M))
            s.add(LaunchpadActivity(chain="bsc", launchpad="fourmeme", day=NOW.date(), updated_at=NOW - 2 * M))
            await s.commit()
            redis = FakeRedis({**{f"yx:hb:{svc}": hb(20) for svc in acc.SERVICES}, "yx:hb:data-evm": hb(400),
                               "yx:pm:last_pass": json.dumps({"at": NOW.isoformat()})})
            rep = await acc.collect(s, redis, SINCE, NOW, NOW)
    finally:
        await engine.dispose()

    by = {d["key"]: d for d in rep["duties"]}
    assert by["solana_discovery"]["verdict"] == "ACTIVE" and by["solana_discovery"]["count"] == 30
    assert by["solana_discovery"]["older"] == 15  # history before the window is still there
    assert by["bsc_discovery"]["verdict"] == "ACTIVE"
    assert by["bsc_trades"]["verdict"] == "GAP" and by["bsc_trades"]["longest_gap_s"] == 21 * 60
    assert by["robinhood_discovery"]["verdict"] == "FAIL" and by["robinhood_trades"]["verdict"] == "FAIL"
    assert by["copy_events"]["verdict"] == "NO EVENT"  # market-dependent: not a failure
    assert by["closed_positions"]["verdict"] == "ACTIVE" and by["closed_positions"]["count"] == 1
    assert rep["services"]["paper-trading"]["restarts"] == 1 and rep["services"]["data-evm"]["heartbeat"] == "STALE"
    assert rep["restart"]["live_reconciled_at"] is None and rep["position_loop"]["at"] == NOW.isoformat()
    # rows overwritten in place: only the newest write counts, the 48 minutes between the two rows are no silence
    assert by["launchpad_probe"]["verdict"] == "ACTIVE" and by["launchpad_probe"]["longest_gap_s"] == 2 * 60
    assert rep["services"]["copy-engine"]["restarts"] == 4

    out, ok = acc.render(rep)
    assert not ok and "RESULT: FAIL" in out and "data-evm" in out and "not running" in out
    assert "restarts 4   <- restarting repeatedly (more than 3)" in out and "restarts 1   <-" not in out
    assert "FAIL     discovering: Robinhood launches: 0 events" in out
    # with every continuous source producing and every service alive, GAP and NO EVENT do not fail the run
    for d in rep["duties"]:
        if d["verdict"] == "FAIL":
            d["verdict"] = "ACTIVE"
    rep["services"]["data-evm"]["heartbeat"] = "OK"
    out, ok = acc.render(rep)
    assert not ok and "restarting repeatedly" in out  # a fresh heartbeat does not hide a crash loop
    rep["services"]["copy-engine"]["restarts"] = 3  # a deliberate restart that came up twice is not one
    out, ok = acc.render(rep)
    assert ok and "RESULT: PASS" in out and "GAP" in out and "NO EVENT" in out
