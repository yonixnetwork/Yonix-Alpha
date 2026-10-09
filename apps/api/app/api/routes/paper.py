from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.paper import PaperPositionOut
from yonixalpha_core import execution_analysis, events, paper_execution, position_pnl, system_profile
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import (
    ExecutionOrder, PaperAccount, PaperOrder, PaperPosition, PlatformSetting, RiskAssessment, TradeTimelineEvent,
)
from yonixalpha_core.safety import store

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/positions", response_model=Page[PaperPositionOut])
async def list_positions(
    status_filter: str | None = Query(None, alias="status"),
    engine: str | None = None,
    account: str | None = None,
    strategy: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_username),
) -> Page[PaperPositionOut]:
    filters = []
    hidden = system_profile.hidden_engines(settings)
    if engine is None and hidden:  # positions of chains the profile switches off (frozen, not managed)
        filters.append(or_(PaperPosition.engine.is_(None), PaperPosition.engine.not_in(hidden)))
    if status_filter is not None:
        filters.append(PaperPosition.status == status_filter)
    if engine is not None:
        filters.append(PaperPosition.engine == engine)
    if account is not None:
        filters.append(PaperPosition.account_id == select(PaperAccount.id).where(PaperAccount.name == account).scalar_subquery())
    if strategy is not None:
        filters.append(PaperPosition.assessment_id.in_(select(RiskAssessment.id).where(RiskAssessment.strategy == strategy)))

    total = (await db.execute(select(func.count()).select_from(PaperPosition).where(*filters))).scalar_one()
    result = await db.execute(select(PaperPosition).where(*filters).order_by(PaperPosition.created_at.desc()).limit(limit).offset(offset))
    positions = result.scalars().all()
    now = datetime.now(timezone.utc)
    return Page(items=[PaperPositionOut.model_validate(p).model_copy(update={"pnl": position_pnl.view(p, now)})
                       for p in positions], total=total, limit=limit, offset=offset)


def _diagnostics(o: ExecutionOrder) -> dict:
    """Stored execution diagnostics; for orders placed before they existed,
    the timing is computed from the recorded stages (nothing estimated)."""
    diag = dict(o.diagnostics or {})
    if "timing" not in diag and o.result:
        diag["timing"] = execution_analysis.timing(o.created_at, o.result, diag.get("decision"))
        diag["timing_reconstructed"] = True
    return diag


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
    orders = (await db.execute(select(ExecutionOrder).where(ExecutionOrder.position_id == p.id)
                               .order_by(ExecutionOrder.created_at))).scalars().all()
    return jsonable({
        "position": {**PaperPositionOut.model_validate(p).model_dump(), "pnl": position_pnl.view(p, datetime.now(timezone.utc))},
        "account": {"name": acct.name, "currency": acct.quote_currency} if acct else None,
        "strategy": a.strategy if a else p.engine,
        "assessment": {"id": a.id, "decision": a.decision, "status_label": a.status_label, "overall_risk": a.overall_risk,
                       "reasons": (a.assessment or {}).get("reasons"), "plan": (a.assessment or {}).get("plan"),
                       "versions": (a.assessment or {}).get("versions"),
                       "ml": ((a.assessment or {}).get("inputs_snapshot") or {}).get("ml"),
                       "evaluated_at": a.evaluated_at} if a else None,
        "timeline": [{"type": t.event_type, "at": t.occurred_at, "detail": t.detail} for t in timeline],
        "orders": [{"id": o.id, "side": o.side, "reason": o.reason, "status": o.status, "route": o.route, "provider": o.provider,
                    "amount": o.amount, "amount_kind": o.amount_kind, "slippage_pct": o.slippage_pct, "signature": o.signature,
                    "error": o.error, "fill": (o.result or {}).get("fill"), "created_at": o.created_at,
                    "confirmed_at": o.confirmed_at, "stage": (o.result or {}).get("stage"),
                    "diagnostics": _diagnostics(o)} for o in orders],
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


@router.get("/execution-settings")
async def get_execution_settings(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Simulated entry/exit failure rates for paper trades: the operator's
    setting, the rate measured from live orders, and which one applies."""
    return jsonable(await paper_execution.effective_rates(db))


@router.put("/execution-settings")
async def put_execution_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                                 username: str = Depends(get_current_username)) -> dict:
    current = (await paper_execution.load_settings(db)).to_dict()
    s, errors = paper_execution.parse_settings({**current, **body})
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = await db.get(PlatformSetting, paper_execution.SETTINGS_KEY)
    if row is None:
        db.add(PlatformSetting(key=paper_execution.SETTINGS_KEY, value=s.to_dict()))
    else:
        row.value = s.to_dict()
    await audit(db, username, request, "paper_execution.updated", {"before": current, "after": s.to_dict()})
    await db.commit()
    return jsonable(await paper_execution.effective_rates(db))
