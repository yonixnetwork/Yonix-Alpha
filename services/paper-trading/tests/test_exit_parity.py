"""Automatic vs manual exit regression (master upgrade §80).

Identical position, identical market, identical provider boundary: a stop
loss (automatic) and a dashboard SELL (manual, exit_requested) must both
produce exactly one full SELL on the same route with the same slippage and
limits, close the position from the sell fill, update PnL, and never need
or queue a second sell. If the automatic exit does not close the position,
this test fails.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from yonixalpha_core import live_trading
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition

from tests.test_live_worker import LIVE_ON, NOW, confirmed, open_live

TOKENS_RAW = 3_000_000_000_000


@pytest.mark.parametrize("origin", ["automatic", "manual"])
async def test_automatic_and_manual_exits_sell_the_same_way_and_close_once(session_factory, redis_client, origin):
    _, _, pid, _, ex, spent = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        if origin == "automatic":
            price, expect_reason = p.stop_loss * Decimal("0.8"), "stop_loss"
        else:
            p.exit_requested = True  # what the dashboard SELL / CLOSE POSITIONS set
            price, expect_reason = p.entry_price, "manual_exit"
        out = await live_trading.manage_live_position(s, p, price, None, NOW + timedelta(seconds=20))
        await s.commit()
        sell_id = p.pending_order_id
    assert out["requested"] == expect_reason

    async with session_factory() as s:
        sell = await s.get(ExecutionOrder, sell_id)
        live = await live_trading.load_live_settings(s)
    # the same order shape for both origins: full quantity, same route, same slippage, a minimum output
    assert (sell.side, sell.status, sell.amount, sell.route) == ("SELL", "PENDING", str(TOKENS_RAW), "pump")
    assert sell.slippage_pct == live.exit_slippage_pct
    assert sell.limits["max_tokens_in"] == TOKENS_RAW and sell.limits["min_sol_out_lamports"] > 0

    # a second management tick while the sell is pending never queues a duplicate
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        again = await live_trading.manage_live_position(s, p, price, None, NOW + timedelta(seconds=21))
        await s.commit()
    assert again["requested"] is None

    received = 60_000_000
    ex.outcomes.append(confirmed("sig-sell", received, -TOKENS_RAW))
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, sell_id) == "CONFIRMED"
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        sells = (await s.execute(select(func.count()).select_from(ExecutionOrder).where(
            ExecutionOrder.position_id == pid, ExecutionOrder.side == "SELL"))).scalar_one()
    assert p.status == "closed" and p.remaining_quantity == 0 and p.exit_reason == expect_reason
    assert p.realized_pnl == (Decimal(received) - Decimal(spent)) / Decimal(1_000_000_000)
    assert sells == 1  # no second sell request was necessary

    # after the close, further ticks do nothing
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert live_trading.request_live_exit is not None and p.pending_order_id is None
        assert await live_trading.request_live_exit(s, p, Decimal(1), "stop_loss", None, NOW) is None


async def test_a_curve_sell_rejected_as_curve_complete_moves_the_next_sell_to_pumpswap(session_factory, redis_client):
    """Production (NEAR, 2026-09-28): a take-profit went to the bonding curve,
    failed on chain with Pump error 6005 (BondingCurveComplete) and the
    position reached PumpSwap only 17 s later. Now the rejection itself moves
    the position; the retry goes to PumpSwap without widening slippage."""
    from yonixalpha_core.solana import pumpswap
    from yonixalpha_core.solana.live_exec import ExecOutcome

    from tests.test_live_worker import MINT

    _, _, pid, _, ex, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        crash = p.stop_loss * Decimal("0.8")
        await live_trading.manage_live_position(s, p, crash, None, NOW + timedelta(seconds=20))
        await s.commit()
        first = p.pending_order_id
    ex.outcomes.append(ExecOutcome("FAILED", "sig-f", sent=True,
                                   error="transaction failed on chain: {'InstructionError': [2, {'Custom': 6005}]}"))
    assert await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, first) == "FAILED"
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        assert (p.lifecycle, p.execution_route, p.pool) == ("MIGRATED", "pump-amm", pumpswap.canonical_pool(MINT))
        assert p.exit_failures == 0 and p.status == "open" and p.pending_order_id is None
        await live_trading.manage_live_position(s, p, crash, None, NOW + timedelta(seconds=21))
        await s.commit()
        retry = await s.get(ExecutionOrder, p.pending_order_id)
        orig = await s.get(ExecutionOrder, first)
    assert retry.route == "pump-amm" and retry.slippage_pct == orig.slippage_pct and retry.amount == str(TOKENS_RAW)


async def test_other_sell_failures_still_widen_slippage_and_keep_the_route(session_factory, redis_client):
    from yonixalpha_core.solana.live_exec import ExecOutcome

    _, _, pid, _, ex, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
        await live_trading.manage_live_position(s, p, p.stop_loss * Decimal("0.8"), None, NOW)
        await s.commit()
        first = p.pending_order_id
    ex.outcomes.append(ExecOutcome("FAILED", "sig-f", sent=True,
                                   error="transaction failed on chain: {'InstructionError': [2, {'Custom': 6003}]}"))
    await live_trading.process_order(session_factory, redis_client, LIVE_ON, ex, first)
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
    assert (p.lifecycle, p.execution_route, p.exit_failures) == ("FRESH", "pump", 1)


async def test_a_migrated_curve_position_is_never_priced_from_the_stale_curve(session_factory, redis_client):
    """After the switch the stream may still show the curve as not complete;
    the price (and so the sell's minimum output) must come from the pool."""
    from app.gate_manage import price_position

    _, _, pid, _, _, _ = await open_live(session_factory, redis_client)  # the stream holds a live, incomplete curve
    async with session_factory() as s:
        p = await s.get(PaperPosition, pid)
    price, _, _, source = await price_position(redis_client, None, p, NOW)
    assert source == "pump_stream:curve" and price is not None
    p.lifecycle = "MIGRATED"
    price, _, _, source = await price_position(redis_client, None, p, NOW)
    assert price is None and source != "pump_stream:curve"  # no RPC / Jupiter here: unpriced, never the curve
