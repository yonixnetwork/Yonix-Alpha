"""Futures strategy runner: Meta Muse Crossover, Gold vs BTC Dual Trend and
Confluence Matrix on exchange (or MT5) market data, through the same safety
gate and position engine as the Solana engines.

Once per CLOSED candle per strategy (Redis-deduplicated, so restarts and
overlapping loops can't double-evaluate):
  1. fetch candles and the live order book from the strategy's venue;
  2. if the strategy already holds a position, apply the strategy's own
     exit rule (Meta Muse: trends weakened / signal changed) by requesting
     an exit, which paper-trading fills against the book on its next tick;
  3. otherwise, if the strategy signals, build the gate input (order-book
     liquidity model, candle volatility, the strategy's own stop/targets as
     STRATEGY levels), assess, persist, and open a paper position — filled
     against a freshly fetched book, so a moved market can fail the order.

Modes: the effective mode is the more restrictive of the strategy's mode
and its venue's mode (binance_futures / bybit_futures / hyperliquid_perps /
mt5_fx). A LIVE target (global mode LIVE, strategy AUTO/MANUAL, all three
environment locks open, the venue's execution worker ready) creates a
pending LIVE position and entry order (futures_live.enter) that
services/execution-futures executes; sizing then uses that exchange
account's synced balance. A standalone bot running the same strategy
(external control API reports a position) blocks LIVE for that strategy.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from yonixalpha_core import events, external_bots, futures_live, paper_engine
from yonixalpha_core.db.models import PaperPosition, RiskAssessment
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety import pipeline, store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import (
    AssessmentInput, GlobalMode, MarketInfo, Observation, StrategyLevels, StrategyMode,
)
from yonixalpha_core.safety.rules import evaluate_custom_rules, match_blacklist
from yonixalpha_core.strategies import confluence, gold_btc_trend, meta_muse
from yonixalpha_core.strategies.indicators import log_returns, stdev
from yonixalpha_core.venues.common import VenueError
from yonixalpha_core.venues.registry import VENUE_ENGINE

log = get_logger("decision-engine.futures")

STRATEGIES = {"meta_muse": meta_muse, "gold_btc_trend": gold_btc_trend, "confluence_matrix": confluence}
PAIR_STRATEGIES = {"meta_muse", "gold_btc_trend"}  # asset 1 leads, asset 2 is traded
DEFAULT_VENUE = {"meta_muse": "binance", "gold_btc_trend": "binance", "confluence_matrix": "binance"}
INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
VOL_LOOKBACK = 60


async def open_position_for(session, strategy: str, engine: str, symbol: str) -> PaperPosition | None:
    q = (select(PaperPosition).join(RiskAssessment, RiskAssessment.id == PaperPosition.assessment_id)
         .where(PaperPosition.status == "open", PaperPosition.engine == engine, PaperPosition.asset_id == symbol,
                RiskAssessment.strategy == strategy))
    return (await session.execute(q.limit(1))).scalar_one_or_none()


async def last_exit(session, strategy: str, engine: str, symbol: str) -> datetime | None:
    q = (select(PaperPosition.exit_at).join(RiskAssessment, RiskAssessment.id == PaperPosition.assessment_id)
         .where(PaperPosition.status == "closed", PaperPosition.engine == engine, PaperPosition.asset_id == symbol,
                RiskAssessment.strategy == strategy).order_by(PaperPosition.exit_at.desc()).limit(1))
    return (await session.execute(q)).scalar_one_or_none()


def candle_volatility(candles) -> Decimal | None:
    closes = [float(c.close) for c in candles if c.closed][-VOL_LOOKBACK - 1:]
    v = stdev(log_returns(closes))
    return Decimal(str(round(v, 8))) if v else None


async def _mode(session, strategy: str, engine: str) -> StrategyMode:
    return pipeline.effective_mode(await store.load_strategy_mode(session, strategy), await store.load_strategy_mode(session, engine))


async def run_strategy(session_factory, redis: Redis, app_settings: Any, venues: dict, strategy: str, now: datetime) -> dict:
    module = STRATEGIES[strategy]
    async with session_factory() as session:
        params = {**module.DEFAULTS, **await store.load_strategy_config(session, strategy)}
        venue = params.get("venue") or DEFAULT_VENUE[strategy]
        engine = VENUE_ENGINE[venue]
        mode = await _mode(session, strategy, engine)
    if mode == StrategyMode.OFF:
        return {"strategy": strategy, "status": "off"}
    adapter = venues[venue]
    interval = params["interval"]

    if strategy in PAIR_STRATEGIES:
        symbol = params["asset2"]
        c1 = await adapter.klines(params["asset1"], interval, int(params["candles"]), now)
        c2 = await adapter.klines(symbol, interval, int(params["candles"]), now)
        last_closed = [c for c in c2 if c.closed][-1].open_time
        sig = module.evaluate(c1, c2, params)
        side = sig.side
        strategy_signal = (meta_muse.as_strategy_signal(sig) if strategy == "meta_muse"
                           else gold_btc_trend.as_strategy_signal(sig, params))
        candles = c2
    else:
        symbol = params["symbol"]
        candles = await adapter.klines(symbol, interval, int(params["candles"]), now)
        last_closed = [c for c in candles if c.closed][-1].open_time
        csig = confluence.evaluate(candles, params)
        side = csig.side
        strategy_signal = confluence.as_strategy_signal(csig)

    dedupe = f"yx:fut:{strategy}:{venue}:{symbol}:{int(last_closed.timestamp())}"
    if not await redis.set(dedupe, "1", nx=True, ex=86400):
        return {"strategy": strategy, "status": "candle already evaluated"}

    async with session_factory() as session:
        position = await open_position_for(session, strategy, engine, symbol)
        if position is not None:
            if strategy in PAIR_STRATEGIES:
                should, why = module.should_exit(position.side, sig)
                if should and not position.exit_requested:
                    position.exit_requested = True
                    await store.add_timeline_event(session, "strategy_exit_signal", now, {"reason": why},
                                                   assessment_id=position.assessment_id, position_id=position.id)
                    await events.publish(redis, "position.updated", {"position_id": str(position.id), "exit_requested": True}, strategy)
                    await session.commit()
                    return {"strategy": strategy, "status": f"exit requested: {why}"}
            return {"strategy": strategy, "status": "holding"}

        if side is None:
            return {"strategy": strategy, "status": "no signal", "reasons": strategy_signal.reasons[:3]}

        if strategy == "confluence_matrix":
            prev = await last_exit(session, strategy, engine, symbol)
            cooldown = timedelta(seconds=INTERVAL_SECONDS.get(interval, 900) * int(params["cooldown_bars"]))
            if prev is not None and now - prev < cooldown:
                return {"strategy": strategy, "status": "cooldown after exit"}

        book = await adapter.book(symbol)
        approval = await pipeline.approval_granted(session, now, engine=engine, asset_id=symbol, strategy=strategy)
        live_intent = (await store.load_global_mode(session) == GlobalMode.LIVE
                       and mode in (StrategyMode.AUTO, StrategyMode.MANUAL) and store.live_trading_permitted(app_settings))
        controls, account, settings_meta = await pipeline.load_controls(session, redis, app_settings, engine, mode, symbol, now,
                                                                        approval, live_venue=venue if live_intent else None)
        live_ready, live_reason = (None, None)
        if live_intent:
            live_ready, live_reason = await futures_live.readiness(redis, app_settings, venue, now)
            conflict = await external_bots.conflict(redis, app_settings, strategy)
            if live_ready and conflict:
                live_ready, live_reason = False, conflict
        mid = book.mid
        if strategy in PAIR_STRATEGIES:
            sign = Decimal(1) if side == "LONG" else Decimal(-1)
            levels = StrategyLevels(
                stop_loss=mid * (1 - sign * Decimal(str(params["stop_pct"]))),
                take_profits=[mid * (1 + sign * Decimal(str(params["target_pct"])))],
                source=f"{strategy} {Decimal(str(params['stop_pct'])) * 100:g}% stop / "
                       f"{Decimal(str(params['target_pct'])) * 100:g}% target",
            )
        else:
            levels = confluence.levels(csig)

        inp = AssessmentInput(
            engine=engine, strategy_name=strategy, asset_id=symbol, symbol=symbol, now=now,
            market=MarketInfo(Observation(f"{venue}:book", now), price=mid, volatility=candle_volatility(candles),
                              liquidity_quote=book.liquidity_quote, age_seconds=None),
            token=None, holders=None, flow=None, account=controls.account, liquidity_model=book,
            signal=strategy_signal, side=side, strategy_levels=levels,
            global_mode=controls.global_mode, strategy_mode=mode, live_trading_permitted=controls.live_trading_permitted,
            manual_approval_granted=approval,
        )
        inp.live_ready, inp.live_not_ready_reason = live_ready, live_reason
        inp.blacklisted_by = match_blacklist(controls.blacklist, engine, None, symbol, symbol)
        features = {"price": mid, "volatility": inp.market.volatility, "liquidity_quote": book.liquidity_quote,
                    "spread_bps": book.spread_bps, "side": side, "signal_strength": strategy_signal.strength}
        inp.rule_actions = evaluate_custom_rules(controls.custom_rules, engine, features)
        inp.ml, ml_info = await pipeline.champion_prediction(session, redis, engine, features)
        vers = await pipeline.versions(session, settings_meta, strategy_signal, f"{venue}_book")
        vers["ml_model"] = f"{ml_info['model']} v{ml_info['version']}" if inp.ml else None
        a = assess(inp, controls.settings, versions=vers)
        ml_info["influenced"] = pipeline.ml_influenced(a)
        a.inputs_snapshot = {"venue": venue, "interval": interval, "candle": last_closed.isoformat(),
                             "features": {k: str(v) for k, v in features.items()}, "signal": strategy_signal.reasons,
                             "ml": ml_info}
        row, created = await store.persist_assessment(session, a, None, store.assessment_key(engine, f"{strategy}:{symbol}",
                                                                                          str(int(last_closed.timestamp()))))
        if not created:
            await session.commit()
            return {"strategy": strategy, "status": "duplicate"}
        await pipeline.after_decision(session, redis, app_settings, a, row, f"{strategy}:{symbol}")
        await pipeline.after_ml(redis, a, row, ml_info)

        result = {"strategy": strategy, "status": a.decision.value, "side": side}
        if a.executable and a.execution_target.value == "LIVE":
            pipeline.record_ml_sample(session, a, row.id, None, {k: str(v) for k, v in features.items()},
                                      *pipeline.ml_sample_args(inp.ml, ml_info))
            try:
                position = await futures_live.enter(
                    session, account, a, row.id, venue, symbol, now,
                    {"venue": {"strategy": strategy, "interval": interval}, "model_version": vers.get("ml_model"),
                     "feature_version": ml_info.get("feature_version")})
                await events.notify(session, redis, app_settings, "entry", f"LIVE entry submitted: {symbol} {a.plan.side}",
                                    f"{venue}: notional {a.plan.position_size.value}, stop {a.plan.stop_loss.value}", "warning",
                                    {"position_id": str(position.id)})
                result["status"] = "LIVE entry submitted"
            except ValueError as exc:
                await store.add_timeline_event(session, "live_entry_refused", now, {"reason": str(exc)}, assessment_id=row.id)
                result["status"] = f"live entry refused: {exc}"
        elif a.executable:
            pipeline.record_ml_sample(session, a, row.id, None, {k: str(v) for k, v in features.items()},
                                      *pipeline.ml_sample_args(inp.ml, ml_info))
            try:
                fill_book = await adapter.book(symbol)
                position = await paper_engine.open_position(
                    session, account, a, row.id, None, book, None, None, now,
                    venue={"type": f"{venue}_book", "kind": "futures", "venue": venue, "symbol": symbol, "strategy": strategy},
                    fill_model=fill_book, max_slippage_bps=controls.settings.max_slippage_bps,
                )
                await pipeline.after_entry(session, redis, app_settings, a, position)
            except (paper_engine.FillError, VenueError) as exc:
                await store.add_timeline_event(session, "paper_entry_failed", now, {"reason": str(exc)}, assessment_id=row.id)
                result["status"] = f"entry failed: {exc}"
        await session.commit()
        log.info("futures.decision", **{k: str(v) for k, v in result.items()})
        return result


async def run_all(session_factory, redis: Redis, app_settings: Any, venues: dict, now: datetime) -> list[dict]:
    out = []
    for strategy in STRATEGIES:
        try:
            out.append(await run_strategy(session_factory, redis, app_settings, venues, strategy, now))
        except (VenueError, ValueError) as exc:
            # Venue down or not enough history: no decision, and nothing invented.
            out.append({"strategy": strategy, "status": f"skipped: {exc}"})
            log.warning("futures.skipped", strategy=strategy, error=str(exc))
    return out
