"""Crypto Trading: Strategy Center, backtests, comparison, portfolio risk and
alerts. Everything is read from the crypto schema; nothing here is
estimated or filled in. Backtest, paper, exchange-demo and live results
are kept apart; live is reported as locked.

    GET  /crypto/center/strategies            the registry with configuration, lifecycle status and latest results
    GET  /crypto/center/strategies/{id}       one strategy: rules, research, settings schema, positions, signals,
                                              performance by mode, backtest runs
    GET  /crypto/center/rejected              candidates merged into others or rejected, with reasons
    GET  /crypto/backtests                    batches with progress
    POST /crypto/backtests                    queue a batch (strategies x symbols)
    GET  /crypto/backtests/{batch_id}         its runs, ranked by the documented rule
    POST /crypto/backtests/{batch_id}/cancel
    GET  /crypto/backtests/runs/{run_id}      one run's full study
    GET  /crypto/compare?runs=a,b,...         curves, metrics, return correlation and time-in-market overlap
    GET  /crypto/portfolio | PUT /crypto/portfolio
    GET  /crypto/alerts                       events (risk, execution, backtests, strategies)
    GET  /crypto/audit                        configuration and mode changes"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from yonixalpha_crypto import config_store, metrics as demo_metrics, portfolio, registry
from yonixalpha_crypto.backtest import jobs
from yonixalpha_crypto.backtest.validation import TIERS
from yonixalpha_crypto.db import (ApiConnection, BacktestBatch, BacktestRun, ConfigAudit, DemoPosition, Event,
                                  ExecTrade, Signal, StrategyConfig, schema_ready)
from yonixalpha_crypto.private.livelock import LIVE_ORDERS_UNLOCKED
from yonixalpha_crypto.private.management import validate_plan
from yonixalpha_crypto.risk import RISK_RULES
from yonixalpha_crypto.strategies.library import BY_ID, COMMON_PARAMS, REJECTED

router = APIRouter(tags=["crypto"])
EXEC_ACTIVE = ("PENDING_ENTRY", "OPEN", "CLOSING", "UNCERTAIN")
PARAM_HELP = {
    "atr_period": "bars in the Average True Range used for the stop",
    "stop_atr": "stop distance below the entry, in ATRs",
    "target_r": "target as a multiple of the risk (0 = no fixed target)",
    "min_volume_ratio": "only enter when volume is at least this multiple of its 20-bar average (0 = off)",
}


def _j(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_j(x) for x in v]
    return v


async def _ready(db: AsyncSession) -> None:
    if not await schema_ready(db):
        raise HTTPException(503, "The crypto schema is not installed yet.")


def lifecycle(spec: registry.StrategySpec, cfg: StrategyConfig | None, latest: Any | None,
              agg: dict[str, Any] | None = None) -> dict[str, str]:
    """research | blocked | implemented | backtest pending | backtest failed | paper testing | demo testing |
    approved for consideration. Never 'live'. (Merged / rejected candidates are listed by /crypto/center/rejected;
    whether a strategy is switched on is shown by its mode, not by this status.)

    `latest` is the WORST finished run of the strategy's latest batch (every
    symbol counts); `agg` holds that batch's counts. A strategy is approved for
    consideration only when every symbol finished and every one is a candidate."""
    if spec.status == registry.BLOCKED:
        return {"status": "blocked", "why": spec.status_reason}
    if spec.status in (registry.NOT_IMPLEMENTED, registry.RESEARCH_ONLY):
        return {"status": "research", "why": spec.status_reason}
    if cfg is not None and cfg.enabled and cfg.mode == "EXCHANGE_DEMO":
        return {"status": "demo testing", "why": "enabled on a practice (demo / testnet) connection"}
    if cfg is not None and cfg.enabled and cfg.mode == "DEMO":
        return {"status": "paper testing", "why": "enabled in paper mode (simulated fills on live data)"}
    if latest is None:
        return {"status": "backtest pending", "why": "no backtest has run yet"}
    if latest.status == "FAILED":
        return {"status": "backtest failed", "why": latest.error or "the run failed"}
    tier = (latest.summary or {}).get("tier")
    agg = agg or {}
    of = f" (worst of {agg['finished']} symbol runs)" if agg.get("finished", 1) > 1 else ""
    if tier == "candidate":
        if agg.get("errored") or agg.get("pending"):
            return {"status": "implemented", "why": f"candidate on the finished runs, but {agg.get('errored', 0)} "
                                                    f"failed and {agg.get('pending', 0)} still queued in this batch"}
        return {"status": "approved for consideration", "why": "positive out-of-sample result passed the documented "
                                                               f"checks on every symbol{of}; paper / demo testing "
                                                               "next (never live)"}
    if tier == "failed":
        return {"status": "backtest failed", "why": f"negative out-of-sample expectancy or profit factor below 1{of}"}
    # The status is the validation stage; whether the strategy is switched on is its mode.
    return {"status": "implemented", "why": f"latest backtest: {tier or latest.status}{of}"}


RUN_COLS = (BacktestRun.id, BacktestRun.strategy_id, BacktestRun.batch_id, BacktestRun.status, BacktestRun.symbol,
            BacktestRun.interval, BacktestRun.finished_at, BacktestRun.summary, BacktestRun.error)


def _worse(r) -> tuple:
    """Sort key: larger = worse (tier, then lower expectancy, then deeper drawdown)."""
    k = (r.summary or {}).get("rank_key") or [len(TIERS), 0, 0]
    return tuple(float(x or 0) for x in k)


async def _latest_runs(db: AsyncSession, sid: str | None = None) -> dict[str, tuple[Any, dict[str, Any]]]:
    """Per strategy: (worst finished run, counts) over its LATEST batch, i.e.
    the batch of its most recently finished run. Light columns only (no
    study JSON)."""
    q = (select(BacktestRun.strategy_id, BacktestRun.batch_id, BacktestRun.finished_at)
         .where(BacktestRun.status.in_(("DONE", "FAILED"))))
    if sid is not None:
        q = q.where(BacktestRun.strategy_id == sid)
    newest: dict[str, tuple[Any, Any]] = {}
    for s_id, b_id, f in (await db.execute(q)).all():
        if s_id not in newest or f > newest[s_id][1]:
            newest[s_id] = (b_id, f)
    if not newest:
        return {}
    rows = (await db.execute(select(*RUN_COLS).where(
        BacktestRun.batch_id.in_({b for b, _ in newest.values()}),
        BacktestRun.strategy_id.in_(list(newest))))).all()
    out: dict[str, tuple[Any, dict[str, Any]]] = {}
    for s_id, (b_id, _) in newest.items():
        mine = [r for r in rows if r.strategy_id == s_id and r.batch_id == b_id]
        done = [r for r in mine if r.status == "DONE"]
        errored = [r for r in mine if r.status == "FAILED"]
        tiers: dict[str, int] = {}
        for r in done:
            tier = (r.summary or {}).get("tier", "unknown")
            tiers[tier] = tiers.get(tier, 0) + 1
        worst = max(done, key=_worse) if done else max(errored, key=lambda r: r.finished_at)
        out[s_id] = (worst, {"batch_id": str(b_id), "finished": len(done) + len(errored), "errored": len(errored),
                             "pending": sum(1 for r in mine if r.status in ("QUEUED", "RUNNING")), "tiers": tiers,
                             "basis": "worst finished run of the latest batch"})
    return out


async def _paper(db: AsyncSession, sid: str) -> dict[str, Any]:
    P = DemoPosition
    closed = (await db.execute(select(P).where(P.strategy_id == sid, P.status == "CLOSED").order_by(P.closed_at))).scalars().all()
    open_ = (await db.execute(select(P).where(P.strategy_id == sid, P.status == "OPEN"))).scalars().all()
    m = demo_metrics.summarize([Decimal(p.realized_pnl_quote or 0) for p in closed], [Decimal(p.fees_quote or 0) for p in closed])
    unreal = sum((Decimal(p.last_price or p.entry_price) * Decimal(p.qty) - Decimal(p.entry_cost_quote)) for p in open_)
    return {**m, "open_positions": len(open_), "unrealized_pnl_quote": str(unreal),
            "period": {"from": _j(closed[0].opened_at) if closed else None, "to": _j(closed[-1].closed_at) if closed else None},
            "label": "PAPER (simulated fills on live public data)"}


async def _exchange_demo(db: AsyncSession, sid: str) -> dict[str, Any]:
    rows = (await db.execute(select(ExecTrade).where(ExecTrade.strategy_id == sid, ExecTrade.environment != "LIVE")
                             .order_by(ExecTrade.created_at))).scalars().all()
    closed = [t for t in rows if t.status == "CLOSED"]
    m = demo_metrics.summarize([Decimal(t.realized_quote) for t in closed], [Decimal(t.fees_quote) for t in closed])
    return {**m, "open_positions": sum(1 for t in rows if t.status in EXEC_ACTIVE),
            "failed_entries": sum(1 for t in rows if t.status == "FAILED"),
            "label": "EXCHANGE DEMO (orders on an official demo / testnet account)"}


def _risk_view(cfg: StrategyConfig | None) -> dict[str, Any]:
    r = (cfg.risk if cfg else {}) or {}
    pct = Decimal(str(r.get("risk_pct", "0")))
    return {"mode": "PERCENT" if pct > 0 else "FIXED",
            "per_trade": f"{pct}% of equity" if pct > 0 else f"{r.get('risk_per_trade_quote', RISK_RULES['risk_per_trade_quote'][0])} USDT",
            "max_position_quote": r.get("max_position_quote"), "currency": "USDT (quote currency)"}


@router.get("/crypto/center/strategies")
async def center(db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    await config_store.ensure_defaults(db)
    await db.commit()
    cfgs = {c.strategy_id: c for c in (await db.execute(select(StrategyConfig))).scalars()}
    latest = await _latest_runs(db)
    out = []
    for spec in registry.all_specs():
        cfg = cfgs.get(spec.id)
        run, agg = latest.get(spec.id, (None, None))
        out.append({"id": spec.id, "name": spec.name, "category": spec.category, "description": spec.description,
                    "registry_status": spec.status, "library": spec.library, **lifecycle(spec, cfg, run, agg),
                    "enabled": bool(cfg and cfg.enabled), "mode": cfg.mode if cfg else "DISABLED",
                    "exchange": cfg.exchange if cfg else None, "symbols": list(cfg.symbols or []) if cfg else [],
                    "interval": cfg.interval if cfg else None, "risk": _risk_view(cfg),
                    "backtest": ({"run_id": str(run.id), "status": run.status, "finished_at": _j(run.finished_at),
                                  "symbol": run.symbol, "interval": run.interval, **(run.summary or {}),
                                  "aggregate": agg} if run else None)})
    return {"strategies": _j(out), "live_orders_unlocked": LIVE_ORDERS_UNLOCKED,
            "ranking_rule": "candidate > weak > insufficient evidence > failed, then out-of-sample expectancy (R), then "
                            "drawdown; win rate is never used to rank"}


@router.get("/crypto/center/rejected")
async def rejected(username: str = Depends(get_current_username)):
    return REJECTED


def _schema(spec: registry.StrategySpec) -> list[dict[str, Any]]:
    out = []
    for k, (default, kind, lo, hi) in spec.params.items():
        out.append({"key": k, "default": _j(default), "kind": kind, "min": _j(lo), "max": _j(hi),
                    "common": k in COMMON_PARAMS, "help": PARAM_HELP.get(k, "")})
    return out


@router.get("/crypto/center/strategies/{sid}")
async def strategy_detail(sid: str, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    spec = registry.get(sid)
    if spec is None:
        raise HTTPException(404, "no such strategy")
    await config_store.ensure_defaults(db)
    await db.commit()
    cfg = (await db.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == sid))).scalar_one_or_none()
    runs = (await db.execute(select(BacktestRun).where(BacktestRun.strategy_id == sid)
                             .order_by(BacktestRun.created_at.desc()).limit(20))).scalars().all()
    latest, agg = (await _latest_runs(db, sid)).get(sid, (None, None))
    sigs = (await db.execute(select(Signal).where(Signal.strategy_id == sid).order_by(Signal.id.desc()).limit(50))).scalars().all()
    events = (await db.execute(select(Event).where(Event.source == sid).order_by(Event.id.desc()).limit(50))).scalars().all()
    P = DemoPosition
    paper_open = (await db.execute(select(P).where(P.strategy_id == sid, P.status == "OPEN"))).scalars().all()
    exec_open = (await db.execute(select(ExecTrade).where(ExecTrade.strategy_id == sid,
                                                          ExecTrade.status.in_(EXEC_ACTIVE)))).scalars().all()
    conns = (await db.execute(select(ApiConnection).where(ApiConnection.environment.in_(("DEMO", "TESTNET")))))
    m = BY_ID.get(sid)
    full = (await db.get(BacktestRun, latest.id)) if latest else None
    return _j({
        "id": sid, "name": spec.name, "category": spec.category, "version": spec.version, "description": spec.description,
        "entry_rules": spec.entry_rules, "exit_rules": spec.exit_rules, "research": spec.research,
        "registry_status": spec.status, "status_reason": spec.status_reason, "notes": list(spec.notes),
        "provenance": spec.provenance, **lifecycle(spec, cfg, latest, agg),
        "exchanges": list(spec.exchanges), "intervals": list(spec.intervals), "max_symbols": spec.max_symbols,
        "params_schema": _schema(spec),
        "risk_schema": [{"key": k, "default": str(v[0]), "min": str(v[1]), "max": str(v[2])} for k, v in RISK_RULES.items()],
        "default_exits": validate_plan(spec.default_exits),
        "config": config_store.config_dict(cfg) if cfg else None, "risk_view": _risk_view(cfg),
        "practice_connections": [{"id": str(c.id), "exchange": c.exchange, "environment": c.environment,
                                  "label": c.label, "enabled": c.enabled} for c in conns.scalars()],
        "warmup_bars": m.warmup(m.p((cfg.params if cfg else {}) or {})) if m else None,
        "positions": {"paper": [{"id": str(p.id), "symbol": p.symbol, "qty": p.qty, "entry": p.entry_price,
                                 "stop": p.stop_price, "initial_stop": p.initial_stop, "target": p.target_price,
                                 "last": p.last_price, "opened_at": p.opened_at} for p in paper_open],
                      "exchange_demo": [{"id": str(t.id), "symbol": t.symbol, "status": t.status, "protection": t.protection,
                                         "qty": t.qty, "entry": t.entry_price, "stop": t.stop, "target": t.take_profit}
                                        for t in exec_open]},
        "signals": [{"at": s.candle_open_time, "symbol": s.symbol, "side": s.side, "action": s.action, "reasons": s.reasons,
                     "detail": s.detail} for s in sigs],
        "decisions": [{"at": e.at, "level": e.level, "message": e.message} for e in events],
        "performance": {
            "backtest": ({"run_id": str(full.id), "symbol": full.symbol, "interval": full.interval,
                          "summary": full.summary, "verdict": (full.result or {}).get("verdict"),
                          "out_of_sample": ((full.result or {}).get("default_params") or {}).get("metrics", {}).get("out_of_sample"),
                          "curve": (full.result or {}).get("oos_curve"), "data": full.data, "code_version": full.code_version,
                          "aggregate": agg, "param_source": (full.result or {}).get("param_source", "defaults"),
                          "label": "BACKTEST (simulation on historical data, out-of-sample period; the worst symbol "
                                   "of the latest batch)"}
                         if full and full.status == "DONE" else None),
            "paper": await _paper(db, sid), "exchange_demo": await _exchange_demo(db, sid),
            "live": {"status": "locked", "label": "LIVE: locked in code; no live results exist"},
        },
        "backtest_runs": [{"id": str(r.id), "batch_id": str(r.batch_id), "status": r.status, "symbol": r.symbol,
                           "interval": r.interval, "finished_at": r.finished_at, "summary": r.summary, "error": r.error}
                          for r in runs],
    })


# ---- backtests -----------------------------------------------------------

class BatchIn(BaseModel):
    strategies: list[str] | str = "all"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"], max_length=5)
    interval: str = "1h"
    exchange: str = "binance_us"
    start: datetime
    end: datetime
    costs: dict[str, float] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    optimize: bool = True
    param_source: Literal["defaults", "saved"] = "defaults"
    note: str | None = Field(None, max_length=300)


COST_KEYS = {"taker_fee_bps": (0, 100), "maker_fee_bps": (0, 100), "spread_bps": (0, 200), "slippage_bps": (0, 200),
             "max_participation": (0.001, 1), "funding_bps_per_day": (0, 100), "min_notional": (0, 1000)}


@router.post("/crypto/backtests")
async def create_backtest(body: BatchIn, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    lim = await portfolio.load(db)
    if body.optimize and not lim.get("optimization_enabled"):
        raise HTTPException(422, "parameter optimisation is switched off (Portfolio risk); run with optimize=false")
    for k, v in body.costs.items():
        if k not in COST_KEYS or not COST_KEYS[k][0] <= v <= COST_KEYS[k][1]:
            raise HTTPException(422, f"costs.{k}: unknown or out of range {COST_KEYS.get(k)}")
    mode = body.risk.get("mode", "PERCENT")
    if mode not in ("PERCENT", "FIXED") or set(body.risk) - {"mode", "value", "max_position_quote", "starting_equity"}:
        raise HTTPException(422, "risk: mode PERCENT | FIXED, value, max_position_quote, starting_equity")
    strategies = list(BY_ID) if body.strategies == "all" else list(body.strategies)
    start = body.start if body.start.tzinfo else body.start.replace(tzinfo=timezone.utc)
    end = body.end if body.end.tzinfo else body.end.replace(tzinfo=timezone.utc)
    try:
        b = await jobs.create_batch(db, strategies=strategies, symbols=body.symbols, interval=body.interval,
                                    exchange=body.exchange, start=start, end=end, costs=body.costs, risk=body.risk,
                                    optimize=body.optimize, actor=username, note=body.note,
                                    param_source=body.param_source)
    except jobs.BatchError as e:
        raise HTTPException(422, str(e)) from None
    db.add(ConfigAudit(actor=username, scope="backtest", before=None, after=_j(b.config)))
    await db.commit()
    return {"batch_id": str(b.id), "runs": b.total, "backtests_enabled": lim.get("backtests_enabled"),
            "worker_hint": "Queued. The server runs it with: docker compose ... run --rm crypto-trading python -m "
                           "yonixalpha_crypto.backtest.worker --max-runs 60 (needs backtests switched on)"}


async def _progress(db: AsyncSession, b: BacktestBatch) -> dict[str, Any]:
    counts = dict((await db.execute(select(BacktestRun.status, func.count()).where(BacktestRun.batch_id == b.id)
                                    .group_by(BacktestRun.status))).all())
    return {"id": str(b.id), "status": b.status, "total": b.total, "counts": counts, "cancelled": b.cancelled,
            "config": b.config, "created_at": _j(b.created_at), "created_by": b.created_by,
            "finished_at": _j(b.finished_at), "note": b.note}


@router.get("/crypto/backtests")
async def list_backtests(db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    rows = (await db.execute(select(BacktestBatch).order_by(BacktestBatch.created_at.desc()).limit(30))).scalars().all()
    lim = await portfolio.load(db)
    return {"batches": [await _progress(db, b) for b in rows], "backtests_enabled": lim.get("backtests_enabled"),
            "optimization_enabled": lim.get("optimization_enabled")}


@router.get("/crypto/backtests/{batch_id}")
async def batch_detail(batch_id: uuid.UUID, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    b = await db.get(BacktestBatch, batch_id)
    if b is None:
        raise HTTPException(404, "no such batch")
    runs = (await db.execute(select(BacktestRun).where(BacktestRun.batch_id == batch_id))).scalars().all()
    def key(r):
        rk = (r.summary or {}).get("rank_key")
        return (0, *rk) if rk else (1, 0, 0, 0)
    return {**(await _progress(db, b)),
            "runs": _j([{"id": str(r.id), "strategy_id": r.strategy_id, "symbol": r.symbol, "interval": r.interval,
                         "status": r.status, "phase": r.phase, "attempts": r.attempts, "summary": r.summary,
                         "error": r.error, "code_version": r.code_version, "finished_at": r.finished_at}
                        for r in sorted(runs, key=key)])}


@router.post("/crypto/backtests/{batch_id}/cancel")
async def cancel_backtest(batch_id: uuid.UUID, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    try:
        n = await jobs.cancel_batch(db, batch_id)
    except jobs.BatchError as e:
        raise HTTPException(404, str(e)) from None
    db.add(ConfigAudit(actor=username, scope="backtest", before={"batch": str(batch_id)}, after={"cancelled": True}))
    await db.commit()
    return {"cancelled_runs": n}


@router.get("/crypto/backtests/runs/{run_id}")
async def run_detail(run_id: uuid.UUID, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    r = await db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, "no such run")
    return _j({"id": str(r.id), "batch_id": str(r.batch_id), "strategy_id": r.strategy_id, "symbol": r.symbol,
               "exchange": r.exchange, "interval": r.interval, "status": r.status, "config": r.config,
               "code_version": r.code_version, "data": r.data, "summary": r.summary, "result": r.result, "error": r.error,
               "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at})


# ---- comparison ------------------------------------------------------------

def _daily(curve: dict | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for t, e in zip((curve or {}).get("t", []), (curve or {}).get("equity", [])):
        out[t[:10]] = e
    days = sorted(out)
    return {d: out[d] / out[p] - 1 for p, d in zip(days, days[1:]) if out[p]}


def _corr(a: dict[str, float], b: dict[str, float]) -> float | None:
    common = sorted(set(a) & set(b))
    if len(common) < 20:
        return None
    x, y = [a[d] for d in common], [b[d] for d in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    return round(sum((p - mx) * (q - my) for p, q in zip(x, y)) / (sx * sy), 3) if sx and sy else None


def _overlap(ta: list[dict], tb: list[dict]) -> float | None:
    """Share of A's time in the market during which B was also in the market."""
    def spans(ts):
        return [(t["entry_time"], t["exit_time"]) for t in ts if t.get("entry_time") and t.get("exit_time")]
    sa, sb = spans(ta), spans(tb)
    if not sa:
        return None
    tot = both = 0.0
    for a0, a1 in sa:
        d0, d1 = datetime.fromisoformat(a0), datetime.fromisoformat(a1)
        tot += (d1 - d0).total_seconds()
        for b0, b1 in sb:
            lo, hi = max(d0, datetime.fromisoformat(b0)), min(d1, datetime.fromisoformat(b1))
            if hi > lo:
                both += (hi - lo).total_seconds()
    return round(both / tot, 3) if tot else None


