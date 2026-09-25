from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import audit, jsonable
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.paper import PaperPositionOut
from yonixalpha_core import events
from yonixalpha_core.db.models import PaperAccount, PaperOrder, PaperPosition, RiskAssessment, TradeTimelineEvent
from yonixalpha_core.safety import store

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/positions", response_model=Page[PaperPositionOut])
async def list_positions(
    status_filter: str | None = Query(None, alias="status"),
    engine: str | None = None,
    account: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[PaperPositionOut]:
    filters = []
    if status_filter is not None:
        filters.append(PaperPosition.status == status_filter)
    if engine is not None:
        filters.append(PaperPosition.engine == engine)
    if account is not None:
        filters.append(PaperPosition.account_id == select(PaperAccount.id).where(PaperAccount.name == account).scalar_subquery())

    total = (await db.execute(select(func.count()).select_from(PaperPosition).where(*filters))).scalar_one()
    result = await db.execute(select(PaperPosition).where(*filters).order_by(PaperPosition.created_at.desc()).limit(limit).offset(offset))
    positions = result.scalars().all()
    return Page(items=[PaperPositionOut.model_validate(p) for p in positions], total=total, limit=limit, offset=offset)


@router.get("/positions/{position_id}")
async def get_position(position_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Trade details: the position, the decision that opened it, every
    timeline event (entry, TP hits, trailing moves, exit intelligence,
    operator actions, close) and its outcome labels."""
    p = await db.get(PaperPosition, position_id)
    if p is None:
        raise HTTPException(404, "position not found")
    a = await db.get(RiskAssessment, p.assessment_id) if p.assessment_id else None
    acct = await db.get(PaperAccount, p.account_id) if p.account_id else None
    q = select(TradeTimelineEvent).where(TradeTimelineEvent.position_id == p.id)
    if p.assessment_id:
        q = select(TradeTimelineEvent).where((TradeTimelineEvent.position_id == p.id) |
                                             (TradeTimelineEvent.assessment_id == p.assessment_id))
    timeline = (await db.execute(q.order_by(TradeTimelineEvent.occurred_at).limit(500))).scalars().all()
    return jsonable({
        "position": PaperPositionOut.model_validate(p).model_dump(),
        "account": {"name": acct.name, "currency": acct.quote_currency} if acct else None,
        "strategy": a.strategy if a else p.engine,
        "assessment": {"id": a.id, "decision": a.decision, "status_label": a.status_label, "overall_risk": a.overall_risk,
                       "reasons": (a.assessment or {}).get("reasons"), "plan": (a.assessment or {}).get("plan"),
                       "versions": (a.assessment or {}).get("versions"),
                       "ml": ((a.assessment or {}).get("inputs_snapshot") or {}).get("ml"),
                       "evaluated_at": a.evaluated_at} if a else None,
        "timeline": [{"type": t.event_type, "at": t.occurred_at, "detail": t.detail} for t in timeline],
    })


async def _control(position_id: UUID, action: str, request: Request, db: AsyncSession, redis: Redis, username: str) -> dict:
    p = await db.get(PaperPosition, position_id)
    if p is None:
        raise HTTPException(404, "position not found")
    if p.status != "open":
        raise HTTPException(409, f"position is {p.status}")
    now = datetime.now(timezone.utc)
    if action == "exit":
        if p.exit_requested:
            raise HTTPException(409, "exit already requested")
        p.exit_requested = True
        note = "paper-trading closes it against the live book/curve on its next tick; a failed fill is retried"
    elif action == "pause":
        p.management_paused = True
        note = "take-profits, trailing and exit intelligence paused; the stop loss is still enforced"
    else:
        p.management_paused = False
        note = "management resumed"
    await store.add_timeline_event(db, f"operator_{action}", now, {"by": username, "note": note},
                                   candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    await audit(db, username, request, f"paper_position.{action}", {"position_id": str(p.id), "symbol": p.symbol})
    await db.commit()
    await events.publish(redis, "position.updated", {"position_id": str(p.id), "action": action,
                                                     "exit_requested": p.exit_requested, "paused": p.management_paused}, "api")
    return {"position": PaperPositionOut.model_validate(p).model_dump(mode="json"), "note": note}


@router.post("/positions/{position_id}/exit")
async def exit_position(position_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                        redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    return await _control(position_id, "exit", request, db, redis, username)


@router.post("/positions/{position_id}/pause")
async def pause_position(position_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                         redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    return await _control(position_id, "pause", request, db, redis, username)


@router.post("/positions/{position_id}/resume")
async def resume_position(position_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                          redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    return await _control(position_id, "resume", request, db, redis, username)


@router.get("/orders")
async def list_orders(strategy: str | None = None, limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT),
                      db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> list[dict]:
    q = select(PaperOrder).order_by(PaperOrder.created_at.desc()).limit(limit)
    if strategy:
        q = q.where(PaperOrder.strategy == strategy)
    rows = (await db.execute(q)).scalars().all()
    return jsonable([{"id": o.id, "strategy": o.strategy, "venue": o.venue, "symbol": o.symbol, "side": o.side,
                      "type": o.order_type, "price": o.price, "quantity": o.quantity, "fill_price": o.fill_price, "fee": o.fee,
                      "status": o.status, "filled_at": o.filled_at, "detail": o.detail} for o in rows])
