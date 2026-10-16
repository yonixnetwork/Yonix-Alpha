"""The Crypto Trading worker loop (services/crypto-trading).

States, worst first:
- WAITING_FOR_SCHEMA: the crypto migrations have not run yet;
- KILL_SWITCH: the platform kill switch is engaged (read only, never set
  here): no new entries;
- EMERGENCY_STOP: this engine's own stop: no new entries;
- DISABLED: the engine switch is off (the default): no new entries;
- IDLE: on, but no strategy is enabled;
- RUNNING: at least one enabled DEMO strategy is evaluated.
In every state except WAITING_FOR_SCHEMA, open demo positions keep their
stop / target protection (runner.protect_positions), and exchange-account
trades are reconciled and managed (private.service). A strategy that holds
a position but may not enter (it is disabled, or the engine is not IDLE /
RUNNING) is still evaluated for its EXIT rule only, once per closed candle.

Exit when idle (worker mode): while the engine is OFF or in emergency stop
and no demo position is open, the worker exits with status 0 to free its
memory (compose restarts it only after a failure); it never exits while an
exchange-account trade is still active. It is started again by
the next deploy or by `docker compose ... up -d crypto-trading`, and exits
again at once if there is still nothing to do.

Load: one config read per loop (default 30 s); strategies are evaluated
only right after a candle of their interval closes; the exchange health
check runs every health_every_seconds while the engine is on, or once when
the dashboard asks for it. Under memory pressure (RSS above 85% of the
container limit) new entries pause and protection continues."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select

from yonixalpha_crypto import REDIS_PREFIX, config_store, registry, resources
from yonixalpha_crypto.candles import INTERVAL_SECONDS
from yonixalpha_crypto.config import CryptoSettings, load_settings
from yonixalpha_crypto.db import (EXEC_ACTIVE, DemoPosition, EngineState, Event, ExecTrade, StrategyConfig,
                                  make_session_factory, schema_ready)
from yonixalpha_crypto.exchanges import EXCHANGES, market_data
from yonixalpha_crypto.exchanges.base import MarketData
from yonixalpha_crypto.guard import ACTIVE_MODES
from yonixalpha_crypto.runner import InstrumentCache, protect_positions, step_strategy
from yonixalpha_crypto.strategies import implementation

log = logging.getLogger("crypto-trading")

# The platform kill switch (yonixalpha_core.kill_switch). Read only; the
# constant is checked against yonixalpha_core in tests.
KILL_SWITCH_KEY = "yonixalpha:kill_switch:engaged"
HEARTBEAT_KEY = REDIS_PREFIX + "heartbeat"
HEALTH_KEY = REDIS_PREFIX + "health"
HEALTH_REQUEST_KEY = REDIS_PREFIX + "health_request"
HEALTH_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
MEMORY_PRESSURE = 0.85


async def health_check(markets: dict[str, MarketData], now: datetime) -> dict[str, Any]:
    """Public reachability and listing of BTC / ETH / SOL per exchange."""
    out: dict[str, Any] = {}
    for ex, md in markets.items():
        entry: dict[str, Any] = {"checked_at": now.isoformat(), "ok": False, "symbols": {}}
        try:
            await md.ping()
            entry["ok"] = True
            for sym in HEALTH_SYMBOLS:
                try:
                    spec = await md.instrument(sym)
                    entry["symbols"][sym] = {"listed": True, "tradable": spec.tradable, "status": spec.status,
                                             "tick_size": str(spec.tick_size), "qty_step": str(spec.qty_step),
                                             "min_notional": str(spec.min_notional)}
                except Exception as e:  # noqa: BLE001
                    entry["symbols"][sym] = {"listed": False, "error": str(e)[:200]}
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)[:300]
            entry["status"] = getattr(e, "status", None)
        entry["last_calls"] = [c.__dict__ for c in getattr(getattr(md, "http", None), "calls", [])[-5:]]
        out[ex] = entry
    return out


def candle_due(interval: str, now: datetime, last_done: datetime | None) -> bool:
    """True once per closed candle: a new interval boundary passed since the
    last evaluation (plus 5 s so the exchange has published the candle)."""
    step = INTERVAL_SECONDS[interval]
    boundary = int((now.timestamp() - 5) // step) * step
    return last_done is None or last_done.timestamp() < boundary


class Engine:
    def __init__(self, settings: CryptoSettings, *, markets: dict[str, MarketData] | None = None,
                 redis: Redis | None = None, session_factory=None, exit_when_idle: bool = False):
        self.settings = settings
        self.exit_when_idle = exit_when_idle
        if session_factory is None:
            self._db_engine, self.sf = make_session_factory(settings.database_url)
        else:
            self._db_engine, self.sf = None, session_factory
        self.redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)
        overrides = dict(settings.base_urls)
        self.markets = markets if markets is not None else {ex: market_data(ex, overrides.get(ex)) for ex in EXCHANGES}
        self.instruments = InstrumentCache()
        self.last_eval: dict[str, datetime] = {}
        self.last_health: datetime | None = None
        self.stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        mem = resources.memory()
        async with self.sf() as s:
            if not await schema_ready(s):
                hb = {"state": "WAITING_FOR_SCHEMA"}
                await self._heartbeat(now, hb, mem)
                return hb
            await config_store.ensure_defaults(s)
            await s.commit()
            eng = await s.get(EngineState, 1)
            configs = {c.strategy_id: c for c in (await s.execute(select(StrategyConfig))).scalars()}
            holding = set((await s.execute(select(DemoPosition.strategy_id).where(
                DemoPosition.status == "OPEN"))).scalars())
            holding |= set((await s.execute(select(ExecTrade.strategy_id).where(
                ExecTrade.status.in_(EXEC_ACTIVE)))).scalars())
        killed = bool(await self.redis.get(KILL_SWITCH_KEY))
        state = ("KILL_SWITCH" if killed else "EMERGENCY_STOP" if eng.emergency_stop else
                 "DISABLED" if not eng.enabled else "IDLE")
        pressure = mem.get("limit_mb") and mem.get("rss_mb") and mem["rss_mb"] > MEMORY_PRESSURE * mem["limit_mb"]
        result: dict[str, Any] = {"state": state, "evaluated": {}, "closed_by_protection": 0,
                                  "memory_pressure": bool(pressure)}

        # Protection first, in every state.
        async with self.sf() as s:
            open_n = await s.scalar(select(func.count()).select_from(DemoPosition).where(DemoPosition.status == "OPEN"))
            if open_n:
                result["closed_by_protection"] = await protect_positions(s, self.markets, self.instruments, configs, now)
        result["open_positions"] = int(open_n or 0) - int(result["closed_by_protection"])

        # Exchange-account trades (practice accounts; live ones only after the code
        # unlock): reconciled and managed in EVERY state, like demo protection.
        async with self.sf() as s:
            exec_n = int(await s.scalar(select(func.count()).select_from(ExecTrade)
                                        .where(ExecTrade.status.in_(EXEC_ACTIVE))) or 0)
        result["exchange_trades"] = exec_n
        if exec_n:
            from yonixalpha_crypto.private.service import run_exchange_accounts  # loaded only when needed
            try:
                result["exchange_accounts"] = await run_exchange_accounts(self.sf, now)
            except Exception as e:  # noqa: BLE001 - logged and visible; retried next loop
                log.exception("exchange accounts pass failed")
                await self._event("ERROR", "execution", f"exchange accounts pass failed: {e}"[:500])

        for sid, cfg in configs.items():
            spec, impl = registry.get(sid), implementation(sid)
            if not (spec and impl):
                continue
            entries = (state == "IDLE" and cfg.enabled and cfg.mode in ACTIVE_MODES
                       and spec.status == registry.IMPLEMENTED)
            exits_only = None
            if entries:
                result["state"] = "RUNNING"
                if pressure:
                    result["evaluated"][sid] = "paused: memory pressure"
                    continue
            elif sid in holding:  # never abandon an open trade's exit rule
                exits_only = (f"exits only: engine {state.lower().replace('_', ' ')}" if state != "IDLE"
                              else "exits only: strategy disabled")
            else:
                continue
            if not candle_due(cfg.interval, now, self.last_eval.get(sid)):
                continue
            md = self.markets.get(cfg.exchange)
            if md is None:
                continue
            try:
                async with self.sf() as s:
                    result["evaluated"][sid] = await step_strategy(s, cfg, spec, impl, md, self.instruments, now,
                                                                   exits_only=exits_only)
                self.last_eval[sid] = now
            except Exception as e:  # noqa: BLE001 - one strategy's failure never stops the others
                result["evaluated"][sid] = f"error: {type(e).__name__}"
                await self._event("ERROR", sid, f"evaluation failed: {e}"[:500])
                self.last_eval[sid] = now  # retried at the next candle, not in a tight loop

        requested = await self.redis.getdel(HEALTH_REQUEST_KEY)
        due = eng.enabled and (self.last_health is None or
                               (now - self.last_health).total_seconds() >= self.settings.health_every_seconds)
        if requested or (due and not pressure):
            health = await health_check(self.markets, now)
            await self.redis.set(HEALTH_KEY, json.dumps(health), ex=7 * 86400)
            self.last_health = now
            result["health_checked"] = True
        await self._heartbeat(now, result, mem)
        return result

    @staticmethod
    def idle(result: dict[str, Any]) -> bool:
        """Nothing to do: no entries are allowed and nothing needs protection."""
        return result.get("state") in ("DISABLED", "EMERGENCY_STOP", "WAITING_FOR_SCHEMA") and \
            not result.get("open_positions") and not result.get("exchange_trades")

    async def _heartbeat(self, now: datetime, result: dict[str, Any], mem: dict[str, Any]) -> None:
        await self.redis.set(HEARTBEAT_KEY, json.dumps({"at": now.isoformat(), **result, "memory": mem}, default=str),
                             ex=max(300, int(self.settings.loop_seconds * 4)))

    async def _event(self, level: str, source: str, message: str) -> None:
        async with self.sf() as s:
            s.add(Event(level=level, source=source, message=message))
            await s.commit()

    async def run(self) -> None:
        log.info("crypto-trading started (DEMO only; engine state is read from crypto.engine_state)")
        while not self.stop.is_set():
            try:
                result = await self.tick()
                if self.exit_when_idle and self.idle(result):
                    log.info("engine is off (or in emergency stop) and no demo position is open: exiting to free "
                             "memory; it starts again with the next deploy or `up -d crypto-trading`")
                    break
            except Exception as e:  # noqa: BLE001 - keep the loop alive; the error is logged and visible
                log.exception("tick failed: %s", e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.settings.loop_seconds)
            except asyncio.TimeoutError:
                pass
        await self.aclose()

    async def aclose(self) -> None:
        for md in self.markets.values():
            await md.aclose()
        await self.redis.aclose()
        if self._db_engine is not None:
            await self._db_engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = Engine(load_settings(), exit_when_idle=True)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, engine.stop.set)
    loop.run_until_complete(engine.run())