@router.get("/crypto/compare")
async def compare(runs: str = Query(..., max_length=2000), db: AsyncSession = Depends(get_db),
                  username: str = Depends(get_current_username)):
    await _ready(db)
    try:
        ids = [uuid.UUID(x) for x in runs.split(",") if x.strip()][:8]
    except ValueError:
        raise HTTPException(422, "runs: comma-separated run ids") from None
    rows = [r for r in [await db.get(BacktestRun, i) for i in ids] if r is not None and r.status == "DONE"]
    items = []
    for r in rows:
        res = r.result or {}
        items.append({"run_id": str(r.id), "strategy_id": r.strategy_id, "symbol": r.symbol, "interval": r.interval,
                      "batch_id": str(r.batch_id), "summary": r.summary, "costs": r.config.get("costs_effective"),
                      "oos": (res.get("default_params") or {}).get("metrics", {}).get("out_of_sample"),
                      "curve": res.get("oos_curve"), "period": (res.get("splits") or {}).get("out_of_sample")})
    daily = {x["run_id"]: _daily(x["curve"]) for x in items}
    trades = {r.id: (r.result or {}).get("oos_trades_sample") or [] for r in rows}
    corr = {a["run_id"]: {b["run_id"]: _corr(daily[a["run_id"]], daily[b["run_id"]]) for b in items} for a in items}
    overlap = {str(a.id): {str(b.id): (_overlap(trades[a.id], trades[b.id]) if a.symbol == b.symbol else 0.0)
                           for b in rows} for a in rows}
    same = len({(x["period"] or {}).get("from") for x in items}) <= 1 and len({str(x["costs"]) for x in items}) <= 1
    st = await portfolio.state(db, datetime.now(timezone.utc))
    return _j({"items": items, "correlation": corr, "overlap": overlap, "same_period_and_costs": same,
               "notes": ["correlation of daily out-of-sample returns; null with fewer than 20 common days",
                         "overlap: share of a strategy's time in the market during which the other was also in the "
                         "market on the same symbol (from the stored trade sample)",
                         "results from different periods or cost assumptions are not comparable" if not same else
                         "same out-of-sample period and costs"],
               "current_exposure": {k: st[k] for k in ("open_positions", "open_risk_quote", "total_exposure_quote",
                                                       "exposure_per_asset_quote")}})


