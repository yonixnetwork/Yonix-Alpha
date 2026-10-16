"""The backtest worker: works through the queue one run at a time.

    python -m yonixalpha_crypto.backtest.worker --once          # one run, then exit
    python -m yonixalpha_crypto.backtest.worker --max-runs 60   # until the queue is empty or 60 runs

Server-safe by design: one run at a time, lowest CPU priority (nice 19),
bounded data (backtest.data.MAX_BARS), refuses to start while
portfolio_settings.limits.backtests_enabled is false, stops between runs
when a batch is cancelled. Run it with the crypto-trading service's memory
and CPU limits:
    docker compose ... run --rm crypto-trading python -m yonixalpha_crypto.backtest.worker --max-runs 60
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone

from yonixalpha_crypto import portfolio
from yonixalpha_crypto.backtest import data, jobs, validation
from yonixalpha_crypto.backtest.engine import Costs, RiskConfig
from yonixalpha_crypto.config import load_settings
from yonixalpha_crypto.db import BacktestBatch, BacktestRun, Event, make_session_factory
from yonixalpha_crypto.exchanges import market_data
from yonixalpha_crypto.exchanges.profiles import PROFILES
from yonixalpha_crypto.strategies.library import BY_ID

log = logging.getLogger("backtest-worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_one(sf, md_factory=None, now=None) -> str | None:
    """Claim and execute one run. Returns its final status, or None when the
    queue is empty."""
    async with sf() as s:
        await jobs.recover_stale(s)
        r = await jobs.claim(s)
        await s.commit()
    if r is None:
        return None
    md = None
    try:
        b = await _batch(sf, r)
        cfg = b.config
        start, end = datetime.fromisoformat(cfg["start"]), datetime.fromisoformat(cfg["end"])
        m = BY_ID[r.strategy_id]
        md = (md_factory or market_data)(r.exchange)
        source = PROFILES[r.exchange].base_url if r.exchange in PROFILES else r.exchange
        await _phase(sf, r, "loading data")
        fetch = {}
        for sym in ([r.config["lead"]] if r.config.get("lead") else []) + [r.symbol]:
            fetch[sym] = await data.ensure_history(sf, md, r.exchange, sym, r.interval, start, end, source, now)
        series, prov = await data.load(sf, r.exchange, r.symbol, r.interval, start, end)
        prov["fetch"] = fetch
        if r.config.get("lead"):
            lead, lprov = await data.load(sf, r.exchange, r.config["lead"], r.interval, start, end)
            _, series = data.align(lead, series)
            prov["lead"] = lprov
        await _phase(sf, r, f"studying {len(series)} bars")
        costs = Costs(**{k: float(v) for k, v in (r.config.get("costs") or {}).items()})
        risk = RiskConfig(**{k: (v if k == "mode" else float(v)) for k, v in (r.config.get("risk") or {}).items()})
        result = validation.study(series, m, r.config.get("params") or {}, r.config.get("exits"), costs, risk,
                                  r.interval, optimize=bool(r.config.get("optimize", True)))
        status = "FAILED" if "error" in result else "DONE"
        if status == "DONE":
            data.apply_data_quality(result, prov, int(fetch[r.symbol]["expected_bars"]))
            result["param_source"] = r.config.get("param_source", "defaults")
            if result["param_source"] == "saved":
                result["verdict"]["reasons"].append(jobs.SAVED_PARAMS_NOTE)
        async with sf() as s:
            row = await s.get(BacktestRun, r.id)
            row.status, row.finished_at, row.phase = status, _now(), None
            row.data, row.result, row.summary = prov, result, jobs.summary(result)
            row.error = result.get("error")
            row.config = {**row.config, "costs_effective": costs.__dict__, "risk_effective": risk.__dict__}
            await jobs.refresh_batch(s, await s.get(BacktestBatch, r.batch_id))
            s.add(Event(at=_now(), level="INFO" if status == "DONE" else "WARN", source="backtest",
                        message=f"backtest {status.lower()}: {r.strategy_id} {r.symbol} {r.interval}"
                                + (f" ({result['verdict']['tier']})" if status == "DONE" else f": {row.error}"),
                        detail={"run": str(r.id), "batch": str(r.batch_id)}))
            await s.commit()
        return status
    except Exception as e:  # noqa: BLE001 - recorded on the run; the queue continues
        async with sf() as s:
            row = await s.get(BacktestRun, r.id)
            row.status, row.finished_at, row.error = "FAILED", _now(), f"{type(e).__name__}: {e}"[:1000]
            row.result = {"traceback": traceback.format_exc()[-3000:]}
            await jobs.refresh_batch(s, await s.get(BacktestBatch, r.batch_id))
            s.add(Event(at=_now(), level="ERROR", source="backtest",
                        message=f"backtest failed: {r.strategy_id} {r.symbol}: {e}"[:500], detail={"run": str(r.id)}))
            await s.commit()
        return "FAILED"
    finally:
        if md is not None:
            await md.aclose()


async def _batch(sf, r: BacktestRun) -> BacktestBatch:
    async with sf() as s:
        return await s.get(BacktestBatch, r.batch_id)


async def _phase(sf, r: BacktestRun, phase: str) -> None:
    async with sf() as s:
        row = await s.get(BacktestRun, r.id)
        row.phase, row.heartbeat_at = phase, _now()
        await s.commit()


async def main_async(once: bool, max_runs: int) -> int:
    settings = load_settings()
    engine, sf = make_session_factory(settings.database_url)
    try:
        async with sf() as s:
            limits = await portfolio.load(s)
        if not limits.get("backtests_enabled"):
            print("Backtests are switched off (Crypto Trading > Portfolio risk > backtests). Nothing was run.")
            return 2
        done = 0
        while done < max_runs:
            st = await run_one(sf)
            if st is None:
                print("queue empty")
                break
            done += 1
            print(f"run {done}: {st}", flush=True)
            if once:
                break
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="YonixAlpha backtest worker (one run at a time, low priority)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--max-runs", type=int, default=60)
    a = p.parse_args()
    try:
        os.nice(19)
    except OSError:
        pass
    raise SystemExit(asyncio.run(main_async(a.once, max(1, min(a.max_runs, 500)))))


if __name__ == "__main__":
    main()
