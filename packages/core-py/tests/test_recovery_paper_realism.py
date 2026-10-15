"""R4 and R5 (2026-10-15): paper fills that a LIVE order could not have had.

R5: a PumpSwap paper buy lands like a LIVE one: filled at the recorded pool
price measured LIVE latency after the decision. When the pool record cannot
prove that price, the measured LIVE buy drift is charged instead (what pool
entries were charged before), never nothing.
R4: a paper position whose token migrates is not managed for the measured
LIVE sell latency, then sells at most one take-profit level per tick."""

from datetime import timedelta
from decimal import Decimal

from solders.pubkey import Pubkey

from yonixalpha_core import paper_engine
from yonixalpha_core import paper_execution as pe
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition
from yonixalpha_core.solana import pumpswap

from tests.test_paper_entry_delay import T, redis, sf  # noqa: F401 - fixtures

MINT = str(Pubkey.new_unique())
POOL = pumpswap.canonical_pool(MINT)
BASE = 10**15  # pool base reserve (raw)


def ptrade(sec: float, quote: int) -> pumpswap.PoolTrade:
    return pumpswap.PoolTrade(T + timedelta(seconds=sec), "u", True, 10**9, 10**8, BASE, quote, 125, POOL)


def pool_position(fallback: str | None = "2.5") -> PaperPosition:
    p = PaperPosition(symbol="P", provider="paper", side="LONG", entry_price=Decimal("0.0005"), quantity=Decimal(1000),
                      initial_quantity=Decimal(1000), remaining_quantity=Decimal(1000), take_profit=[], status="open",
                      entry_at=T, engine="solana_migration", asset_id=MINT, execution_mode="PAPER", lifecycle="MIGRATED",
                      entry_cost_quote=Decimal("0.5"), highest_price=Decimal("0.0005"), lowest_price=Decimal("0.0005"),
                      last_price=Decimal("0.0005"), plan={"venue": {"decimals": 6, "type": "pumpswap_pool"}})
    p.plan = pe.schedule_entry_delay(p.plan, T.timestamp(), 2.0, "measured LIVE", venue="pool",
                                     fallback_drift_pct=Decimal(fallback) if fallback else None)
    return p


# --- R5 -------------------------------------------------------------------------

async def test_a_pool_entry_is_refilled_at_the_recorded_pool_price_at_landing(sf, redis):  # noqa: F811
    # the gate's read at the decision, then the read position management makes after landing
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(-5, 500 * 10**9)], now=T.timestamp() - 0.3)
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(-5, 500 * 10**9), ptrade(1, 550 * 10**9)],
                                      now=T.timestamp() + 4)
    async with sf() as s:
        p = pool_position()
        s.add(p)
        await s.flush()
        assert pe.entry_delay_pending(p, T + timedelta(seconds=1))
        out = await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=5))
        await s.commit()
    assert out["measured"] and Decimal(out["factor"]) == Decimal("1.1") and out["price_source"] == "pumpswap pool trades"
    assert abs(p.quantity - Decimal(1000) / Decimal("1.1")) < Decimal("1e-9") and p.entry_cost_quote == Decimal("0.5")
    assert "charged_live_drift_pct" not in out  # one or the other, never both


async def test_unproven_pool_price_charges_the_measured_live_drift_instead(sf, redis):  # noqa: F811
    # only the gate's read exists: nothing proves the price at landing
    await pumpswap.record_pool_trades(redis, POOL, [ptrade(-5, 500 * 10**9)], now=T.timestamp() - 0.3)
    async with sf() as s:
        p = pool_position("2.5")
        s.add(p)
        await s.flush()
        out = await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=5))
    assert out["measured"] is False and out["charged_live_drift_pct"] == "2.5" and "not read" in out["reason"]
    assert abs(p.quantity - Decimal(1000) / Decimal("1.025")) < Decimal("1e-9")
    assert p.entry_price == Decimal("0.0005") * Decimal("1.025")  # never better than the decision fill


async def test_no_pool_record_and_no_measured_drift_keeps_the_decision_fill(sf, redis):  # noqa: F811
    async with sf() as s:
        p = pool_position(None)
        s.add(p)
        await s.flush()
        out = await pe.settle_entry_delay(s, redis, p, T + timedelta(seconds=5))
    assert out["measured"] is False and out["factor"] == "1" and p.quantity == Decimal(1000)
    assert "decision fill kept" in out["reason"]


# --- R4 -------------------------------------------------------------------------

async def test_sell_latency_is_measured_from_confirmed_live_sells(sf, redis):  # noqa: F811
    async with sf() as s:
        assert await pe.measured_sell_latency(s, None) == (3.0, "DEFAULT: 0 measured LIVE sells (need 5)")
        for i, secs in enumerate((2, 4, 6, 8, 40)):
            s.add(ExecutionOrder(idempotency_key=f"s{i}", mode="LIVE", side="SELL", reason="stop_loss", mint=MINT,
                                 provider="pumpportal", route="pump-amm", amount="1", amount_kind="tokens",
                                 slippage_pct=Decimal(5), priority_fee_sol=Decimal("0.0001"), status="CONFIRMED",
                                 created_at=T, confirmed_at=T + timedelta(seconds=secs)))
        await s.flush()
        lat, src = await pe.measured_sell_latency(s, redis)
    assert lat == 6.0 and src == "MEASURED: median of 5 confirmed LIVE sells"
    assert await redis.get(pe.SELL_LATENCY_CACHE) == "6.0|MEASURED: median of 5 confirmed LIVE sells"


def test_a_crossing_position_waits_for_the_sell_latency():
    p = PaperPosition(plan={})
    p.plan = pe.schedule_crossing(p.plan, T, 3.0, "DEFAULT")
    assert pe.crossing_pending(p, T + timedelta(seconds=2.9))
    assert not pe.crossing_pending(p, T + timedelta(seconds=3.1))
    assert not pe.crossing_pending(PaperPosition(plan={}), T)


def _state(crossing: bool) -> paper_engine.PositionState:
    tps = [{"price": {"value": "2"}, "exit_fraction": "0.4"}, {"price": {"value": "3"}, "exit_fraction": "0.3"},
           {"price": {"value": "4"}, "exit_fraction": "0.3"}]
    plan = {"take_profits": tps, **({pe.CROSSING_KEY: {"due_ts": 0}} if crossing else {})}
    p = PaperPosition(initial_quantity=Decimal(100), quantity=Decimal(100), remaining_quantity=Decimal(100),
                      stop_loss=Decimal("0.9"), plan=plan, tp_hits=[], side="LONG", trailing_stop=None,
                      highest_price=None, lowest_price=None)
    return paper_engine.state_of(p)


def test_after_a_migration_jump_take_profits_sell_one_level_per_tick():
    # STONKS / TAPE: the pool opens far above every take-profit level
    s = _state(crossing=True)
    first = paper_engine.manage_step(s, Decimal("6"))
    assert [r for _, r in first.exits] == ["take_profit_1"]
    second = paper_engine.manage_step(s, Decimal("5"))  # the next tick's price
    assert [r for _, r in second.exits] == ["take_profit_2"]
    # the price collapses within seconds: the rest is not sold at the jump price
    third = paper_engine.manage_step(s, Decimal("0.5"))
    assert [r for _, r in third.exits] == ["stop_loss"] and third.closed


def test_positions_that_never_crossed_a_migration_are_unchanged():
    s = _state(crossing=False)
    r = paper_engine.manage_step(s, Decimal("6"))
    assert [x for _, x in r.exits] == ["take_profit_1", "take_profit_2", "take_profit_3"]
