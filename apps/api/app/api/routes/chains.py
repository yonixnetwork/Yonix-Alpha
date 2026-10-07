"""Chains, launchpads (with evidence-based status) and operator trading
controls: per-chain / sniper / copy / new-entries switches, launchpad modes,
CLOSE POSITIONS and EMERGENCY EXIT. Every change is audited."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import audit, jsonable
from yonixalpha_core import events, kill_switch
from yonixalpha_core.chains import activity, controls, verification
from yonixalpha_core.chains.base import CHECKS, Chain
from yonixalpha_core.chains.registry import CHAINS, LAUNCHPADS
from yonixalpha_core.db.models import PaperPosition

router = APIRouter(tags=["chains"])


class ControlIn(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(default=None, pattern="^(OFF|PAPER|LIVE)$")
    note: str | None = Field(default=None, max_length=300)


class ConfirmIn(BaseModel):
    confirm: str = Field(..., description="must equal the action name, e.g. CLOSE POSITIONS")
    reason: str | None = Field(default=None, max_length=200)


async def _launchpads(db: AsyncSession, redis: Redis, chain: str | None) -> list[dict]:
    ctl = await controls.load(db)
    out = []
    for spec in LAUNCHPADS.values():
        if chain and spec.chain.value != chain:
            continue
        mode = controls.launchpad_mode(ctl, spec.key, spec.chain)
        st = await verification.status_for(db, redis, spec, mode)
        out.append({**spec.to_dict(), **st, **await activity.launchpad_activity(db, spec, mode, st)})
    return out


@router.get("/chains")
async def chains(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                 _: str = Depends(get_current_username)) -> dict:
    return jsonable(_chains_payload(await controls.load(db), await _launchpads(db, redis, None)))


def _chains_payload(ctl: dict, lps: list[dict]) -> dict:
    return {"chains": [{"chain": c.value, "name": s.name, "native_symbol": s.native_symbol,
                                 "account_model": s.account_model, "evm_chain_id": s.evm_chain_id, "explorer": s.explorer,
                                 "notes": s.notes, "enabled": ctl.get(f"chain:{c.value}", {}).get("enabled", True),
                                 "launchpads": [{"key": lp["key"], "name": lp["name"], "status": lp["status"],
                                                 "activity_status": lp["activity_status"], "listed": lp["listed"]}
                                                for lp in lps if lp["chain"] == c.value]}
                                for c, s in CHAINS.items()]}


@router.get("/launchpads")
async def launchpads(chain: str | None = Query(None, pattern="^(solana|bsc|robinhood)$"), db: AsyncSession = Depends(get_db),
                     redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    return jsonable({"launchpads": await _launchpads(db, redis, chain), "checks": list(CHECKS),
                     "statuses": ["LIVE", "PAPER_ONLY", "DEGRADED", "UNVERIFIED", "DISABLED"],
                     "activity_statuses": ["ACTIVE", "QUIET", "DEGRADED", "UNVERIFIED", "INACTIVE", "DISABLED"],
                     "note": "status is computed from recorded evidence on the real chain, never set by hand; "
                             "activity_status from launches / trades / migrations this system recorded "
                             "(7 days without any = INACTIVE, listed under archived / inactive adapters)"})


@router.get("/controls")
async def get_controls(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       _: str = Depends(get_current_username)) -> dict:
    ctl = await controls.load(db)
    return jsonable({
        "kill_switch": {"engaged": await kill_switch.is_engaged(redis), "reason": await kill_switch.get_reason(redis)},
        "switches": {k: {"enabled": ctl.get(k, {}).get("enabled", True), **{x: ctl.get(k, {}).get(x) for x in
                                                                              ("note", "updated_by", "updated_at")}}
                     for k in controls.SWITCHES},
        "launchpad_modes": {k: controls.launchpad_mode(ctl, k, s.chain) for k, s in LAUNCHPADS.items()},
        "modes": list(controls.MODES),
        "note": "switches block NEW entries only; exits and position management always continue"})


@router.put("/controls/{key:path}")
async def put_control(key: str, body: ControlIn, request: Request, db: AsyncSession = Depends(get_db),
                      redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    if key.startswith("launchpad:") and key.split(":", 1)[1] not in LAUNCHPADS:
        raise HTTPException(404, "unknown launchpad")
    if key.startswith("launchpad:") and body.mode == "LIVE":
        spec = LAUNCHPADS[key.split(":", 1)[1]]
        st = await verification.status_for(db, redis, spec, "LIVE")
        if st["status"] != "LIVE":
            raise HTTPException(409, f"{spec.name} cannot be set LIVE: {st['why']}")
    try:
        await controls.set_control(db, key, enabled=body.enabled, mode=body.mode, note=body.note, user=username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit(db, username, request, "trading_control.update", {"key": key, **body.model_dump(exclude_none=True)})
    await db.commit()
    await events.publish(redis, "controls.updated", {"key": key}, "api")
    return await get_controls(db, redis, username)


async def _close_all(db: AsyncSession, username: str, why: str) -> int:
    now = datetime.now(timezone.utc)
    from yonixalpha_core.safety import store

    rows = (await db.execute(select(PaperPosition).where(
        PaperPosition.status == "open", PaperPosition.exit_requested.is_(False),
        or_(PaperPosition.engine.is_(None), PaperPosition.engine.not_in(store.LEGACY_ENGINES))))).scalars().all()
    for p in rows:
        p.exit_requested = True
        await store.add_timeline_event(db, "operator_exit", now, {"by": username, "note": why},
                                       candidate_id=p.candidate_id, assessment_id=p.assessment_id, position_id=p.id)
    return len(rows)


@router.post("/controls/close-positions")
async def close_positions(body: ConfirmIn, request: Request, db: AsyncSession = Depends(get_db),
                          redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Requests an exit for every open position (paper and LIVE): the
    position loop sells each one on its next pass; a failed fill is retried."""
    if body.confirm != "CLOSE POSITIONS":
        raise HTTPException(422, "confirm must be 'CLOSE POSITIONS'")
    n = await _close_all(db, username, "CLOSE POSITIONS")
    await audit(db, username, request, "trading_control.close_positions", {"positions": n, "reason": body.reason})
    await db.commit()
    await events.publish(redis, "position.updated", {"action": "close_all", "positions": n}, "api")
    return {"exit_requested": n, "note": "positions are sold by the position loop on its next pass"}


