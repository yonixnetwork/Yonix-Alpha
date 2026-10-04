"""Research pipeline (master §67): RESEARCH -> REVIEW -> PAPER -> VALIDATION
-> CONTROLLED_RELEASE, moved by an operator, audited. A record only: no
move here changes a trading rule (yonixalpha_core.research)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.api.util import audit, jsonable
from yonixalpha_core import research
from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.db.models import ResearchItem, UpdateEvent

router = APIRouter(prefix="/research", tags=["research"])
SUGGEST_UPDATE_DAYS = 30
SUGGEST_CLASSES = ("ACTION_REQUIRED", "SECURITY_UPDATE", "BREAKING_CHANGE", "PROVIDER_CHANGE", "UPGRADE_AVAILABLE")


class ItemIn(BaseModel):
    kind: str = Field(min_length=3, max_length=32)
    title: str = Field(min_length=3, max_length=200)
    summary: str | None = Field(None, max_length=1000)
    source: str = Field("manual", max_length=32)
    ref: str | None = Field(None, max_length=128)


class MoveIn(BaseModel):
    stage: str = Field(min_length=3, max_length=24)
    note: str | None = Field(None, max_length=1000)


def _out(i: ResearchItem) -> dict:
    return {"id": i.id, "kind": i.kind, "title": i.title, "source": i.source, "ref": i.ref, "stage": i.stage,
            "summary": i.summary, "history": i.history, "created_at": i.created_at, "updated_at": i.updated_at,
            "next": (research.STAGES[research.STAGES.index(i.stage) + 1]
                     if i.stage in research.STAGES[:-1] else None)}


async def _suggestions(db: AsyncSession, now: datetime) -> list[dict]:
    """Venues observed but not traded, and recent update-monitor findings,
    that have no research item yet."""
    have = {(s, r) for s, r in (await db.execute(select(ResearchItem.source, ResearchItem.ref))).all()}
    out = []
    for key, spec in sorted(LAUNCHPADS.items()):
        if not spec.supports_trading and spec.active and ("launchpad", key) not in have:
            out.append({"source": "launchpad", "ref": key, "kind": "launchpad",
                        "title": f"{spec.name} ({spec.chain.value}): observed, not traded"})
    ups = (await db.execute(select(UpdateEvent).where(
        UpdateEvent.detected_at >= now - timedelta(days=SUGGEST_UPDATE_DAYS),
        UpdateEvent.classification.in_(SUGGEST_CLASSES)).order_by(UpdateEvent.detected_at.desc()).limit(50))).scalars()
    for u in ups:
        if ("update_event", str(u.id)) not in have:
            out.append({"source": "update_event", "ref": str(u.id), "kind": "github_implementation",
                        "title": f"{u.key}: {u.classification} {u.from_ref or ''} -> {u.to_ref or ''}".strip()[:200]})
    return out


@router.get("")
async def list_items(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    items = (await db.execute(select(ResearchItem).order_by(ResearchItem.updated_at.desc()))).scalars().all()
    return jsonable({
        "items": [_out(i) for i in items], "stages": list(research.STAGES), "rejected": research.REJECTED,
        "kinds": list(research.KINDS), "requirements": research.REQUIREMENTS,
        "suggestions": await _suggestions(db, now),
        "note": "A research result never changes a trading rule by itself. CONTROLLED RELEASE records the operator's "
                "decision and its limits; the release is made with the existing controls (launchpad status, strategy "
                "and copy switches, risk settings), each audited on its own.",
    })


@router.post("")
async def create_item(body: ItemIn, request: Request, db: AsyncSession = Depends(get_db),
                      username: str = Depends(get_current_username)) -> dict:
    if body.kind not in research.KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(research.KINDS)}")
    if body.source not in ("manual", "launchpad", "update_event"):
        raise HTTPException(422, "source must be manual, launchpad or update_event")
    if body.source == "launchpad" and body.ref not in LAUNCHPADS:
        raise HTTPException(422, "unknown launchpad")
    if body.source == "update_event":
        try:
            import uuid

            found = await db.get(UpdateEvent, uuid.UUID(str(body.ref)))
        except ValueError:
            found = None
        if found is None:
            raise HTTPException(422, "unknown update event")
    if body.source != "manual" and (await db.execute(select(ResearchItem.id).where(
            ResearchItem.source == body.source, ResearchItem.ref == body.ref))).first():
        raise HTTPException(409, "this source already has a research item")
    now = datetime.now(timezone.utc)
    item = ResearchItem(kind=body.kind, title=body.title, summary=body.summary, source=body.source,
                        ref=body.ref if body.source != "manual" else None, stage="RESEARCH",
                        history=[research.entry("RESEARCH", username, now, body.summary)], created_at=now, updated_at=now)
    db.add(item)
    await db.flush()
    await audit(db, username, request, "research.created", {"id": item.id, "title": item.title, "kind": item.kind})
    await db.commit()
    return jsonable(_out(item))


@router.post("/{item_id}/move")
async def move_item(item_id: int, body: MoveIn, request: Request, db: AsyncSession = Depends(get_db),
                    username: str = Depends(get_current_username)) -> dict:
    item = await db.get(ResearchItem, item_id)
    if item is None:
        raise HTTPException(404, "research item not found")
    errors = research.check_move(item.stage, body.stage, body.note)
    if errors:
        raise HTTPException(409, {"errors": errors})
    now = datetime.now(timezone.utc)
    before = item.stage
    item.stage = body.stage
    item.history = [*(item.history or []), research.entry(body.stage, username, now, body.note)]
    item.updated_at = now
    await audit(db, username, request, "research.moved", {"id": item.id, "from": before, "to": body.stage,
                                                          "note": body.note})
    await db.commit()
    return jsonable(_out(item))
