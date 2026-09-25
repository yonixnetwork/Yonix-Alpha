"""Hyperliquid grid, LIVE: the ported grid (strategies/grid.py) with real
post-only orders on Hyperliquid, run by services/execution-futures.

Differences from the paper grid, all toward "never assume":
- every level is a real post-only (ALO) limit order with our client id;
  a fill is booked only from the exchange's order status (filled size,
  average price, fee), partial fills included;
- a filled level is replaced one spacing away, as in the repository — but
  only after the fill is confirmed (the source bot assumed fills);
- the net position is protected on the exchange by a reduce-only trigger
  stop at the range-break price on the adverse side, replaced whenever the
  position changes, so the worst case holds even if YonixAlpha is down;
- circuit breakers (drawdown, range break), a kill switch, a mode change
  or the environment locks closing all cancel every resting order and,
  with flatten_on_pause (or on stop), close the position reduce-only;
- each tick compares the exchange position with the grid's own; a
  mismatch pauses the grid for a human (needs review).

Start: global mode LIVE, strategy AUTO (or MANUAL + operator start), the
Hyperliquid execution worker ready, no standalone grid bot running, the
capital within the synced account balance minus the reserve, and the
worst-case loss within max_daily_loss_quote.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from yonixalpha_core import events, external_bots, futures_live, kill_switch
from yonixalpha_core.db.models import AuditLog, ExecutionOrder, ReconciliationEvent, StrategyState
from yonixalpha_core.execution.base import CANCELED, EXPIRED, FILLED, REJECTED, ExecutionError
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.strategies import grid

log = get_logger("core.grid_live")

STRATEGY = grid.NAME
ENGINE = "hyperliquid_perps"
VENUE = "hyperliquid"
PROVIDER = "hyperliquid_grid"  # ExecutionOrder.provider: kept apart from the futures order worker
DEAD = {CANCELED, REJECTED, EXPIRED}


def state_key(coin: str) -> str:
    return f"live:{coin}"


async def live_intent(session, app_settings: Any) -> tuple[bool, StrategyMode]:
    mode = pipeline.effective_mode(await store.load_strategy_mode(session, STRATEGY), await store.load_strategy_mode(session, ENGINE))
    live = (await store.load_global_mode(session) == GlobalMode.LIVE and mode in (StrategyMode.AUTO, StrategyMode.MANUAL)
            and store.live_trading_permitted(app_settings))
    return live, mode


async def _row(session, coin: str) -> StrategyState | None:
    return (await session.execute(select(StrategyState).where(
        StrategyState.strategy == STRATEGY, StrategyState.key == state_key(coin)))).scalar_one_or_none()


def _order_row(coin: str, cid: str, is_buy: bool, size: Decimal, price: Decimal, reason: str, status: str) -> ExecutionOrder:
    return ExecutionOrder(mode="LIVE", side="BUY" if is_buy else "SELL", reason=reason, mint=coin, provider=PROVIDER,
                          route="grid", amount=str(size), amount_kind="base", slippage_pct=Decimal(0),
                          priority_fee_sol=Decimal(0), limits={"price": str(price), "post_only": reason == "grid_level"},
                          status=status, idempotency_key=cid, signature=cid)


async def _place(session, provider, coin: str, data: dict, is_buy: bool, price: Decimal, size: Decimal, level: int) -> None:
    gen = data["gen"] = data.get("gen", 0) + 1
    cid = f"g{data['run']}l{level}n{gen}"
    st = await provider.limit_order(coin, "BUY" if is_buy else "SELL", size, price, cid, post_only=True)
    session.add(_order_row(coin, cid, is_buy, size, price, "grid_level", "SUBMITTED" if st.status not in DEAD else "FAILED"))
    if st.status in DEAD:
        data.setdefault("dropped", []).append({"level": level, "price": str(price), "error": st.error})
        return
    data["orders"].append({"cid": cid, "is_buy": is_buy, "price": str(price), "size": str(size), "level": level,
                           "filled": "0", "fee": "0"})


async def _protect(provider, coin: str, s: grid.GridState, params: dict, data: dict) -> None:
    """Reduce-only trigger stop for the whole net position at the
    range-break price on the adverse side (new stop first, then the old
    one is cancelled)."""
    old = data.get("stop_id")
    if s.net_position == 0:
        if old:
            await provider.cancel_stop(coin, old)
            data["stop_id"] = None
        return
    brk = Decimal(str(params["range_break_pct"])) / 100
    long = s.net_position > 0
    price = s.range_lower * (1 - brk) if long else s.range_upper * (1 + brk)
    sid = await provider.set_stop(coin, "LONG" if long else "SHORT", price, abs(s.net_position),
                                  f"g{data['run']}s{data.get('gen', 0)}")
    data["stop_id"], data["stop_price"] = sid, str(price)
    if old and old != sid:
        try:
            await provider.cancel_stop(coin, old)
        except ExecutionError as exc:
            log.warning("grid_live.old_stop_not_cancelled", error=str(exc)[:160])


async def _cancel_all(session, provider, coin: str, data: dict) -> None:
    for o in data.get("orders", []):
        try:
            await provider.cancel_order(coin, o["cid"])
        except ExecutionError as exc:
            log.warning("grid_live.cancel_failed", cid=o["cid"], error=str(exc)[:160])
        await session.execute(ExecutionOrder.__table__.update().where(
            ExecutionOrder.idempotency_key == o["cid"], ExecutionOrder.status == "SUBMITTED").values(status="CANCELLED"))
    data["orders"] = []


async def _flatten(session, provider, coin: str, s: grid.GridState, data: dict, mid: Decimal, now: datetime) -> None:
    if s.net_position == 0:
        return
    size = abs(s.net_position)
    cid = f"g{data['run']}f{data.get('gen', 0)}"
    st = await provider.market_order(coin, "BUY" if s.net_position < 0 else "SELL", size, True, cid, mid)
    row = _order_row(coin, cid, s.net_position < 0, size, mid, "grid_flatten", "PENDING")
    session.add(row)
    if st.filled_qty > 0 and st.avg_price:
        grid._record_fill(s, s.net_position < 0, st.avg_price, st.filled_qty, st.fee)
        row.status, row.confirmed_at, row.result = "CONFIRMED", now, st.to_dict()
    else:
        row.status, row.error = "FAILED", (st.error or st.status)[:500]
        data["flatten_failed"] = row.error


async def stop(session, redis, provider, row: StrategyState, reason: str, mid: Decimal | None, now: datetime,
               flatten: bool = True, user_id=None) -> None:
    data = dict(row.state or {})
    s = grid.from_json(data["grid"])
    coin = row.key.split(":", 1)[1]
    await _cancel_all(session, provider, coin, data)
    if flatten and mid is not None:
        await _flatten(session, provider, coin, s, data, mid, now)
    try:
        await _protect(provider, coin, s, {**grid.DEFAULTS, **(data.get("params") or {})}, data)
    except ExecutionError as exc:
        data["protection_error"] = str(exc)[:200]
    data["grid"], data["stopped_reason"] = s.to_json(), reason
    row.state = data
    row.status = "stopped" if s.net_position == 0 else "needs_review"
    session.add(AuditLog(user_id=user_id, event_type="grid.live_stopped", detail={"coin": coin, "reason": reason,
                                                                                 "net_position": str(s.net_position)}))
    await events.publish(redis, "strategy.updated", {"strategy": STRATEGY, "status": row.status, "coin": coin, "mode": "LIVE"}, "grid")


async def _start(session, redis, app_settings, provider, params: dict, mid: Decimal, now: datetime) -> StrategyState:
    coin = params["coin"]
    safety, _ = await store.load_settings(session, ENGINE)
    live = await futures_live.load_settings(session)
    row = await _row(session, coin)
    if row is None:
        row = StrategyState(strategy=STRATEGY, key=state_key(coin), status="stopped", state={})
        session.add(row)
    balance = await provider.balance()
    capital = Decimal(str(params["capital"]))
    if capital <= 0 or capital > balance - live.min_free_balance:
        row.status, row.state = "refused", {"reason": f"capital {capital} exceeds available {balance} minus reserve "
                                                      f"{live.min_free_balance}"}
        return row
    s = grid.build(params, mid, capital, safety.max_leverage)
    worst = grid.worst_case_loss(s, params)
    if worst > safety.max_daily_loss_quote:
        row.status = "refused"
        row.state = {"reason": f"worst-case loss {worst:.4f} exceeds max_daily_loss_quote {safety.max_daily_loss_quote}"}
        return row
    await provider.prepare(coin, int(min(Decimal(str(params["leverage"])), safety.max_leverage)) or 1)
    data = {"run": now.strftime("%y%m%d%H%M%S"), "gen": 0, "orders": [], "params": {k: str(v) for k, v in params.items()},
            "worst_case_loss": str(worst), "started_at": now.isoformat(), "mode": "LIVE"}
    for o in s.orders:
        await _place(session, provider, coin, data, o.is_buy, o.price, o.size, s.levels.index(o.price))
    s.orders = []  # the live grid's orders are the exchange's, tracked in data["orders"]
    data["grid"] = s.to_json()
    row.state, row.status = data, "running"
    session.add(AuditLog(event_type="grid.live_started", detail={"coin": coin, "capital": str(capital), "worst_case_loss": str(worst),
                                                                 "orders": len(data["orders"])}))
    await events.notify(session, redis, app_settings, "entry", f"LIVE grid started: {coin}",
                        f"{len(data['orders'])} post-only orders, capital {capital}, worst case {worst:.2f}", "warning")
    return row


async def _advance(session, redis, app_settings, provider, row: StrategyState, mid: Decimal, now: datetime) -> dict:
    data = dict(row.state)
    coin = row.key.split(":", 1)[1]
    params = {**grid.DEFAULTS, **(data.get("params") or {})}
    s = grid.from_json(data["grid"])
    fills = 0
    current, data["orders"] = list(data["orders"]), []  # still-open orders and replacements are re-added
    for o in current:
        st = await provider.order_status(coin, o["cid"])
        prev, prev_fee = Decimal(o["filled"]), Decimal(o.get("fee", "0"))
        if st.filled_qty > prev and st.avg_price:
            # book only the newly filled part; the fee is the order's cumulative fee delta
            delta = st.filled_qty - prev
            grid._record_fill(s, o["is_buy"], st.avg_price, delta, max(Decimal(0), st.fee - prev_fee))
            o["filled"], o["fee"] = str(st.filled_qty), str(st.fee)
            fills += 1
            await events.publish(redis, "trade.updated", {"strategy": STRATEGY, "coin": coin, "mode": "LIVE",
                                                          "side": "BUY" if o["is_buy"] else "SELL", "price": str(st.avg_price),
                                                          "size": str(delta)}, "grid")
        if st.status == FILLED or (st.status in DEAD and st.filled_qty > 0):
            await session.execute(ExecutionOrder.__table__.update().where(ExecutionOrder.idempotency_key == o["cid"]).values(
                status="CONFIRMED", confirmed_at=now, result=st.to_dict()))
            price, size = Decimal(o["price"]), Decimal(o["filled"])
            nxt = price + s.spacing if o["is_buy"] else price - s.spacing
            level = o["level"] + (1 if o["is_buy"] else -1)
            if s.range_lower - s.spacing / 2 <= nxt <= s.range_upper + s.spacing / 2:
                await _place(session, provider, coin, data, not o["is_buy"], nxt, size, level)
        elif st.status in DEAD:
            await session.execute(ExecutionOrder.__table__.update().where(ExecutionOrder.idempotency_key == o["cid"]).values(
                status="CANCELLED", error=(st.error or st.status)[:500]))
            data.setdefault("dropped", []).append({"level": o["level"], "price": o["price"], "error": st.error or st.status})
        else:
            data["orders"].append(o)
    s.mid = mid
    s.peak_equity = max(s.peak_equity, s.equity)
    if fills:
        await _protect(provider, coin, s, params, data)

    # The exchange's position must match the grid's.
    ex = await provider.position(coin)
    ex_size = ex.size if ex else Decimal(0)
    rules = await provider.instrument(coin)
    if abs(ex_size - s.net_position) > rules.qty_step:
        session.add(ReconciliationEvent(kind="grid_position_mismatch", severity="critical", mint=coin,
                                        detail={"exchange": str(ex_size), "grid": str(s.net_position)}))
        data["grid"] = s.to_json()
        row.state = data
        await _cancel_all(session, provider, coin, data)
        row.state, row.status = data, "needs_review"
        await events.notify(session, redis, app_settings, "provider_failure", f"LIVE grid needs review: {coin}",
                            f"exchange position {ex_size} vs grid {s.net_position}", "critical")
        return {"status": "needs_review"}

    brk = Decimal(str(params["range_break_pct"])) / 100
    reason = None
    if s.drawdown_pct >= Decimal(str(params["max_drawdown_pct"])):
        reason = "pause_drawdown"
    elif mid < s.range_lower * (1 - brk) or mid > s.range_upper * (1 + brk):
        reason = "pause_range_break"
    data["grid"], data["last_mid"], data["updated"] = s.to_json(), str(mid), now.isoformat()
    row.state = data
    if reason:
        await stop(session, redis, provider, row, reason, mid, now, flatten=str(params["flatten_on_pause"]).lower() == "true")
        await events.notify(session, redis, app_settings, "strategy_disabled", f"LIVE grid paused: {reason}",
                            f"mid {mid}, equity {s.equity}", "warning", {"coin": coin})
        return {"status": row.status, "fills": fills, "reason": reason}
    return {"status": "running", "fills": fills}


async def run(session_factory, redis: Redis, app_settings: Any, provider, mid_fn, now: datetime) -> dict:
    """One live-grid tick. `mid_fn(coin)` returns the current mid."""
    async with session_factory() as session:
        params = {**grid.DEFAULTS, **await store.load_strategy_config(session, STRATEGY)}
        coin = params["coin"]
        row = await _row(session, coin)
        live, mode = await live_intent(session, app_settings)
        running = row is not None and row.status == "running"
        if not live and not running:
            return {"status": "not live"}
        command = await redis.getdel(grid.COMMAND_KEY) if live else None
        mid = await mid_fn(coin)
        killed = await kill_switch.is_engaged(redis)
        if running and (not live or killed or command == "stop" or mode == StrategyMode.OFF):
            reason = "kill switch" if killed else ("operator stop" if command == "stop" else "live mode ended")
            await stop(session, redis, provider, row, reason, mid, now)
            await session.commit()
            return {"status": row.status, "reason": reason}
        if not running:
            ready, why = await futures_live.readiness(redis, app_settings, VENUE, now)
            conflict = await external_bots.conflict(redis, app_settings, STRATEGY)
            if not ready or conflict or killed:
                return {"status": "not started", "reason": why or conflict or "kill switch"}
            if row is not None and row.status == "needs_review":
                return {"status": "needs_review"}
            auto = mode == StrategyMode.AUTO and not (row and row.status == "refused")
            if auto or command == "start":
                row = await _start(session, redis, app_settings, provider, params, mid, now)
                await session.commit()
                return {"status": row.status, **({"reason": row.state.get("reason")} if row.status == "refused" else {})}
            return {"status": row.status if row else "not started"}
        out = await _advance(session, redis, app_settings, provider, row, mid, now)
        await session.commit()
        return out
