"""The backtest job queue: a batch is one request (strategies x symbols); a
run is one strategy on one symbol. Runs are claimed one at a time
(SELECT ... FOR UPDATE SKIP LOCKED), so a second worker cannot take the
same run; a run whose worker died (no heartbeat for STALE_AFTER) is put
back in the queue, at most MAX_ATTEMPTS times. Cancelling a batch cancels
its queued runs; a running one stops after its current study."""

from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto.db import BacktestBatch, BacktestRun, StrategyConfig
from yonixalpha_crypto.strategies.library import BY_ID

STALE_AFTER = timedelta(minutes=20)
MAX_ATTEMPTS = 2
MAX_RUNS_PER_BATCH = 120
FINAL = ("DONE", "FAILED", "CANCELLED", "SKIPPED")
_PKG = Path(__file__).resolve().parents[1]
_CODE_FILES = ("strategies/indicators.py", "strategies/library.py", "strategies/meta_muse.py",
               "backtest/engine.py", "backtest/metrics.py", "backtest/validation.py", "private/management.py")


def code_version() -> str:
    """A fingerprint of every file that decides a backtest result (CRC32 of
    each file's bytes: identifies a version, it is not a security hash)."""
    crc = 0
    for f in _CODE_FILES:
        crc = zlib.crc32((_PKG / f).read_bytes(), crc)
    return f"src-{crc:08x}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BatchError(ValueError):
    pass


PARAM_SOURCES = ("defaults", "saved")
SAVED_PARAMS_NOTE = ("saved parameters: if they were chosen after looking at earlier out-of-sample results, these "
                     "out-of-sample figures are no longer independent evidence")


async def create_batch(s: AsyncSession, *, strategies: list[str], symbols: list[str], interval: str, exchange: str,
                       start: datetime, end: datetime, costs: dict[str, Any], risk: dict[str, Any],
                       optimize: bool, actor: str | None, note: str | None = None,
                       param_source: str = "defaults") -> BacktestBatch:
    """param_source: "defaults" (the module's published defaults) or "saved"
    (each strategy's saved parameters and exits from its settings page)."""
    unknown = [x for x in strategies if x not in BY_ID]
    if unknown:
        raise BatchError(f"unknown strategies: {unknown}")
    if not symbols or end <= start:
        raise BatchError("symbols and a valid date range are required")
    if param_source not in PARAM_SOURCES:
        raise BatchError(f"param_source must be one of {list(PARAM_SOURCES)}")
    saved: dict[str, StrategyConfig] = {}
    if param_source == "saved":
        saved = {c.strategy_id: c for c in (await s.execute(
            select(StrategyConfig).where(StrategyConfig.strategy_id.in_(strategies)))).scalars()}
    cfg = {"strategies": strategies, "symbols": symbols, "interval": interval, "exchange": exchange,
           "start": start.isoformat(), "end": end.isoformat(), "costs": costs, "risk": risk, "optimize": optimize,
           "param_source": param_source}
    b = BacktestBatch(status="QUEUED", config=cfg, created_at=_now(), created_by=actor, note=note)
    s.add(b)
    await s.flush()
    runs = 0
    for sid in strategies:
        m = BY_ID[sid]
        targets = [symbols[-1]] if m.needs_lead else symbols
        for sym in targets:
            status, err = "QUEUED", None
            if m.needs_lead and len(symbols) < 2:
                status, err = "SKIPPED", "pair strategy: needs a leading and a traded symbol (e.g. BTC/USDT, ETH/USDT)"
            if interval not in m.intervals:
                status, err = "SKIPPED", f"{sid} supports {list(m.intervals)}, not {interval}"
            sc = saved.get(sid)
            params = dict(sc.params or {}) if sc is not None else {}
            exits = (sc.exits or m.default_exits) if sc is not None else m.default_exits
            s.add(BacktestRun(batch_id=b.id, strategy_id=sid, exchange=exchange, symbol=sym, interval=interval,
                              status=status, error=err, created_at=_now(),
                              config={"params": params, "exits": exits, "costs": costs, "risk": risk,
                                      "lead": symbols[0] if m.needs_lead else None, "optimize": optimize,
                                      "module_version": m.version,
                                      "param_source": param_source if sc is not None or param_source == "defaults"
                                      else "defaults (nothing saved)",
                                      "settings_version": sc.version if sc is not None else None}))
            runs += 1
    if runs > MAX_RUNS_PER_BATCH:
        raise BatchError(f"{runs} runs: at most {MAX_RUNS_PER_BATCH} per batch")
    b.total = runs
    return b


