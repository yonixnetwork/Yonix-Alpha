"""One evaluation step of one strategy, and the protection pass for open
demo positions.

Order of a step:
1. fetch candles for every configured symbol; clean them (candles.clean);
2. refuse stale data, and candles that are not aligned across symbols;
3. record one Signal per strategy / exchange / symbol / closed candle (the
   unique key turns a repeated evaluation into a no-op);
4. the strategy decides; risk.size_entry validates and sizes;
5. the demo executor fills (client id derived from the candle, so a retry
   cannot double-fill).
SHORT decisions are recorded but not simulated: spot is long-only here.

Protection (protect_positions) runs every loop for every OPEN demo position,
whether or not its strategy or the engine is enabled, so disabling never
abandons a position: stops and targets keep being checked against the bid."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto import demo, portfolio
from yonixalpha_crypto.candles import INTERVAL_SECONDS, clean
from yonixalpha_crypto.db import EXEC_ACTIVE, DemoPosition, Event, ExecTrade, Signal, StrategyConfig
from yonixalpha_crypto.exchanges.base import InstrumentSpec, MarketData
from yonixalpha_crypto.exchanges.profiles import PROFILES
from yonixalpha_crypto.registry import StrategySpec
from yonixalpha_crypto.risk import RiskLimits, size_entry
from yonixalpha_crypto.strategies import Strategy

INSTRUMENT_TTL_S = 3600.0


class InstrumentCache:
    def __init__(self):
        self._c: dict[tuple[str, str], tuple[float, InstrumentSpec]] = {}

    async def get(self, md: MarketData, symbol: str) -> InstrumentSpec:
        key = (md.exchange, symbol)
        hit = self._c.get(key)
        if hit and time.monotonic() - hit[0] < INSTRUMENT_TTL_S:
            return hit[1]
        spec = await md.instrument(symbol)
        self._c[key] = (time.monotonic(), spec)
        return spec


def jsonable(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


async def _record_signal(session: AsyncSession, **kw) -> Signal | None:
    """Insert, or None when this candle was already evaluated."""
    kw["reasons"], kw["detail"] = jsonable(kw.get("reasons") or []), jsonable(kw.get("detail") or {})
    stmt = insert(Signal).values(**kw).on_conflict_do_nothing(constraint="uq_signal_candle").returning(Signal.id)
    sid = (await session.execute(stmt)).scalar_one_or_none()
    return await session.get(Signal, sid) if sid else None


async def step_strategy(session: AsyncSession, cfg: StrategyConfig, spec: StrategySpec, impl: Strategy,
                        md: MarketData, instruments: InstrumentCache, now: datetime, *,
                        blocked: str | None = None) -> str:
    params, symbols = dict(cfg.params or {}), list(cfg.symbols or [])
    traded = impl.traded_symbol(symbols)
    need = impl.required_candles(params)
    sets = {s: clean(await md.candles(s, cfg.interval, need + 2), cfg.interval, now) for s in symbols}
    newest = {s: cs.candles[-1].open_time for s, cs in sets.items() if cs.candles}
    base = dict(strategy_id=spec.id, strategy_version=spec.version, exchange=cfg.exchange, symbol=traded,
                interval=cfg.interval)
    stale = [f"{s}: {cs.stale_reason}" for s, cs in sets.items() if cs.stale]
    if traded not in newest:
        return "no closed candle"
    candle_time = newest[traded]
    reasons: list[str] = []
    if stale:
        reasons = ["stale market data: " + "; ".join(stale)]
    elif len(set(newest.values())) != 1 or len(newest) != len(symbols):
        reasons = [f"candles not aligned across symbols: { {s: t.isoformat() for s, t in newest.items()} }"]
    elif any(len(cs.candles) < need for cs in sets.values()):
        reasons = [f"not enough closed candles (need {need})"]
    if reasons:
        sig = await _record_signal(session, **base, candle_open_time=candle_time, side="NONE", action="REJECTED",
                                   reasons=reasons, detail={})
        await session.commit()
        return "rejected" if sig else "duplicate"

    P = DemoPosition
    exchange_demo = cfg.mode == "EXCHANGE_DEMO"
    position = (await session.execute(select(P).where(P.strategy_id == spec.id, P.exchange == cfg.exchange,
                                                      P.symbol == traded, P.status == "OPEN"))).scalar_one_or_none()
    xtrade = None
    if exchange_demo:
        xtrade = (await session.execute(select(ExecTrade).where(
            ExecTrade.strategy_id == spec.id, ExecTrade.symbol == traded, ExecTrade.connection_id == cfg.connection_id,
            ExecTrade.status.in_(EXEC_ACTIVE)))).scalar_one_or_none()
    d = impl.evaluate({s: cs.candles for s, cs in sets.items()}, params, (position or xtrade) is not None)
    detail = {**d.detail, "gaps": {s: len(cs.gaps) for s, cs in sets.items()}}
    action, reasons = d.action, list(d.reasons)
    order_kw = None
    if d.action == "ENTER" and d.side == "SHORT":
        action, reasons = "REJECTED", reasons + ["SHORT signal: spot is long-only here, not simulated"]
    elif d.action == "ENTER" and (position or xtrade) is not None:
        action, reasons = "HOLD", reasons + ["already in a position"]
    elif d.action == "ENTER":
        inst = await instruments.get(md, traded)
        book = await md.top_of_book(traded)
        prof = PROFILES[cfg.exchange]
        limits = RiskLimits.from_dict(cfg.risk)
        expo = await demo.exposure(session, spec.id, cfg.exchange, inst.quote, now)
        stop_pct = Decimal(d.detail["stop_pct"]) if d.detail.get("stop_pct") else None
        target_pct = Decimal(d.detail["target_pct"]) if d.detail.get("target_pct") else None
        stop = d.stop_price or Decimal(0)
        if stop_pct is not None:  # size on the expected fill, as the stop will be anchored there
            stop = book.ask * (1 + limits.slippage_bps / 10000) * (1 - stop_pct)
        rd = size_entry(limits, expo, book, inst, stop, prof.taker_fee_bps, now, blocked=blocked)
        detail["risk"] = {"approved": rd.approved, "qty": str(rd.qty), "expected_price": str(rd.expected_price),
                          "planned_loss_quote": str(rd.planned_loss_quote), "reasons": rd.reasons}
        if not rd.approved:
            action, reasons = "REJECTED", reasons + rd.reasons
        else:
            extra = await _entry_filters(session, spec.id, limits, sets[traded].candles[-1], now)
            extra += await portfolio.check_entry(session, symbol=traded, planned_loss=rd.planned_loss_quote,
                                                 notional=rd.quote_amount, now=now)
            detail["risk"]["budget"] = rd.reasons
            if extra:
                action, reasons = "REJECTED", reasons + extra
            elif exchange_demo:
                from yonixalpha_crypto.private.service import strategy_entry  # practice account orders
                ok, why, trade_id = await strategy_entry(session, cfg, spec.id, traded, d, limits, prof.taker_fee_bps,
                                                         f"{spec.id}:{cfg.exchange}:{traded}:{candle_time.isoformat()}")
                detail["exchange_demo"] = {"trade_id": trade_id, "result": why}
                if not ok:
                    action, reasons = "REJECTED", reasons + why
            else:
                order_kw = dict(kind="entry", inst=inst, book=book, qty=rd.qty, prof=prof, limits=limits,
                                stop_pct=stop_pct, target_pct=target_pct)
    elif d.action == "EXIT" and xtrade is not None:
        from yonixalpha_crypto.private.service import strategy_exit
        detail["exchange_demo"] = {"trade_id": str(xtrade.id),
                                   "result": await strategy_exit(session, xtrade, d.reasons[0] if d.reasons else "strategy exit")}
    elif d.action == "EXIT" and position is not None:
        inst = await instruments.get(md, traded)
        book = await md.top_of_book(traded)
        order_kw = dict(kind="exit", inst=inst, book=book, prof=PROFILES[cfg.exchange],
                        limits=RiskLimits.from_dict(cfg.risk))
    elif d.action == "EXIT":
        action = "NONE"

    sig = await _record_signal(session, **base, candle_open_time=candle_time, side=d.side, action=action,
                               reasons=reasons, detail=detail)
    if sig is None:
        await session.rollback()
        return "duplicate"
    if order_kw is not None:
        cid = f"{spec.id}:{cfg.exchange}:{traded}:{candle_time.isoformat()}:{order_kw['kind']}"
        if order_kw["kind"] == "entry":
            await demo.open_long(session, mode=cfg.mode, strategy_id=spec.id, client_id=cid, book=order_kw["book"],
                                 spec=order_kw["inst"], qty=order_kw["qty"], fee_bps=order_kw["prof"].taker_fee_bps,
                                 slippage_bps=order_kw["limits"].slippage_bps, stop_price=d.stop_price,
                                 target_price=d.target_price, top_size=order_kw["book"].ask_qty, signal_id=sig.id,
                                 now=now, stop_pct=order_kw["stop_pct"], target_pct=order_kw["target_pct"])
        else:
            await demo.close_long(session, mode=cfg.mode, position=position, client_id=cid, book=order_kw["book"],
                                  spec=order_kw["inst"], fee_bps=order_kw["prof"].taker_fee_bps,
                                  slippage_bps=order_kw["limits"].slippage_bps,
                                  reason=(d.reasons[0] if d.reasons else "strategy exit")[:48], now=now)
    await session.commit()
    return action


async def _entry_filters(session: AsyncSession, strategy_id: str, limits: RiskLimits, candle, now: datetime) -> list[str]:
    """Per-strategy filters beyond sizing: candle traded value and the
    cooldown after consecutive losses."""
    out = []
    if limits.min_bar_volume_quote > 0:
        traded_value = Decimal(candle.volume) * Decimal(candle.close)
        if traded_value < limits.min_bar_volume_quote:
            out.append(f"liquidity: signal candle traded {traded_value:.2f}, minimum {limits.min_bar_volume_quote}")
    n = limits.max_consecutive_losses
    if n > 0:
        P = DemoPosition
        last = (await session.execute(select(P.realized_pnl_quote, P.closed_at).where(
            P.strategy_id == strategy_id, P.status == "CLOSED").order_by(P.closed_at.desc()).limit(n))).all()
        if len(last) == n and all((p or 0) <= 0 for p, _ in last):
            until = last[0][1] + timedelta(minutes=float(limits.cooldown_minutes))
            if now < until:
                out.append(f"cooldown: {n} losses in a row; new entries paused until {until.isoformat()}")
    return out


async def manage_paper(session: AsyncSession, p: DemoPosition, cfg: StrategyConfig | None, book, inst, md,
                       now: datetime) -> str | None:
    """Trailing stop, breakeven, partial targets and the time stop of a paper
    position, from its strategy's exits plan (private.management.decide, the
    function the exchange executor and the backtester use). Returns the
    action taken, or None."""
    from yonixalpha_crypto.private import management as mg
    if cfg is None or not cfg.exits:
        return None
    plan = mg.validate_plan(cfg.exits)
    st = dict(p.exits_state or {})
    done = set(st.get("partials_done", []))
    plan["partials"] = [{**x, "done": i in done} for i, x in enumerate(plan["partials"])]
    hi = max(Decimal(p.highest_price or p.entry_price), book.bid)
    p.highest_price = hi
    step = INTERVAL_SECONDS.get(cfg.interval, 3600)
    bars_held = int((now - p.opened_at).total_seconds() // step)
    bars = []
    if plan["trailing"]["method"] in ("atr", "swing") and md is not None:
        cs = clean(await md.candles(p.symbol, cfg.interval, 40), cfg.interval, now).candles
        bars = [mg.Bar(c.high, c.low, c.close) for c in cs]
    initial = Decimal(p.initial_stop if p.initial_stop is not None else p.stop_price or 0)
    if initial <= 0:
        return None
    snap = mg.TradeSnapshot(Decimal(p.entry_price), initial, Decimal(p.stop_price or initial), Decimal(p.qty),
                            None, hi, bars_held, plan, PROFILES[p.exchange].taker_fee_bps)
    limits = RiskLimits.from_dict(cfg.risk)
    taken = None
    for dec_ in mg.decide(snap, mg.MarketSnapshot(book.bid, book.ask, bars), inst.tick_size or Decimal("0.00000001")):
        if dec_.action == mg.MOVE_STOP:
            p.stop_price = Decimal(dec_.detail["new_stop"])
            taken = "MOVE_STOP"
        elif dec_.action == mg.PARTIAL_EXIT:
            idx = int(dec_.detail["index"])
            o = await demo.partial_close(session, mode="DEMO", position=p, fraction=Decimal(dec_.detail["fraction"]),
                                         client_id=f"partial:{p.id}:{idx}", book=book, spec=inst,
                                         fee_bps=PROFILES[p.exchange].taker_fee_bps, slippage_bps=limits.slippage_bps,
                                         reason=dec_.reason)
            st = dict(p.exits_state or {})
            st["partials_done"] = sorted(set(st.get("partials_done", [])) | {idx})
            p.exits_state = st
            taken = "PARTIAL_EXIT" if o is not None else "PARTIAL_SKIPPED"
        elif dec_.action == mg.CLOSE:
            await demo.close_long(session, mode="DEMO", position=p, client_id=f"manage:{p.id}:close", book=book,
                                  spec=inst, fee_bps=PROFILES[p.exchange].taker_fee_bps,
                                  slippage_bps=limits.slippage_bps, reason=dec_.reason[:48], now=now)
            taken = "CLOSE"
        if dec_.action != mg.HOLD:
            session.add(Event(at=now, level="WARN" if dec_.action == mg.ALERT else "INFO", source=p.strategy_id,
                              message=f"paper {p.symbol}: {dec_.action}: {dec_.reason}"[:2000],
                              detail={"position": str(p.id), **{k: str(v) for k, v in dec_.detail.items()}}))
        if taken == "CLOSE":
            break
    return taken


async def protect_positions(session: AsyncSession, markets: dict[str, MarketData], instruments: InstrumentCache,
                            configs: dict[str, StrategyConfig], now: datetime) -> int:
    """Close OPEN demo positions whose stop or target the current bid has
    crossed. Returns how many were closed."""
    closed = 0
    rows = (await session.execute(select(DemoPosition).where(DemoPosition.status == "OPEN"))).scalars().all()
    for p in rows:
        md = markets.get(p.exchange)
        if md is None:
            continue
        try:
            book = await md.top_of_book(p.symbol)
            inst = await instruments.get(md, p.symbol)
        except Exception as e:  # noqa: BLE001 - the next loop retries; the failure is visible as an event
            session.add(Event(level="WARN", source="protect", message=f"{p.exchange} {p.symbol}: no price ({e})"[:500]))
            continue
        p.last_price = book.bid
        hit = ("stop_loss" if p.stop_price is not None and book.bid <= p.stop_price else
               "take_profit" if p.target_price is not None and book.bid >= p.target_price else None)
        if not hit:
            try:
                await manage_paper(session, p, configs.get(p.strategy_id), book, inst, md, now)
            except Exception as e:  # noqa: BLE001 - protection by stop / target above still ran
                session.add(Event(level="WARN", source="protect",
                                  message=f"{p.symbol}: trade management skipped ({e})"[:500]))
        if hit:
            cfg = configs.get(p.strategy_id)
            limits = RiskLimits.from_dict(cfg.risk if cfg else None)
            # Protection keeps working when the strategy is DISABLED: the exit
            # is still a simulated DEMO fill (there is no other kind).
            await demo.close_long(session, mode="DEMO", position=p, client_id=f"protect:{p.id}:{hit}", book=book,
                                  spec=inst, fee_bps=PROFILES[p.exchange].taker_fee_bps,
                                  slippage_bps=limits.slippage_bps, reason=hit, now=now)
            closed += 1
    await session.commit()
    return closed
