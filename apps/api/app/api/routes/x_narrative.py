"""X narrative intelligence (SHADOW): status, settings, per-token observations.

The bearer token is never returned. NOT_CONFIGURED without a token, DISABLED
while a switch is off; no endpoint calls X itself (lookups run in the
decision-engine background pass)."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import x_narrative as xn
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import PlatformSetting, XNarrativeObservation

router = APIRouter(prefix="/x-narrative", tags=["x-narrative"])


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                 settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    cfg = await xn.load_settings(db)
    now = datetime.now(timezone.utc)
    last = await redis.get(xn.STATUS)
    return jsonable({
        "provider_status": xn.provider_status(settings, cfg),
        "env_enabled": bool(settings.X_NARRATIVE_ENABLED), "token_configured": xn.bearer(settings) is not None,
        "settings": cfg.to_dict(), "spent_today_usd": str(await xn.spent_today(redis, now)),
        "cost_per_post_usd": str(xn.COST_PER_POST_USD), "queued": await redis.zcard(xn.QUEUE),
        "cooldown": await redis.get(xn.COOLDOWN), "last_pass": json.loads(last) if last else None,
        "mode": "SHADOW: recorded next to decisions; never read by the gate, never buys or blocks a trade"})


@router.put("/settings")
async def put_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                       username: str = Depends(get_current_username)) -> dict:
    current = (await xn.load_settings(db)).to_dict()
    s, errors = xn.parse_settings({**current, **body})
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = await db.get(PlatformSetting, xn.SETTINGS_KEY)
    if row is None:
        db.add(PlatformSetting(key=xn.SETTINGS_KEY, value=s.to_dict()))
    else:
        row.value = s.to_dict()
    await audit(db, username, request, "x_narrative.settings_updated", {"before": current, "after": s.to_dict()})
    await db.commit()
    return s.to_dict()


@router.get("/tokens/{mint}")
async def token(mint: str, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                _: str = Depends(get_current_username)) -> dict:
    """Newest observations for one mint, or NO X DATA."""
    if not 32 <= len(mint) <= 44:
        raise HTTPException(422, "not a Solana mint")
    rows = (await db.execute(select(XNarrativeObservation).where(XNarrativeObservation.mint == mint)
                             .order_by(XNarrativeObservation.observed_at.desc()).limit(5))).scalars().all()
    cfg = await xn.load_settings(db)
    obs = [{"observed_at": r.observed_at, "status": r.status, "identity_confidence": r.identity_confidence,
            "narrative_score": r.narrative_score, "social_data_quality": r.social_data_quality,
            "onchain_score": r.onchain_score, "combined_score": r.combined_score, "posts_returned": r.posts_returned,
            "features": r.features, "evidence": r.evidence, "query": r.query} for r in rows]
    return jsonable({"mint": mint, "provider_status": xn.provider_status(settings, cfg),
                     "state": "NO X DATA" if not obs else obs[0]["status"], "observations": obs,
                     "note": "SHADOW: never changes a decision. Identity needs evidence (exact mint, or ticker AND name); "
                             "a ticker alone or a celebrity name is not an identification or an endorsement."})
