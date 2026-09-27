"""Manual BUY: the operator asks to buy one token from the dashboard.

Not a second trading path. The API records the request and queues it; the
decision engine runs the same safety gate and execution code as automatic
trading (services/decision-engine/app/gate_eval.py) with one difference:
the strategy's entry signal is replaced by the operator's decision
(AssessmentInput.operator_request), and the confirmation counts as the
approval step. Sellability, catastrophic token risk, liquidity, route,
stale data, wallet, sizing and global risk limits all still apply, and a
blocked request reports the exact reasons.

The route follows the token's real state: a Pump.fun bonding curve before
migration, the PumpSwap pool after. Execution happens in the current global
mode: LIVE (with every environment lock open and the order worker ready)
or PAPER.

Request status lives in Redis (yx:manual:req:<id>); for a LIVE buy it is
derived from the real order and position rows once they exist, never
stored as "confirmed" by this module.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events
from yonixalpha_core.db.models import Token, TradingCandidate
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.state_machine import CandidateState

QUEUE = "yx:manual:queue"
PREFIX = "yx:manual:req:"
RECENT = "yx:manual:recent"
TTL_SECONDS = 24 * 3600
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
CANDIDATE_ENGINE = {"solana_fresh": "discovery", "solana_momentum": "momentum", "solana_migration": "migration"}
ROUTE_LABEL = {"solana_fresh": "Pump.fun bonding curve", "solana_momentum": "Pump.fun bonding curve",
               "solana_migration": "PumpSwap pool (migrated)"}
OPEN_STATES = [CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value, CandidateState.ANALYZING.value,
               CandidateState.WAITING_FOR_LIQUIDITY.value, CandidateState.WAITING_FOR_APPROVAL.value]

# QUEUED → EVALUATING → (BLOCKED | PAPER_POSITION_OPEN | SUBMITTING → CONFIRMING → CONFIRMED/POSITION_OPEN | FAILED)
FINAL = {"BLOCKED", "PAPER_POSITION_OPEN", "POSITION_OPEN", "FAILED", "EXPIRED"}


class ManualTradeError(ValueError):
    pass


def resolve_engine(pool: str | None, requested: str | None) -> str:
    """Migrated tokens (a PumpSwap pool is known) always use the migration
    engine and pool route; otherwise the requested curve engine."""
    if pool:
        return "solana_migration"
    return requested if requested in ("solana_fresh", "solana_momentum") else "solana_fresh"


async def get(redis: Redis, request_id: str) -> dict[str, Any] | None:
    raw = await redis.get(PREFIX + request_id)
    return json.loads(raw) if raw else None


async def update(redis: Redis, request_id: str, status: str, **fields: Any) -> dict[str, Any] | None:
    req = await get(redis, request_id)
    if req is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    req.update({k: v for k, v in fields.items()})
    if req.get("status") != status:
        req["history"] = [*req.get("history", []), {"status": status, "at": now, **({"reason": fields["reason"]} if fields.get("reason") else {})}]
    req["status"], req["updated_at"] = status, now
    await redis.set(PREFIX + request_id, json.dumps(req, default=str), ex=TTL_SECONDS)
    await events.publish(redis, "manual_trade.updated", {"id": request_id, "status": status, "mint": req.get("mint")}, "manual")
    return req


async def _token(session: AsyncSession, mint: str, meta: dict | None, now: datetime) -> Token:
    meta = meta or {}
    await session.execute(insert(Token).values(
        mint_address=mint, creator_address=meta.get("creator") or None,
        symbol=(meta.get("symbol") or None) and meta["symbol"][:32], name=(meta.get("name") or None) and meta["name"][:128],
        first_seen_source="manual", last_event_at=now).on_conflict_do_nothing(index_elements=["mint_address"]))
    return (await session.execute(select(Token).where(Token.mint_address == mint))).scalar_one()


async def create_request(session: AsyncSession, redis: Redis, mint: str, requested_engine: str | None, source: str,
                         user: str) -> dict[str, Any]:
    """Validates, resolves the route, attaches a candidate and queues the
    request for the decision engine. Nothing is bought here."""
    if not BASE58.match(mint or ""):
        raise ManualTradeError("not a valid Solana mint address")
    now = datetime.now(timezone.utc)
    curve = await pump_stream.load_curve(redis, mint)
    meta = await pump_stream.load_meta(redis, mint)
    if curve is None and meta is None:
        raise ManualTradeError("this token is not in the Pump.fun stream data (only Pump.fun tokens seen by the "
                               "discovery stream can be bought manually)")
    engine = resolve_engine(curve.pool if curve else None, requested_engine)
    token = await _token(session, mint, meta, now)
    cand_engine = CANDIDATE_ENGINE[engine]
    candidate = (await session.execute(select(TradingCandidate).where(
        TradingCandidate.token_id == token.id, TradingCandidate.engine == cand_engine,
        TradingCandidate.state.in_(OPEN_STATES)).limit(1))).scalar_one_or_none()
    request_id = uuid.uuid4().hex
    if candidate is None:
        candidate = TradingCandidate(
            token_id=token.id, engine=cand_engine, state=CandidateState.DISCOVERED.value,
            state_history=[{"state": CandidateState.DISCOVERED.value, "at": now.isoformat(),
                            "reason": f"manual BUY requested by {user} from {source}"}],
            # manual_only: the automatic loop never picks this candidate up;
            # only this request evaluates it.
            detail={"source": "pump_stream", "mint": mint, "strategy": engine, "manual_only": True, "manual_request": request_id})
        session.add(candidate)
    await session.commit()
    req = {"id": request_id, "mint": mint, "symbol": (meta or {}).get("symbol"), "engine": engine,
           "route": ROUTE_LABEL[engine], "migrated": bool(curve and curve.pool), "candidate_id": str(candidate.id),
           "source": source, "user": user, "requested_at": now.isoformat(), "status": "QUEUED",
           "history": [{"status": "QUEUED", "at": now.isoformat()}]}
    await redis.set(PREFIX + request_id, json.dumps(req), ex=TTL_SECONDS)
    await redis.lpush(RECENT, request_id)
    await redis.ltrim(RECENT, 0, 49)
    await redis.rpush(QUEUE, request_id)
    await events.publish(redis, "manual_trade.updated", {"id": request_id, "status": "QUEUED", "mint": mint}, "api")
    return req


async def next_request(redis: Redis, timeout: int = 2) -> str | None:
    item = await redis.blpop(QUEUE, timeout=timeout)
    return item[1] if item else None
