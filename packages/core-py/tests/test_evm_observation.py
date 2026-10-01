"""EVM observation (master §14-17): a token is observed once per category,
snapshotted at T0..T+60 with the §16 fields it can measure (unmeasured ones
None with a reason), expired as EXPIRED_NO_ENTRY when nothing qualified,
kept open while a MIGRATED / MOMENTUM token is still trading, and rejected
only after repeated safety failures."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.chains.evm import observation as ob  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EvmObservation, EvmToken, EvmTrade, WalletProfile  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TOKEN = "0x" + "1" * 40
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


def trade(n, sec, buyer, buy=True, quote=0.1, tokens=10 ** 24):
    return EvmTrade(event_id=f"e{n}", chain="bsc", launchpad="flap", token=TOKEN, trader=buyer, is_buy=buy,
                    token_amount=Decimal(tokens), quote_amount=Decimal(int(quote * E18)), at=T0 + timedelta(seconds=sec))


async def obs_of(db, category="FRESH") -> EvmObservation:
    return (await db.execute(select(EvmObservation).where(EvmObservation.category == category))).scalar_one()


async def seed(db, category="FRESH"):
    db.add(EvmToken(chain="bsc", token=TOKEN, launchpad="flap", creator="0x" + "c" * 40, created_at=T0, created_block=1,
                    venue={}, stats={}, extra={"launch_seen": True}, category=category, stage="CURVE",
                    state={"liquidity_quote": "1.5", "progress": "0.2"}, safety_verdict="PASS", safety_at=T0,
                    coordination={"status": "COORDINATION_DETECTED", "action": "REDUCE_SIZE", "facts": {"supply": str(10 ** 27)},
                                  "wallets": [{"wallet": "0x" + "b" * 40, "roles": ["CREATOR_LINKED"]}]}))
    db.add(WalletProfile(chain="bsc", wallet="0x" + "5" * 40, metrics={"discovery": {"stage": "VALIDATED"}}, labels=[],
                         source="evm_trades", trades=30, tokens=9, last_seen=T0))
    await ob.ensure(db, "bsc", TOKEN, category, T0, "launch observed", ob.ObservationConfig())
    await db.commit()


async def test_snapshots_and_expiry_without_an_entry(db):
    await seed(db)
    db.add_all([trade(1, 30, "0x" + "b" * 40), trade(2, 90, "0x" + "5" * 40), trade(3, 200, "0x" + "6" * 40, quote=0.3),
                trade(4, 250, "0x" + "6" * 40, buy=False, quote=0.2, tokens=5 * 10 ** 23)])
    await db.commit()
    cfg = ob.ObservationConfig()
    await ob.ensure(db, "bsc", TOKEN, "FRESH", T0 + timedelta(minutes=9), "again", cfg)  # once per category
    await db.commit()
    assert len((await db.execute(select(EvmObservation))).scalars().all()) == 1

    out = await ob.step(db, "bsc", T0 + timedelta(minutes=7), cfg)
    await db.commit()
    o = await obs_of(db)
    assert out["snapshots"] == 2 and set(o.snapshots) == {"T0", "T+5"} and o.state == ob.OBSERVING
    s5 = o.snapshots["T+5"]
    assert s5["buyers"] == 3 and s5["sellers"] == 1 and s5["smart_money_buyers"] == 1
    assert s5["effective_buyers"] == 2  # the creator-linked wallet is not organic demand
    assert s5["buy_volume"] == "0.5" and s5["sell_volume"] == "0.2" and s5["holders"] == 3
    assert Decimal(s5["net_flow"]) == Decimal("0.4286") and s5["organic_net_flow"] == "0.3333"
    assert s5["market_cap"] is not None and s5["ml"] is None and "Solana only" in s5["ml_note"]
    assert s5["top_buyer_share"] == "0.6000" and s5["safety"] == "PASS"

    ob.record_entry_decision(o, {"decision": "NO_TRADE", "blockers": [{"code": "TOO_FEW_BUYERS"}]}, False, T0 + timedelta(minutes=8))
    assert o.state == ob.ANALYZING
    await db.commit()
    await ob.step(db, "bsc", T0 + timedelta(minutes=61), cfg)
    await db.commit()
    o = await obs_of(db)
    assert o.state == ob.EXPIRED and o.expiry_reason == "EXPIRED_NO_ENTRY" and "TOO_FEW_BUYERS" in o.reason
    assert set(o.snapshots) == {"T0", "T+5", "T+10", "T+20", "T+30", "T+60"}
    assert [h["state"] for h in o.history][-2:] == [ob.NO_ENTRY, ob.EXPIRED]
    ob.record_entry_decision(o, {"decision": "PAPER_BUY", "blockers": []}, True, T0 + timedelta(minutes=62))
    assert o.state == ob.EXPIRED  # terminal: an expired observation is never entered


async def test_qualified_waiting_and_entered(db):
    await seed(db)
    o = await obs_of(db)
    now = T0 + timedelta(minutes=2)
    ob.record_entry_decision(o, {"decision": "NO_TRADE", "blockers": [{"code": "MAX_OPEN_POSITIONS"}]}, False, now)
    assert o.state == ob.WAITING and "MAX_OPEN_POSITIONS" in o.reason
    ob.record_entry_decision(o, {"decision": "PAPER_BUY", "blockers": []}, True, now)
    assert o.state == ob.ENTERED and o.decided_at == now
    assert [h["state"] for h in o.history] == [ob.DISCOVERED, ob.OBSERVING, ob.QUALIFIED, ob.WAITING, ob.PENDING, ob.ENTERED]


async def test_active_migrated_window_extends_and_repeated_safety_failure_rejects(db):
    await seed(db, "MIGRATED")
    db.add_all([trade(i, 3600 + i * 20, f"0x{i:040x}") for i in range(1, 8)])  # busy at the deadline
    await db.commit()
    cfg = ob.ObservationConfig()
    out = await ob.step(db, "bsc", T0 + timedelta(minutes=62), cfg)
    await db.commit()
    o = await obs_of(db, "MIGRATED")
    assert out["extended"] == 1 and o.state == ob.OBSERVING and o.deadline == T0 + timedelta(minutes=70)

    tok = await db.get(EvmToken, ("bsc", TOKEN))
    for i in range(3):
        tok.safety_verdict, tok.safety_at = "FAIL", T0 + timedelta(minutes=63 + i)
        tok.safety = {"findings": [{"level": "FAIL", "code": "NOT_SELLABLE"}]}
        await db.commit()
        await ob.step(db, "bsc", T0 + timedelta(minutes=63 + i), cfg)
        await db.commit()
        o = await obs_of(db, "MIGRATED")
        assert o.state == (ob.SAFETY_FAILURE if i < 2 else ob.REJECTED)
    assert "NOT_SELLABLE" in o.reason and o.safety_failures == 3


def test_settings_are_validated():
    cfg, errors = ob.parse_config({"snapshots_min": [0, 2, 5, 60], "window_min": {"fresh": 30}})
    assert not errors and cfg.snapshots_min == (0, 2, 5, 60) and cfg.window_min["FRESH"] == 30
    _, errors = ob.parse_config({"snapshots_min": [5], "window_min": {"OTHER": 5}, "max_window_minutes": 10, "x": 1})
    assert len(errors) == 4