@router.post("/controls/emergency-exit")
async def emergency_exit(body: ConfirmIn, request: Request, db: AsyncSession = Depends(get_db),
                         redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Engages the global kill switch (no new entries anywhere; pending
    LIVE buys are cancelled by the live worker) and requests an exit for
    every open position."""
    if body.confirm != "EMERGENCY EXIT":
        raise HTTPException(422, "confirm must be 'EMERGENCY EXIT'")
    await kill_switch.engage(redis, f"EMERGENCY EXIT by {username}: {body.reason or 'no reason given'}")
    n = await _close_all(db, username, "EMERGENCY EXIT")
    await audit(db, username, request, "trading_control.emergency_exit", {"positions": n, "reason": body.reason})
    await db.commit()
    await events.publish(redis, "kill_switch.updated", {"engaged": True}, "api")
    return {"kill_switch": "ENGAGED", "exit_requested": n}


@router.get("/chains/{chain}")
async def chain_detail(chain: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       username: str = Depends(get_current_username)) -> dict:
    try:
        c = Chain(chain)
    except ValueError as exc:
        raise HTTPException(404, "unknown chain") from exc
    # only this chain's launchpads, built once (they were built for every
    # chain, then again for this one, on each refresh)
    lps = await _launchpads(db, redis, c.value)
    summary = next(x for x in _chains_payload(await controls.load(db), lps)["chains"] if x["chain"] == c.value)
    return jsonable({**summary, "launchpads": lps})