async def cancel_batch(s: AsyncSession, batch_id) -> int:
    b = await s.get(BacktestBatch, batch_id)
    if b is None:
        raise BatchError("no such batch")
    b.cancelled = True
    res = await s.execute(update(BacktestRun).where(BacktestRun.batch_id == batch_id, BacktestRun.status == "QUEUED")
                          .values(status="CANCELLED", finished_at=_now(), error="cancelled by the operator"))
    await refresh_batch(s, b)
    return res.rowcount or 0


async def recover_stale(s: AsyncSession, now: datetime | None = None) -> int:
    now = now or _now()
    stale = (await s.execute(select(BacktestRun).where(
        BacktestRun.status == "RUNNING", BacktestRun.heartbeat_at < now - STALE_AFTER).with_for_update())).scalars().all()
    for r in stale:
        if r.attempts >= MAX_ATTEMPTS:
            r.status, r.finished_at, r.error = "FAILED", now, f"worker stopped {r.attempts} times during this run"
        else:
            r.status, r.phase = "QUEUED", "requeued after a worker stop"
    return len(stale)


async def claim(s: AsyncSession, now: datetime | None = None) -> BacktestRun | None:
    now = now or _now()
    q = (select(BacktestRun).join(BacktestBatch, BacktestBatch.id == BacktestRun.batch_id)
         .where(BacktestRun.status == "QUEUED", BacktestBatch.cancelled.is_(False))
         .order_by(BacktestRun.created_at).limit(1).with_for_update(skip_locked=True, of=BacktestRun))
    r = (await s.execute(q)).scalar_one_or_none()
    if r is None:
        return None
    r.status, r.attempts, r.started_at, r.heartbeat_at, r.phase = "RUNNING", r.attempts + 1, now, now, "claimed"
    r.code_version = code_version()
    b = await s.get(BacktestBatch, r.batch_id)
    b.status = "RUNNING"
    return r


async def refresh_batch(s: AsyncSession, b: BacktestBatch) -> None:
    counts = dict((await s.execute(select(BacktestRun.status, func.count()).where(BacktestRun.batch_id == b.id)
                                   .group_by(BacktestRun.status))).all())
    pending = sum(v for k, v in counts.items() if k not in FINAL)
    if pending == 0:
        b.status = "CANCELLED" if b.cancelled else "DONE"
        b.finished_at = b.finished_at or _now()
    elif counts.get("RUNNING"):
        b.status = "RUNNING"


def summary(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"error": result["error"]}
    oos = result["default_params"]["metrics"]["out_of_sample"]
    return {"tier": result["verdict"]["tier"], "rank_key": result["verdict"]["rank_key"],
            "oos_trades": oos["trades"], "oos_net_return_pct": oos["net_return_pct"],
            "oos_expectancy_r": oos["expectancy_r"], "oos_profit_factor": oos["profit_factor"],
            "oos_max_drawdown_pct": oos["max_drawdown_pct"], "oos_win_rate": oos["win_rate"],
            "oos_sharpe": oos["sharpe"], "wf_positive_folds": result["walk_forward"]["positive_folds"],
            "robust": result["sensitivity"]["robust"],
            "benchmark_oos_return_pct": result["benchmark_buy_and_hold_oos"]["return_pct"],
            "oos_period": result["splits"]["out_of_sample"],
            "data_coverage_pct": (result.get("data_quality") or {}).get("coverage_pct"),
            "data_gaps": (result.get("data_quality") or {}).get("gaps")}
