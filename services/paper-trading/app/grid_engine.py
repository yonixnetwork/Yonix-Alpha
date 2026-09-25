"""Hyperliquid grid, paper only: the ported grid math (yonixalpha_core.
strategies.grid) stepped against live Hyperliquid mid prices, with state
persisted in strategy_states and every fill recorded in paper_orders.

Start: only when the strategy (and its venue, hyperliquid_perps) is not OFF,
the kill switch is off, and the grid's worst-case loss fits the engine's
max_daily_loss_quote. MANUAL mode waits for an operator start from the
dashboard. The grid's capital is reserved from the "hyperliquid" paper book
while it runs and returned (with PnL) when it stops.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from yonixalpha_core import events, kill_switch
from yonixalpha_core.db.models import AuditLog, PaperOrder, StrategyState
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.models import StrategyMode
from yonixalpha_core.strategies import grid
from yonixalpha_core.venues.common import VenueError

log = get_logger("paper-trading.grid")

STRATEGY = grid.NAME
ENGINE = "hyperliquid_perps"


async def load_state(session, coin: str) -> StrategyState | None:
    return (await session.execute(
        select(StrategyState).where(StrategyState.strategy == STRATEGY, StrategyState.key == coin)
    )).scalar_one_or_none()


async def start_grid(session, redis: Redis, app_settings: Any, params: dict, mid: Decimal, user_id=None) -> StrategyState:
    """Builds and starts a grid (also used by the API's manual start)."""
    coin = params["coin"]
    safety, _ = await store.load_settings(session, ENGINE)
    account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[ENGINE])
    capital = Decimal(str(params["capital"]))
    row = await load_state(session, coin)
    if row is None:
        row = StrategyState(strategy=STRATEGY, key=coin, status="stopped", state={})
        session.add(row)
    if capital <= 0 or capital > account.cash_balance:
        row.status, row.state = "refused", {"reason": f"capital {capital} exceeds paper cash {account.cash_balance}"}
        return row
    state = grid.build(params, mid, capital, safety.max_leverage)
    worst = grid.worst_case_loss(state, params)
    if worst > safety.max_daily_loss_quote:
        row.status = "refused"
        row.state = {"reason": f"worst-case loss {worst:.4f} exceeds max_daily_loss_quote {safety.max_daily_loss_quote}",
                     "worst_case_loss": str(worst)}
        await events.notify(session, redis, app_settings, "strategy_disabled", "Grid refused: risk too large", row.state["reason"],
                            "warning")
        return row
    account.cash_balance -= capital
    row.status = "running"
    row.state = {"grid": state.to_json(), "params": {k: str(v) for k, v in params.items()}, "worst_case_loss": str(worst),
                 "reserved": str(capital), "started_at": datetime.now().isoformat()}
    session.add(AuditLog(user_id=user_id, event_type="grid.started",
                         detail={"coin": coin, "capital": str(capital), "worst_case_loss": str(worst)}))
    await events.publish(redis, "strategy.updated", {"strategy": STRATEGY, "status": "running", "coin": coin}, "grid")
    return row


async def stop_grid(session, redis: Redis, row: StrategyState, mid: Decimal | None, reason: str, user_id=None) -> None:
    """Flattens at mid with the taker fee, returns capital + PnL to the book."""
    data = dict(row.state or {})
    if "grid" not in data or row.status not in ("running", "paused"):
        row.status = "stopped"
        return
    params = {**grid.DEFAULTS, **(data.get("params") or {})}
    s = grid.from_json(data["grid"])
    if mid is not None and s.net_position != 0:
        taker = Decimal(str(params["taker_fee_bps"])) / 10000
        size = abs(s.net_position)
        grid._record_fill(s, s.net_position < 0, mid, size, mid * size * taker)
        s.mid = mid
    account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[ENGINE])
    account.cash_balance += s.equity
    data["grid"] = s.to_json()
    data["stopped_reason"] = reason
    data["final_equity"] = str(s.equity)
    row.state = data
    row.status = "stopped"
    session.add(AuditLog(user_id=user_id, event_type="grid.stopped", detail={"coin": row.key, "reason": reason, "equity": str(s.equity)}))
    await events.publish(redis, "strategy.updated", {"strategy": STRATEGY, "status": "stopped", "coin": row.key}, "grid")


async def run_grid(session_factory, redis: Redis, app_settings: Any, venues: dict, now: datetime) -> dict:
    async with session_factory() as session:
        params = {**grid.DEFAULTS, **await store.load_strategy_config(session, STRATEGY)}
        mode = pipeline.effective_mode(await store.load_strategy_mode(session, STRATEGY), await store.load_strategy_mode(session, ENGINE))
        coin = params["coin"]
        row = await load_state(session, coin)
        killed = await kill_switch.is_engaged(redis)
        command = await redis.getdel(grid.COMMAND_KEY)
        try:
            mid = await venues["hyperliquid"].mid(coin)
        except VenueError as exc:
            if command:
                await redis.set(grid.COMMAND_KEY, command, ex=600)  # keep it for the next tick
            return {"status": f"no price: {exc}"}

        if row is not None and row.status in ("running", "paused") and (mode == StrategyMode.OFF or killed or command == "stop"):
            reason = "kill switch" if killed else ("strategy OFF" if mode == StrategyMode.OFF else "operator stop")
            await stop_grid(session, redis, row, mid, reason)
            await session.commit()
            return {"status": "stopped"}
        if row is None or row.status in ("stopped", "refused"):
            # PAPER/AUTO start on their own (unless a previous start was refused);
            # MANUAL waits for an operator start; an operator start also retries
            # a refused grid (it is re-checked against the risk budget).
            auto = mode in (StrategyMode.PAPER, StrategyMode.AUTO) and not (row and row.status == "refused")
            if mode != StrategyMode.OFF and not killed and (auto or command == "start"):
                row = await start_grid(session, redis, app_settings, params, mid)
                await session.commit()
                return {"status": row.status}
            return {"status": row.status if row else "not started"}
        if row.status != "running":
            return {"status": row.status}

        data = dict(row.state)
        s = grid.from_json(data["grid"])
        evs = grid.step(s, mid, params)
        account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[ENGINE])
        for e in evs:
            if e["type"] in ("fill", "flatten"):
                side = e.get("side") or ("SELL" if e["type"] == "flatten" else "BUY")
                session.add(PaperOrder(account_id=account.id, strategy=STRATEGY, venue="hyperliquid", symbol=coin, side=side,
                                       order_type="limit" if e["type"] == "fill" else "market", price=Decimal(e["price"]),
                                       quantity=Decimal(e["size"]), status="filled", fill_price=Decimal(e["price"]),
                                       fee=Decimal(e.get("fee", "0")), client_order_id=f"grid-{uuid.uuid4().hex[:24]}",
                                       detail={"event": e["type"]}, filled_at=now))
                await events.publish(redis, "trade.updated", {"strategy": STRATEGY, "coin": coin, **e}, "grid")
            if e["type"] == "pause":
                row.status = "paused"
                await events.notify(session, redis, app_settings, "strategy_disabled", f"Grid paused: {e['reason']}",
                                    f"mid {mid}, equity {s.equity}", "warning", {"coin": coin})
        data["grid"] = s.to_json()
        data["last_mid"] = str(mid)
        data["updated"] = now.isoformat()
        row.state = data
        await session.commit()
        return {"status": row.status, "fills": sum(1 for e in evs if e["type"] == "fill")}