# ---- portfolio, alerts, audit -----------------------------------------------

class PortfolioIn(BaseModel):
    model_config = {"extra": "allow"}
    expected_version: int = Field(..., ge=1)


@router.get("/crypto/portfolio")
async def get_portfolio(db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    lim = await portfolio.load(db)
    await db.commit()
    return _j({"limits": lim, "rules": {k: [str(v[0]), str(v[1]) if v[1] is not None else None,
                                            str(v[2]) if v[2] is not None else None] for k, v in portfolio.RULES.items()},
               "state": await portfolio.state(db, datetime.now(timezone.utc))})


@router.put("/crypto/portfolio")
async def put_portfolio(body: PortfolioIn, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    await _ready(db)
    change = {k: v for k, v in body.model_dump().items() if k != "expected_version"}
    try:
        out = await portfolio.save(db, change, body.expected_version, username)
    except portfolio.PortfolioError as e:
        raise HTTPException(409 if "changed elsewhere" in str(e) else 422, str(e)) from None
    await db.commit()
    return out


ALERT_KIND = {"risk": "risk", "execution": "execution", "connections": "connection", "backtest": "backtest",
              "protect": "data", "practice_lifecycle": "execution"}


@router.get("/crypto/alerts")
async def alerts(level: str | None = Query(None, pattern="^(INFO|WARN|ERROR)$"), kind: str | None = Query(None, max_length=24),
                 limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                 username: str = Depends(get_current_username)):
    await _ready(db)
    q = select(Event).order_by(Event.id.desc()).limit(limit)
    if level:
        q = q.where(Event.level == level)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for e in rows:
        k = ALERT_KIND.get(e.source, "strategy" if e.source in BY_ID or registry.get(e.source) else "engine")
        if kind and k != kind:
            continue
        out.append({"id": e.id, "at": e.at, "level": e.level, "kind": k, "source": e.source, "message": e.message,
                    "detail": e.detail})
    return _j(out)


@router.get("/crypto/audit")
async def audit(limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                username: str = Depends(get_current_username)):
    await _ready(db)
    rows = (await db.execute(select(ConfigAudit).order_by(ConfigAudit.id.desc()).limit(limit))).scalars().all()
    return _j([{"at": r.at, "actor": r.actor, "scope": r.scope, "before": r.before, "after": r.after} for r in rows])
