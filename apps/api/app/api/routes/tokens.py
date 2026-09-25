"""Token details: everything the platform knows about one Solana mint — the
registry row, live stream state (curve, recent trades), candidates across
engines, every safety-gate decision, and paper positions."""

from fastapi import APIRouter, Depends, HTTPException, Path
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import jsonable
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, Token, TokenEvent, TradingCandidate
from yonixalpha_core.solana import pump_stream

router = APIRouter(prefix="/tokens", tags=["tokens"])

MINT = Path(..., pattern=r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@router.get("/{mint}")
async def token_details(mint: str = MINT, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                        _: str = Depends(get_current_username)) -> dict:
    token = (await db.execute(select(Token).where(Token.mint_address == mint))).scalar_one_or_none()
    meta = await pump_stream.load_meta(redis, mint)
    curve = await pump_stream.load_curve(redis, mint)
    assessments = (await db.execute(select(RiskAssessment).where(RiskAssessment.asset_id == mint)
                                    .order_by(RiskAssessment.evaluated_at.desc()).limit(50))).scalars().all()
    if token is None and meta is None and not assessments:
        raise HTTPException(404, "mint not seen by this platform")
    candidates, events_ = [], []
    if token is not None:
        candidates = (await db.execute(select(TradingCandidate).where(TradingCandidate.token_id == token.id)
                                       .order_by(TradingCandidate.created_at.desc()))).scalars().all()
        events_ = (await db.execute(select(TokenEvent).where(TokenEvent.token_id == token.id)
                                    .order_by(TokenEvent.occurred_at.desc()).limit(50))).scalars().all()
    positions = (await db.execute(select(PaperPosition).where(PaperPosition.asset_id == mint)
                                  .order_by(PaperPosition.created_at.desc()))).scalars().all()
    trades = await pump_stream.load_trades(redis, mint)
    latest = assessments[0].assessment if assessments else {}
    return jsonable({
        "mint": mint,
        "token": {"symbol": token.symbol, "name": token.name, "creator": token.creator_address, "first_seen_at": token.first_seen_at,
                  "first_seen_source": token.first_seen_source, "metadata_uri": token.metadata_uri} if token else None,
        "stream": {"meta": meta, "curve": curve.__dict__ if curve else None,
                   "recent_trades": [t.__dict__ for t in trades[-50:]]},
        "latest_evidence": (latest or {}).get("inputs_snapshot"),
        "candidates": [{"id": c.id, "engine": c.engine, "state": c.state, "state_history": c.state_history[-20:],
                        "created_at": c.created_at} for c in candidates],
        "assessments": [{"id": a.id, "engine": a.engine, "decision": a.decision, "status_label": a.status_label,
                         "overall_risk": a.overall_risk, "reasons": (a.assessment or {}).get("reasons"),
                         "approval_state": a.approval_state, "evaluated_at": a.evaluated_at} for a in assessments],
        "positions": [{"id": p.id, "engine": p.engine, "side": p.side, "status": p.status, "entry_price": p.entry_price,
                       "exit_price": p.exit_price, "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason,
                       "entry_at": p.entry_at, "exit_at": p.exit_at} for p in positions],
        "events": [{"type": e.event_type, "source": e.source, "at": e.occurred_at, "trader": e.trader_address,
                    "sol": e.sol_amount, "is_buy": e.is_buy} for e in events_],
    })
