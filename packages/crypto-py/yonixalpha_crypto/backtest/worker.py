"""The database queue worker for backtests.

DISABLED on the server by policy (backtest.policy): historical backtests run
locally (`python -m yonixalpha_crypto.backtest.local`), never on the
production VPS. `main` refuses to start. The queue code (run_one) is kept
for tests and shares `execute` with the local runner, so both compute a
run in exactly the same way."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone

from yonixalpha_crypto import portfolio
from yonixalpha_crypto.backtest import data, jobs, policy, run
from yonixalpha_crypto.config import load_settings
from yonixalpha_crypto.db import BacktestBatch, BacktestRun, Event, make_session_factory
from yonixalpha_crypto.exchanges import market_data
from yonixalpha_crypto.exchanges.profiles import PROFILES

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
        out = run.execute(series, prov, r.strategy_id, r.interval, r.config, int(fetch[r.symbol]["expected_bars"]))
        status, result = out["status"], out["result"]
        async with sf() as s:
            row = await s.get(BacktestRun, r.id)
            row.status, row.finished_at, row.phase = status, _now(), None
            row.data, row.result, row.summary = prov, result, out["summary"]
            row.error = out["error"]
            row.config = {**row.config, "costs_effective": out["costs_effective"], "risk_effective": out["risk_effective"]}
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
    if not policy.SERVER_BACKTESTS_ENABLED:
        print("Refused: " + policy.SERVER_REFUSAL + ". Nothing was run.")
        return 2
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
