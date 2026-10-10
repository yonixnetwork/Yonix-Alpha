"""Early-entry intelligence (SHADOW): live token states, strategy signals and
their outcomes, the chronological strategy comparison and readiness, entry
latency, late entries and paper vs LIVE parity per signal.

Read-only except PUT /settings (audited). No endpoint can place an order;
the strategies only record signals, or in PAPER mode create gate candidates
that can only become PAPER trades."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import audit, jsonable
from yonixalpha_core import entry_eval, entry_intel as ei, entry_parity, entry_store, entry_timing
from yonixalpha_core import strategy_registry as reg
from yonixalpha_core.db.models import EntrySignal, ModelVersion, PlatformSetting, TradingCandidate

router = APIRouter(prefix="/entry-intel", tags=["entry-intel"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _model(db: AsyncSession) -> dict | None:
    row = (await db.execute(select(ModelVersion.version, ModelVersion.status, ModelVersion.metrics, ModelVersion.trained_at)
                            .where(ModelVersion.name == "entry_timing").order_by(ModelVersion.version.desc()).limit(1))).first()
    if row is None:
        return None
    metrics = {k: v for k, v in (row.metrics or {}).items() if k != "coefficients"}
    return {"version": row.version, "status": row.status, "trained_at": row.trained_at, "metrics": metrics,
            "contribution": "none: probabilities are recorded next to signals, never used for a decision"}


@router.get("/overview")
async def overview(hours: int = Query(24, ge=1, le=720), db: AsyncSession = Depends(get_db),
                   redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    s = await entry_store.load_settings(db)
    since = _now() - timedelta(hours=hours)
    latency = await entry_store.measured_latency(db, redis)
    return jsonable({
        "mode": "SHADOW by default: strategies record signals; PAPER mode creates gate candidates that can only "
                "become PAPER trades; LIVE use is not available in this release",
        "enabled": s["enabled"], "config": s["config"].to_dict(), "modes": s["modes"],
        "strategies": list(ei.STRATEGIES), "baselines": list(ei.BASELINES), "migrated_variants": list(ei.MIGRATED_VARIANTS),
        "pass": await redis.hgetall("yx:ee:pass"), "counts": await entry_store.counts(db, since),
        "label_latency": {"seconds": latency[0], "source": latency[1]},
        "active": await entry_store.active_states(redis, 40), "model": await _model(db),
    })


@router.put("/settings")
async def put_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                       username: str = Depends(get_current_username)) -> dict:
    errors = entry_store.validate_update(body)
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = await db.get(PlatformSetting, entry_store.SETTINGS_KEY)
    before = dict(row.value) if row else {}
    cur = entry_store.parse_settings(before)
    value = {"enabled": bool(body.get("enabled", cur["enabled"])),
             "config": {**cur["config"].to_dict(), **(body.get("config") or {})},
             "modes": {**cur["modes"], **(body.get("modes") or {})}}
    value["config"] = ei.EntryConfig.from_dict(value["config"]).to_dict()
    if row is None:
        db.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value=value))
    else:
        row.value = value
    await audit(db, username, request, "entry_intel.settings_updated", {"before": before, "after": value})
    await db.commit()
    return value


@router.get("/signals")
async def signals(strategy: str | None = None, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                  _: str = Depends(get_current_username)) -> dict:
    q = select(EntrySignal).order_by(EntrySignal.decided_at.desc()).limit(limit)
    if strategy:
        if strategy not in ei.ALL_RECORDED:
            raise HTTPException(422, "unknown strategy")
        q = q.where(EntrySignal.strategy == strategy)
    rows = (await db.execute(q)).scalars().all()
    return jsonable({"signals": [entry_store.row_dict(r) for r in rows]})


@router.get("/evaluation")
async def evaluation(days: int = Query(30, ge=1, le=365), db: AsyncSession = Depends(get_db),
                     _: str = Depends(get_current_username)) -> dict:
    s = await entry_store.load_settings(db)
    rows = await entry_store.labelled_rows(db, _now() - timedelta(days=days))
    frozen_row = await db.get(PlatformSetting, entry_eval.FREEZE_SETTING)
    return jsonable({"labelled": len(rows), "states": list(entry_eval.STATES),
                     **entry_eval.comparison(rows, frozen_row.value if frozen_row else None, s["modes"],
                                             await entry_store.paper_results(db)),
                     "note": "chronological splits; the frozen test period never moves; a strategy is promoted only by "
                             "the operator and only after beating the existing pipeline (CURRENT_GATE_ENTRY) out of sample"})


@router.get("/strategies")
async def strategies(days: int = Query(14, ge=1, le=90), db: AsyncSession = Depends(get_db),
                     redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """The strategy registry with each strategy's mode, readiness and
    measured performance (executable returns after fees, impact, latency and
    fixed costs, from the shared labeller), its most frequent blocking
    reasons and the demotion check. Bounded: the labelled rows of the window
    (at most 20000), one indexed query of 60 rows per strategy in PAPER."""
    s = await entry_store.load_settings(db)
    now = _now()
    since = now - timedelta(days=days)
    rows = await entry_store.labelled_rows(db, since)
    frozen_row = await db.get(PlatformSetting, entry_eval.FREEZE_SETTING)
    comp = entry_eval.comparison(rows, frozen_row.value if frozen_row else None, s["modes"],
                                 await entry_store.paper_results(db))
    counts = await entry_store.counts(db, since)
    by: dict[str, list[float]] = {}
    for r in rows:
        v = (r.get("outcome") or {}).get("executable_return_pct")
        if v is not None:
            by.setdefault(r["strategy"], []).append(float(v))
    out = []
    for spec in reg.SPECS:
        name = spec.recorded_as
        c = comp["strategies"].get(name) or {}
        m = c.get("all") or {}
        out.append({**spec.to_dict(), "version_full": reg.version_of(name), "mode": s["modes"].get(name, "SHADOW"),
                    "readiness": c.get("readiness"), "signals": (counts.get(name) or {}).get("signals", 0),
                    "labelled": (counts.get(name) or {}).get("labelled", 0),
                    "with_return": m.get("with_executable_return", 0), "win_rate": m.get("win_rate"),
                    "expectancy_pct": m.get("mean_return_pct"), "median_return_pct": m.get("median_return_pct"),
                    "profit_factor": m.get("profit_factor"), "bad_entry_rate": m.get("bad_entry_rate"),
                    "late_entry_rate": m.get("late_entry_rate"), "max_drawdown_pct_points": m.get("max_drawdown_pct_points"),
                    "deterioration": reg.deterioration(by.get(name, [])),
                    "top_reasons": await reg.top_reasons(redis, name, now)})
    baselines = {}
    for name in (ei.CURRENT_GATE_ENTRY, ei.NAIVE_SAMPLE, ei.MIGRATED_NO_TRADE):
        m = (comp["strategies"].get(name) or {}).get("all") or {}
        baselines[name] = {k: m.get(k) for k in ("signals", "with_executable_return", "win_rate", "mean_return_pct",
                                                 "median_return_pct", "profit_factor", "bad_entry_rate")}
    passed = await redis.hgetall("yx:ee:pass")
    return jsonable({
        "days": days, "strategies": out, "baselines": baselines, "routes": await reg.route_stats(redis, now),
        "last_evaluation_at": passed.get("last_pass_at") if passed else None,
        "categories": {k: list(v) for k, v in ei.CATEGORY_STRATEGIES.items()},
        "rules": {"routing": "one strategy per token, from its category; none qualifies: NO_TRADE; a routed token is "
                             "never routed again (no re-entry after a loss)",
                  "promotion": "manual only (operator); LIVE use of these strategies is not available",
                  "demotion": reg.deterioration([])["rule"],
                  "units": "percent of a reference-size trade after fees, impact, latency and fixed costs"},
    })


@router.get("/latency")
async def latency(hours: float = Query(6, ge=0.25, le=168), entered_only: bool = False, db: AsyncSession = Depends(get_db),
                  redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    return jsonable(await entry_timing.latency_summary(db, redis, _now() - timedelta(hours=hours), limit=150,
                                                       entered_only=entered_only))


@router.get("/late-entries")
async def late(days: float = Query(2, ge=0.1, le=30), db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
               _: str = Depends(get_current_username)) -> dict:
    return jsonable(await entry_timing.late_entries(db, redis, _now() - timedelta(days=days)))


@router.get("/parity")
async def parity(days: float = Query(7, ge=0.1, le=60), db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                 _: str = Depends(get_current_username)) -> dict:
    lat, src = await entry_store.measured_latency(db, redis)
    return jsonable(await entry_parity.per_signal(db, redis, _now() - timedelta(days=days), lat, src))


@router.get("/tokens/{mint}")
async def token(mint: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                _: str = Depends(get_current_username)) -> dict:
    if not 32 <= len(mint) <= 44:
        raise HTTPException(422, "not a Solana mint")
    rows = (await db.execute(select(EntrySignal).where(EntrySignal.mint == mint).order_by(EntrySignal.decided_at))).scalars().all()
    cands = (await db.execute(select(TradingCandidate).where(TradingCandidate.detail["mint"].astext == mint)
                              .order_by(TradingCandidate.created_at.desc()).limit(3))).scalars().all()
    return jsonable({"mint": mint, "state": await entry_store.read_state(redis, mint),
                     "timeline_points": {k: entry_timing._iso(v) for k, v in (await entry_timing.redis_points(redis, mint)).items()},
                     "signals": [entry_store.row_dict(r) for r in rows],
                     "candidates": [await entry_timing.candidate_timeline(db, redis, c) for c in cands]})
