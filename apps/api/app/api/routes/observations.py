"""Fresh pump.fun observation: every token the stream saw, what the
observation window concluded, and — for tokens handed to the safety gate —
what the gate decided and why. Answers "why didn't the bot trade this
token?" with the actual reasons and the numbers behind them.

Read-only. Live observations come from Redis (tokens still inside their
window or under continued monitoring); final outcomes from the
token_observations table.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import jsonable
from yonixalpha_core.db.models import RiskAssessment, Token, TokenObservation, TradingCandidate
from yonixalpha_core.solana import pump_stream

router = APIRouter(prefix="/observations", tags=["observations"])

OUTCOMES = ["PROMOTE", "REJECT", "NO_TRADE", "MIGRATION_DETECTED"]
SUMMARY_METRICS = ("trades_total", "unique_buyers_total", "unique_sellers_total", "volume_total_sol", "buy_sell_volume_ratio",
                   "price_drawdown_from_peak_pct", "liquidity_state", "curve_real_sol", "curve_progress", "creator_sold")


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    m = report.get("metrics") or {}
    return {k: m.get(k) for k in SUMMARY_METRICS}


@router.get("/stats")
async def observation_stats(redis: Redis = Depends(get_redis), db: AsyncSession = Depends(get_db),
                            _: str = Depends(get_current_username)) -> dict:
    """Funnel counters (since the stream store started) plus outcome totals
    currently retained in the database."""
    funnel = await redis.hgetall(f"{pump_stream.PREFIX}:funnel")
    rows = (await db.execute(select(TokenObservation.outcome, func.count()).group_by(TokenObservation.outcome))).all()
    return {"funnel": funnel, "retained_outcomes": {o: n for o, n in rows},
            "live": await redis.zcard(pump_stream.OBS_LIVE)}


@router.get("/live")
async def live_observations(limit: int = Query(100, ge=1, le=500), redis: Redis = Depends(get_redis),
                            _: str = Depends(get_current_username)) -> list[dict]:
    """Tokens FRESH_OBSERVING or under CONTINUE_MONITORING, newest first."""
    mints = await redis.zrevrange(pump_stream.OBS_LIVE, 0, limit - 1)
    out = []
    for mint in mints:
        raw = await redis.get(pump_stream.obs_report_key(mint))
        if not raw:
            continue
        r = json.loads(raw)
        if r.get("outcome") not in ("OBSERVING", "CONTINUE_MONITORING"):
            continue
        meta = await pump_stream.load_meta(redis, mint) or {}
        out.append({"mint": mint, "symbol": meta.get("symbol"), "name": meta.get("name"), "state":
                    "FRESH_OBSERVING" if r["outcome"] == "OBSERVING" else "CONTINUE_MONITORING",
                    "age_seconds": r.get("age_seconds"), "trend": r.get("trend"), "reasons": r.get("reasons"),
                    "metrics": _summary(r), "evaluated_at": r.get("evaluated_at")})
    return out


@router.get("")
async def list_observations(outcome: str | None = Query(None), q: str | None = Query(None, max_length=64),
                            limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                            db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Final outcomes, newest first. `q` matches mint, symbol or name."""
    if outcome is not None and outcome not in OUTCOMES:
        raise HTTPException(422, f"outcome must be one of {OUTCOMES}")
    stmt = select(TokenObservation)
    if outcome:
        stmt = stmt.where(TokenObservation.outcome == outcome)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(TokenObservation.mint.ilike(like) | TokenObservation.symbol.ilike(like) | TokenObservation.name.ilike(like))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(TokenObservation.decided_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [{
        "mint": r.mint, "symbol": r.symbol, "name": r.name, "outcome": r.outcome, "trend": r.trend, "reasons": r.reasons,
        "decided_at": r.decided_at.isoformat(), "launched_at": r.launched_at.isoformat() if r.launched_at else None,
        "candidate_id": str(r.candidate_id) if r.candidate_id else None, "metrics": _summary(r.report),
    } for r in rows]}


@router.get("/{mint}")
async def explain_token(mint: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                        _: str = Depends(get_current_username)) -> dict:
    """Everything that decided this token: the observation window (full
    report: checkpoints, halves, positive / negative signals, curve state)
    and, if it reached the safety gate, each candidate's state history and
    latest assessment with every finding."""
    if len(mint) > 64:
        raise HTTPException(422, "mint too long")
    row = (await db.execute(select(TokenObservation).where(TokenObservation.mint == mint))).scalar_one_or_none()
    report = row.report if row else None
    if report is None:
        raw = await redis.get(pump_stream.obs_report_key(mint))
        report = json.loads(raw) if raw else None
    token = (await db.execute(select(Token).where(Token.mint_address == mint))).scalar_one_or_none()
    candidates = []
    if token is not None:
        for c in (await db.execute(select(TradingCandidate).where(TradingCandidate.token_id == token.id)
                                   .order_by(TradingCandidate.created_at))).scalars():
            latest = (await db.execute(select(RiskAssessment).where(RiskAssessment.candidate_id == c.id)
                                       .order_by(RiskAssessment.evaluated_at.desc()).limit(1))).scalar_one_or_none()
            candidates.append({
                "id": str(c.id), "engine": c.engine, "state": c.state, "state_history": c.state_history,
                "latest_assessment": None if latest is None else {
                    "id": str(latest.id), "decision": latest.decision, "status": latest.status_label,
                    "evaluated_at": latest.evaluated_at.isoformat(), "reasons": (latest.assessment or {}).get("reasons"),
                    "findings": (latest.assessment or {}).get("findings"),
                },
            })
    if report is None and not candidates:
        raise HTTPException(404, "no observation or candidate for this mint (observations are kept 3 days)")
    return jsonable({"mint": mint, "symbol": row.symbol if row else (token.symbol if token else None),
                     "observation": {"outcome": row.outcome if row else (report or {}).get("outcome"),
                                     "decided_at": row.decided_at.isoformat() if row else None, "report": report},
                     "candidates": candidates})
