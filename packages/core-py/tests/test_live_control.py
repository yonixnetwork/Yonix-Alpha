"""Dashboard-managed live control: every mandatory check is evaluated on
the server and named when it fails, the operating state is reported in plain
words, a failing or stale check stops new LIVE orders, a disarmed or killed
system never sends a queued BUY, and exits are never held."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import events, kill_switch, live_control as lc, live_trading, scanner_health  # noqa: E402
from yonixalpha_core.config import Settings  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    ExecutionOrder, PaperPosition, PlatformSetting, RiskSettingsVersion, StrategyConfig,
)
from yonixalpha_core.safety.settings import default_settings_for, settings_to_dict  # noqa: E402

SECRET = json.dumps(list(bytes(Keypair())))


def app_settings(open_=True):
    return Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x", TRADING_ENABLED=True, LIVE_TRADING_ENABLED=open_,
                    PAPER_TRADING=not open_, WALLET_PRIVATE_KEY=SECRET, SOLANA_RPC_URL="https://rpc.example",
                    SOLANA_WS_URL="wss://rpc.example")


@pytest.fixture
async def env(monkeypatch):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    redis = from_url(os.environ["REDIS_URL"])
    await redis.flushdb()

    async def healthy(db, r, now=None):
        return {"overall": "CONNECTED", "stages": [], "rpc_last_hour": {"rate_limited_share": 0.01}}

    monkeypatch.setattr(scanner_health, "report", healthy)
    yield make_session_factory(engine), redis
    await redis.flushdb()
    await redis.aclose()
    await engine.dispose()


async def ready(sf, redis, *, genesis=lc.MAINNET_GENESIS, sol="1", heartbeat=True, max_loss="0.01", mode="AUTO"):
    now = datetime.now(timezone.utc)
    await redis.set(live_trading.READY_KEY, json.dumps({
        "status": "ready", "reason": None, "at": now.isoformat(), "wallet_max_age_seconds": "120",
        "min_sol_reserve": "0.05", **({"genesis_hash": genesis} if genesis else {})}))
    await redis.set(live_trading.WALLET_KEY, json.dumps({"sol": sol, "at": now.isoformat()}))
    if heartbeat:
        await events.heartbeat(redis, "decision-engine")
    s = settings_to_dict(default_settings_for("GLOBAL"))
    s.update({"max_loss_per_trade_sol": max_loss, "max_position_size_quote": "0.1"})
    async with sf() as db:
        db.add(RiskSettingsVersion(scope="GLOBAL", version=1, settings=s))
        db.add(StrategyConfig(strategy="solana_fresh", mode=mode, config={}))
        for other in ("solana_migration", "solana_momentum"):
            db.add(StrategyConfig(strategy=other, mode="OFF", config={}))
        await db.commit()


def failed(ev):
    return [c["code"] for c in ev["checks"] if c["status"] == lc.FAIL]


@pytest.mark.asyncio
async def test_a_closed_ceiling_blocks_live_and_states_are_named(env):
    sf, redis = env
    async with sf() as db:
        db.add(StrategyConfig(strategy="solana_fresh", mode="PAPER", config={}))
        await db.commit()
        o = await lc.overview(db, redis, app_settings(open_=False))
    assert "ceiling" in failed(o) and o["readiness_ok"] is False and o["state"] == "PAPER"
    assert "LIVE is blocked by" in o["summary"] and "Server ceiling" in o["summary"]
    ceiling = next(c for c in o["checks"] if c["code"] == "ceiling")
    assert "PAPER_TRADING=false" in ceiling["action"] and SECRET not in json.dumps(o)
    async with sf() as db:
        (await db.get(StrategyConfig, "solana_fresh")).mode = "OFF"
        db.add_all([StrategyConfig(strategy=e, mode="OFF", config={}) for e in ("solana_migration", "solana_momentum")])
        await db.commit()  # a strategy without a row is PAPER: all three must be OFF
        assert (await lc.overview(db, redis, app_settings(open_=False)))["state"] == "OBSERVATION_ONLY"


@pytest.mark.asyncio
async def test_every_check_passes_only_when_everything_is_in_place(env):
    sf, redis = env
    await ready(sf, redis)
    async with sf() as db:
        ev = await lc.evaluate(db, redis, app_settings())
        assert failed(ev) == [] and ev["arming_ok"] and ev["readiness_ok"]
        db.add(PlatformSetting(key="global_mode", value={"mode": "LIVE"}))
        await db.commit()
        assert (await lc.overview(db, redis, app_settings()))["state"] == "LIVE_ELIGIBLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("change, code", [
    ({"genesis": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"}, "network"),  # devnet
    ({"genesis": None}, "network"),  # not verified is not a pass
    ({"sol": "0.06"}, "balance"),
    ({"heartbeat": False}, "risk_engine"),
    ({"max_loss": "0"}, "sizing"),
    ({"mode": "PAPER"}, "strategy_selected"),
])
async def test_each_missing_condition_is_named(env, change, code):
    sf, redis = env
    await ready(sf, redis, **change)
    async with sf() as db:
        ev = await lc.evaluate(db, redis, app_settings())
    assert code in failed(ev) and not ev["arming_ok"]


@pytest.mark.asyncio
async def test_unresolved_orders_faults_and_the_kill_switch_block(env):
    sf, redis = env
    await ready(sf, redis)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with sf() as db:
        db.add(ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="M", provider="pumpportal_local", route="pump",
                              amount="0.01", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal(0),
                              idempotency_key="k-stuck", status="SUBMITTED",
                              updated_at=old))
        db.add(PaperPosition(symbol="T", provider="live", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                             take_profit=[], status="needs_review", execution_mode="LIVE", engine="solana_fresh",
                             entry_at=old))
        await db.commit()
        await kill_switch.engage(redis, "test")
        ev = await lc.evaluate(db, redis, app_settings())
    assert {"duplicate_protection", "critical_faults", "kill_switch"} <= set(failed(ev))


@pytest.mark.asyncio
async def test_failing_or_stale_checks_stop_new_live_orders(env):
    sf, redis = env
    await ready(sf, redis)
    s = app_settings()
    ok, why = await live_trading.live_readiness(redis, s)
    assert not ok and "not run yet" in why  # nothing passes before the worker has checked
    async with sf() as db:
        rec = await lc.publish_enforcement(db, redis, s)
    assert rec["ok"] and (await live_trading.live_readiness(redis, s)) == (True, None)
    later = datetime.now(timezone.utc) + timedelta(seconds=lc.CONTROL_TTL + 5)
    assert (await lc.enforcement_ok(redis, later))[1] == "live checks are stale"
    await redis.delete(events.HEARTBEAT_PREFIX + "decision-engine")
    async with sf() as db:
        assert not (await lc.publish_enforcement(db, redis, s))["ok"]
    ok, why = await live_trading.live_readiness(redis, s)
    assert not ok and "risk_engine" in why


@pytest.mark.asyncio
async def test_a_queued_buy_is_held_unless_live_is_armed_but_a_smoke_buy_runs_under_paper(env):
    sf, redis = env
    await ready(sf, redis)
    s = app_settings()
    buy = ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="M", provider="pumpportal_local", route="pump",
                         amount="0.01", amount_kind="sol", idempotency_key="k1", status="PENDING")
    smoke = ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="M", provider="pumpportal_local", route="pump",
                           amount="0.01", amount_kind="sol", idempotency_key="k2", status="PENDING",
                           diagnostics={"smoke_test_run": "run-1"})
    async with sf() as db:
        assert "not run yet" in await live_trading.buy_block_reason(db, redis, buy)
        await lc.publish_enforcement(db, redis, s)
        assert "not LIVE" in await live_trading.buy_block_reason(db, redis, buy)  # global PAPER: disarmed
        assert await live_trading.buy_block_reason(db, redis, smoke) is None
        db.add(PlatformSetting(key="global_mode", value={"mode": "LIVE"}))
        await db.commit()
        assert await live_trading.buy_block_reason(db, redis, buy) is None
        await kill_switch.engage(redis, "test")
        assert await live_trading.buy_block_reason(db, redis, buy) == "kill switch engaged"


@pytest.mark.asyncio
async def test_configuration_is_judged_as_it_would_be_with_live_on(env):
    sf, redis = env
    await ready(sf, redis)
    s = app_settings().model_copy(update={"SOLANA_WS_URL": None})
    async with sf() as db:
        ev = await lc.evaluate(db, redis, s)
    cfg = next(c for c in ev["checks"] if c["code"] == "configuration")
    assert cfg["status"] == lc.FAIL and "SOLANA_WS_URL" in cfg["detail"] and not ev["arming_ok"]
