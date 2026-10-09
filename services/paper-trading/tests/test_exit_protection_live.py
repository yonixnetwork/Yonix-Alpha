"""Sellable-amount protection on LIVE exits (exit_plan, 2026-10-10).

Default mode PAPER: a LIVE take-profit that would leave an uneconomic
remainder still goes out exactly as before; the would-be change is recorded
on the timeline (shadow). Mode PAPER_AND_LIVE: the remainder is folded into
the sale (one SELL for everything). Protective full exits are unchanged in
both modes."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from yonixalpha_core import exit_plan, live_trading
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, PlatformSetting, TradeTimelineEvent

from tests.test_live_worker import NOW, open_live

TOKENS_RAW = 3_000_000_000_000  # open_live: 3,000,000 tokens at 6 decimals


async def _tp_position(s, pid, remainder_raw: int):
    """A TP1 for 60 % of the original that leaves `remainder_raw` behind."""
    p = await s.get(PaperPosition, pid)
    e = p.entry_price
    p.plan = {**p.plan, "take_profits": [{"price": {"value": str(e * 2)}, "exit_fraction": "0.6"}]}
    p.remaining_quantity = p.initial_quantity * Decimal("0.6") + exit_plan.from_raw(remainder_raw, 6)
    return p, e * 3


async def _events(s, pid) -> list[str]:
    return list((await s.execute(select(TradeTimelineEvent.event_type).where(
        TradeTimelineEvent.position_id == pid))).scalars())


async def test_default_mode_records_the_change_but_sells_as_before(session_factory, redis_client):
    _, _, pid, _, _, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        p, price = await _tp_position(s, pid, 2)  # 2 raw units left: worth far less than a sell fee
        out = await live_trading.manage_live_position(s, p, price, None, NOW + timedelta(seconds=30))
        await s.commit()
        sell = await s.get(ExecutionOrder, p.pending_order_id)
        events = await _events(s, pid)
    assert out["requested"] == "take_profit_1"
    assert int(sell.amount) == TOKENS_RAW * 6 // 10  # unchanged: exactly the planned 60 %
    assert out["exit_protection"]["action"] == exit_plan.MERGED_REMAINDER and out["exit_protection"]["applied"] is False
    assert "exit_protection.merged_remainder.shadow" in events


async def test_paper_and_live_mode_folds_the_remainder_into_the_sale(session_factory, redis_client):
    _, _, pid, _, _, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        s.add(PlatformSetting(key=exit_plan.SETTINGS_KEY, value={"mode": "PAPER_AND_LIVE"}))
        p, price = await _tp_position(s, pid, 2)
        out = await live_trading.manage_live_position(s, p, price, None, NOW + timedelta(seconds=30))
        await s.commit()
        sell = await s.get(ExecutionOrder, p.pending_order_id)
        events = await _events(s, pid)
    assert int(sell.amount) == TOKENS_RAW * 6 // 10 + 2  # everything: no unsellable remainder left behind
    assert sell.limits["max_tokens_in"] == int(sell.amount)
    assert out["exit_protection"]["applied"] is True and "exit_protection.merged_remainder" in events


async def test_a_stop_loss_is_never_changed_in_any_mode(session_factory, redis_client):
    _, _, pid, _, _, _ = await open_live(session_factory, redis_client)
    async with session_factory() as s:
        s.add(PlatformSetting(key=exit_plan.SETTINGS_KEY, value={"mode": "PAPER_AND_LIVE"}))
        p = await s.get(PaperPosition, pid)
        out = await live_trading.manage_live_position(s, p, p.stop_loss * Decimal("0.5"), None, NOW + timedelta(seconds=30))
        await s.commit()
        sell = await s.get(ExecutionOrder, p.pending_order_id)
    assert out["requested"] == "stop_loss" and int(sell.amount) == TOKENS_RAW
    assert out["exit_protection"]["action"] == exit_plan.SELL
