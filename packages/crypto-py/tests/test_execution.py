"""Executor lifecycle against the fake Binance and Bybit spot exchanges
(tests/fake_exchanges.py), through the real signing client and adapters.
Each exchange is exercised on its own; behaviours that differ (exchange-held
target on Binance, in-place stop amend on Bybit) are asserted separately."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from yonixalpha_crypto.db import ApiConnection, Event, ExecGuard, ExecOrder, ExecTrade, TradeAction
from yonixalpha_crypto.private import livelock, management
from yonixalpha_crypto.private.adapters import account_for
from yonixalpha_crypto.private.executor import EntryRefused, Executor
from yonixalpha_crypto.private.signedhttp import SignedHttp
from yonixalpha_crypto.private.sizing import SizingRules

from tests.fake_exchanges import FakeBinance, FakeBybit

RULES = SizingRules(risk_pct=Decimal(1), max_loss_quote=Decimal(10), max_position_quote=Decimal(500),
                    taker_fee_bps=Decimal(10), slippage_bps=Decimal(5), stop_limit_gap_bps=Decimal(20))
KEY, SECRET = "aaaaaaaabbbbbbbb0001", "ccccccccdddddddd0001"  # fake values


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


async def nosleep(_):
    return None


async def make(sf, exchange, environment="TESTNET", **fake_kw):
    fake = (FakeBinance if exchange == "binance" else FakeBybit)(**fake_kw)
    async with sf() as s:
        c = ApiConnection(id=uuid.uuid4(), exchange=exchange, environment=environment, label=uuid.uuid4().hex[:8], key_enc="x",
                          secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…0001", enabled=True,
                          status="OK", created_at=Clock()(), updated_at=Clock()())
        s.add(c)
        await s.commit()
    http = SignedHttp(exchange, environment, KEY, SECRET, transport=fake.transport())
    clock = Clock()
    ex = Executor(account_for(http), c, clock=clock, sleep=nosleep, poll_attempts=2)
    return fake, ex, c, clock


async def open_default(s, ex, **kw):
    args = dict(strategy_id="test", symbol="BTC/USDT", entry_key=kw.pop("entry_key", "test:BTC/USDT:1"), rules=RULES,
                stop_price=Decimal("98"), take_profit=Decimal("105"), plan=kw.pop("plan", None),
                thesis={"reason": "test entry", "regime": "trend"})
    args.update(kw)
    return await ex.open_long(s, **args)


async def actions(s, trade):
    return [a.action for a in (await s.execute(select(TradeAction).where(TradeAction.trade_id == trade.id)
                                                .order_by(TradeAction.id))).scalars()]


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_entry_is_sized_by_risk_and_protection_is_verified_on_the_exchange(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex)
        assert t.status == "OPEN" and t.protection == "VERIFIED"
        assert t.planned_loss <= t.risk_budget == Decimal(10)  # 1% of 1000 USDT equity, capped at 10
        assert t.qty == t.qty_planned * (1 - Decimal("0.001"))  # the buy fee was paid in BTC
        assert fake.order_count("BUY") == 1
        open_prot = [o for o in fake.orders.values() if o["status"] in ("NEW", "Untriggered")]
        if exchange == "binance":  # stop and target both held by the exchange (OCO)
            assert {o["kind"] for o in open_prot} == {"STOP_LOSS_LIMIT", "LIMIT_MAKER"}
            assert t.plan["engine_take_profit"] is False
        else:  # Bybit spot: stop on the exchange, target managed by the engine
            assert [o["kind"] for o in open_prot] == ["TPSL"]
            assert t.plan["engine_take_profit"] is True
        assert open_prot[0]["qty"] <= t.qty
        acts = await actions(s, t)
        assert acts[:3] == ["ENTRY_PLANNED", "ENTRY_FILLED", "PROTECTION_VERIFIED"]


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_stop_fill_is_found_by_reconciliation_and_the_loss_stays_inside_the_budget(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex)
        fake.trigger("97.9")
        await ex.reconcile(s)
        await s.refresh(t)
        assert t.status == "CLOSED" and "stop filled" in t.close_reason
        assert t.realized_quote < 0 and -t.realized_quote <= t.risk_budget
        if exchange == "binance":
            assert any(o["status"] == "EXPIRED" and o["kind"] == "LIMIT_MAKER" for o in fake.orders.values())


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_entry_with_no_answer_blocks_entries_and_is_recovered_without_a_duplicate(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        fake.timeout_next = True  # executed by the exchange, answer lost
        t = await open_default(s, ex)
        assert t.status == "PENDING_ENTRY"
        g = await s.get(ExecGuard, c.id)
        assert g.entries_blocked
        with pytest.raises(EntryRefused, match="blocked"):
            await open_default(s, ex, entry_key="test:BTC/USDT:2")
        # restart: a fresh executor reconciles from the exchange's state
        fresh = Executor(ex.acct, c, clock=ex.clock, sleep=nosleep, poll_attempts=2)
        out = await fresh.reconcile(s)
        await s.refresh(t)
        assert t.status == "OPEN" and t.protection == "VERIFIED", out
        assert fake.order_count("BUY") == 1  # never sent twice
        await fresh.reconcile(s)
        assert (await s.get(ExecGuard, c.id)).entries_blocked is False


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_a_request_that_never_arrived_fails_the_trade_only_after_the_grace_period(sf, exchange):
    fake, ex, c, clock = await make(sf, exchange)
    async with sf() as s:
        fake.drop_next = True
        t = await open_default(s, ex)
        assert t.status == "PENDING_ENTRY"
        await ex.reconcile(s)
        await s.refresh(t)
        assert t.status == "PENDING_ENTRY" and (await s.get(ExecGuard, c.id)).entries_blocked
        clock.t += timedelta(seconds=61)
        await ex.reconcile(s)
        await ex.reconcile(s)
        await s.refresh(t)
        assert t.status == "FAILED" and fake.order_count("BUY") == 0
        assert (await s.get(ExecGuard, c.id)).entries_blocked is False


@pytest.mark.parametrize("exchange,code", [("binance", -2010), ("bybit", 170131)])
async def test_a_rejected_entry_fails_cleanly(sf, exchange, code):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        fake.reject_next = (code, "Insufficient balance.")
        t = await open_default(s, ex)
        assert t.status == "FAILED" and "rejected" in t.close_reason
        assert (await s.get(ExecGuard, c.id)).entries_blocked is False
        assert fake.order_count("BUY") == 0


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_a_partial_entry_protects_exactly_what_was_bought(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        fake.fill_ratio = Decimal("0.6")
        t = await open_default(s, ex)
        assert t.status == "OPEN" and t.protection == "VERIFIED"
        assert t.qty < t.qty_planned * Decimal("0.61")
        stop = next(o for o in fake.orders.values() if o["kind"] in ("STOP_LOSS_LIMIT", "TPSL"))
        assert stop["qty"] <= t.qty
        assert "partial fill" in " ".join(a.reason for a in (await s.execute(
            select(TradeAction).where(TradeAction.trade_id == t.id))).scalars())


@pytest.mark.parametrize("exchange,code", [("binance", -1013), ("bybit", 170140)])
async def test_a_rejected_stop_closes_the_position_and_blocks_entries(sf, exchange, code):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        orig = fake.route

        def reject_protection(r):
            if r.method == "POST" and (r.url.path == "/api/v3/orderList/oco" or b"tpslOrder" in (r.content or b"")):
                if exchange == "binance":
                    return httpx.Response(400, json={"code": code, "msg": "Filter failure: PRICE_FILTER"})
                return fake.err(code, "Order price is out of permissible range.")
            return orig(r)

        fake.route = reject_protection
        t = await open_default(s, ex)
        assert t.status == "CLOSED" and "protection could not be confirmed" in t.close_reason
        assert fake.order_count("SELL") == 1
        assert {"PROTECTION_REJECTED", "PROTECTION_MISSING", "CLOSED"} <= set(await actions(s, t))
        n_err = await s.scalar(select(func.count()).select_from(Event).where(Event.level == "ERROR"))
        assert n_err >= 1


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_protection_removed_by_the_exchange_is_detected_and_replaced(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex)
        fake.exchange_cancels_protection()
        await ex.reconcile(s)
        await s.refresh(t)
        assert t.status == "OPEN" and t.protection == "VERIFIED" and t.protection_seq == 2
        assert "PROTECTION_LOST" in await actions(s, t)


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_restart_never_duplicates_a_signal_or_a_symbol(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        await open_default(s, ex)
    fresh = Executor(ex.acct, c, clock=ex.clock, sleep=nosleep, poll_attempts=2)
    async with sf() as s:
        await fresh.reconcile(s)
        with pytest.raises(EntryRefused, match="already"):
            await open_default(s, fresh)
        with pytest.raises(EntryRefused, match="already active"):
            await open_default(s, fresh, entry_key="test:BTC/USDT:other")
    assert fake.order_count("BUY") == 1


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_coins_missing_from_the_account_make_the_trade_uncertain(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex)
        fake.free["BTC"] = Decimal(0)
        fake.locked["BTC"] = t.qty / 2  # someone sold part of it outside YonixAlpha
        out = await ex.reconcile(s)
        await s.refresh(t)
        assert t.status == "UNCERTAIN" and out["problems"]
        assert (await s.get(ExecGuard, c.id)).entries_blocked


async def test_binance_trailing_replaces_the_oco_and_bybit_amends_in_place(sf):
    plan = {"trailing": {"method": "percent", "activate_r": 1, "percent": 1}}
    for exchange in ("binance", "bybit"):
        fake, ex, c, _ = await make(sf, exchange)
        async with sf() as s:
            t = await open_default(s, ex, plan=plan, entry_key=f"trail:{exchange}")
            first_stop = t.stop
            fake.trigger("103.5")  # about +1.6 R
            ds = await ex.manage(s, t, management.MarketSnapshot(fake.bid, fake.ask), 3, Decimal(10))
            assert [d.action for d in ds] == ["MOVE_STOP"]
            await s.refresh(t)
            assert t.stop > first_stop and t.stop <= fake.bid * Decimal("0.99") and t.protection == "VERIFIED"
            stops = [o for o in fake.orders.values() if o["kind"] in ("STOP_LOSS_LIMIT", "TPSL")]
            if exchange == "binance":
                assert [o["status"] for o in stops] == ["CANCELED", "NEW"] and t.protection_seq == 2
            else:
                assert len(stops) == 1 and stops[0]["stop"] == t.stop and t.protection_seq == 1
            # a lower candidate never widens the stop
            fake.trigger("102")
            ds = await ex.manage(s, t, management.MarketSnapshot(fake.bid, fake.ask), 4, Decimal(10))
            assert all(d.action != "MOVE_STOP" for d in ds)


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_partial_exit_sells_part_and_reprotects_the_rest_at_breakeven(sf, exchange):
    plan = {"partials": [{"at_r": 1, "fraction": 0.5, "stop_to": "breakeven"}]}
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex, plan=plan, take_profit=Decimal("110"))
        held = t.qty
        fake.trigger("102.6")
        ds = await ex.manage(s, t, management.MarketSnapshot(fake.bid, fake.ask), 2, Decimal(10))
        assert [d.action for d in ds] == ["PARTIAL_EXIT", "MOVE_STOP"]
        await s.refresh(t)
        assert t.qty < held and t.realized_quote > 0 and t.plan["partials"][0]["done"]
        assert t.stop >= t.entry_price and t.protection == "VERIFIED"
        live_stop = [o for o in fake.orders.values() if o["kind"] in ("STOP_LOSS_LIMIT", "TPSL")
                     and o["status"] in ("NEW", "Untriggered")]
        assert len(live_stop) == 1 and live_stop[0]["qty"] <= t.qty


async def test_bybit_engine_managed_target_and_strategy_invalidation_close_the_trade(sf):
    fake, ex, c, _ = await make(sf, "bybit")
    async with sf() as s:
        t = await open_default(s, ex)
        fake.trigger("105.2")
        ds = await ex.manage(s, t, management.MarketSnapshot(fake.bid, fake.ask), 5, Decimal(10))
        assert ds[0].action == "CLOSE" and "engine-managed target" in ds[0].reason
        await s.refresh(t)
        assert t.status == "CLOSED" and t.realized_quote > 0
    fake, ex, c, _ = await make(sf, "binance")
    async with sf() as s:
        t = await open_default(s, ex, entry_key="inv")
        ds = await ex.manage(s, t, management.MarketSnapshot(fake.bid, fake.ask, strategy_exit="EMA 9 crossed below EMA 21"),
                             1, Decimal(10))
        await s.refresh(t)
        assert t.status == "CLOSED" and "EMA 9 crossed below" in t.close_reason
        assert fake.order_count("SELL") == 1


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_live_connections_cannot_enter_while_the_code_lock_is_on(sf, exchange):
    assert livelock.LIVE_ORDERS_UNLOCKED is False
    fake, ex, c, _ = await make(sf, exchange, environment="LIVE")
    async with sf() as s:
        with pytest.raises(livelock.LiveLocked, match="code_lock"):
            await open_default(s, ex)
        assert await s.scalar(select(func.count()).select_from(ExecTrade)) == 0
        assert await s.scalar(select(func.count()).select_from(ExecOrder)) == 0
    assert fake.requests == []


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_entries_refused_before_anything_is_sent(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange, usdt="3")
    async with sf() as s:
        with pytest.raises(EntryRefused, match="below the minimum"):
            await open_default(s, ex)  # 3 USDT cannot buy the 5 USDT minimum
    assert all(r.method == "GET" for r in fake.requests)
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        with pytest.raises(EntryRefused, match="not above the ask"):
            await open_default(s, ex, take_profit=Decimal("99"), entry_key="k2")
        with pytest.raises(EntryRefused, match="plan"):
            await open_default(s, ex, plan={"trailing": {"method": "martingale"}}, entry_key="k3")
    assert all(r.method == "GET" for r in fake.requests)


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_an_exit_with_no_answer_is_resumed_and_booked_once(sf, exchange):
    fake, ex, c, _ = await make(sf, exchange)
    async with sf() as s:
        t = await open_default(s, ex)
        fake.timeout_next = True  # the market SELL executes, its answer is lost
        await ex.close(s, t, "operator test")
        await s.refresh(t)
        assert t.status == "CLOSING" and (await s.get(ExecGuard, c.id)).entries_blocked
    fresh = Executor(ex.acct, c, clock=ex.clock, sleep=nosleep, poll_attempts=2)
    async with sf() as s:
        await fresh.reconcile(s)
        await fresh.reconcile(s)  # duplicate reconciliation changes nothing
        t = await s.get(ExecTrade, t.id)
        assert t.status == "CLOSED" and t.qty < Decimal("0.00001")  # only dust below the exchange step
        sells = [o for o in fake.orders.values() if o["side"] == "SELL" and o["kind"] == "MARKET"]
        assert len(sells) == 1
        expected = sells[0]["filled"] * sells[0]["avg"] - sells[0]["fee"] - Decimal(t.thesis["cost_per_unit"]) * sells[0]["filled"]
        assert abs(t.realized_quote - expected) < Decimal("1e-12")  # booked exactly once (DB keeps 18 decimals)
        assert (await s.get(ExecGuard, c.id)).entries_blocked is False


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_the_worker_pass_recovers_and_manages_from_the_saved_encrypted_keys(sf, exchange):
    from yonixalpha_crypto.engine import Engine
    from yonixalpha_crypto.private import secretstore
    from yonixalpha_crypto.private.service import run_exchange_accounts

    env = {"JWT_SECRET": "w" * 40}
    fake, ex, c, clock = await make(sf, exchange)
    async with sf() as s:
        row = await s.get(ApiConnection, c.id)
        row.key_enc, row.secret_enc = secretstore.encrypt(KEY, env), secretstore.encrypt(SECRET, env)
        await s.commit()
        t = await open_default(s, ex)
    assert Engine.idle({"state": "DISABLED", "open_positions": 0, "exchange_trades": 1}) is False
    fake.trigger("97.5")
    out = await run_exchange_accounts(sf, clock(), transport=fake.transport(), env=env)
    assert out[str(c.id)]["reconciled"]["trades"] == 1
    async with sf() as s:
        assert (await s.get(ExecTrade, t.id)).status == "CLOSED"
    # keys that cannot be decrypted: entries blocked, nothing guessed
    fake2, ex2, c2, _ = await make(sf, exchange)
    async with sf() as s:
        await open_default(s, ex2, entry_key="k-unreadable")
    out = await run_exchange_accounts(sf, clock(), transport=fake2.transport(), env={"JWT_SECRET": "z" * 40})
    assert out[str(c2.id)] == "keys unreadable"
    async with sf() as s:
        assert (await s.get(ExecGuard, c2.id)).entries_blocked


@pytest.mark.parametrize("exchange", ["binance", "bybit"])
async def test_practice_lifecycle_tool_end_to_end_against_the_simulated_exchange(sf, exchange, capsys):
    """The operator's tool, run against the simulated exchange: every step
    must PASS. (On the server it runs against the real practice host.)"""
    import os

    from yonixalpha_crypto.private import connections, practice_lifecycle

    env = {"JWT_SECRET": "p" * 40}
    fake = (FakeBinance if exchange == "binance" else FakeBybit)()
    async with sf() as s:
        await connections.create(s, exchange=exchange, environment="TESTNET", label=f"lifecycle-{exchange}",
                                 api_key=KEY, api_secret=SECRET, actor="test", env=env)
        await s.commit()
    code = await practice_lifecycle.run(f"lifecycle-{exchange}", "BTC/USDT", Decimal(200), Decimal(2),
                                        transport=fake.transport(), env=env, database_url=os.environ["DATABASE_URL"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[FAIL]" not in out and out.count("[PASS]") >= 11
    assert fake.order_count("BUY") == 1  # the rejected probe never became an order
    assert ("replaced" if exchange == "binance" else "amended in place") in out
    async with sf() as s:
        live = await connections.create(s, exchange=exchange, environment="LIVE", label=f"live-{exchange}",
                                        api_key="eeeeeeeeffffffff0009", api_secret=SECRET + "x", actor="test", env=env)
        await s.commit()
    assert await practice_lifecycle.run(str(live.id), "BTC/USDT", Decimal(20), Decimal(2), transport=fake.transport(),
                                        env=env, database_url=os.environ["DATABASE_URL"]) == 2
    assert "REFUSED" in capsys.readouterr().out
