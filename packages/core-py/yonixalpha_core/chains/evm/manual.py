"""Manual BUY on BSC and Robinhood Chain (master §45), paper only.

Not a second trading path. The API records the operator's request and
queues it for that chain's data-evm worker, which runs the same entry code
as automatic trading (chains.evm.paper.evaluate_entry with operator=True)
on the token's current venue (its launchpad curve, or the DEX after
migration). The operator's decision replaces the strategy's trade signal
(buy counts, distinct buyers, buy share); everything else still applies:
kill switch and trading switches, the launchpad's verified status, a fresh
safety PASS (re-run on the spot when older than 5 minutes), liquidity,
launch-window coordination, account limits, the re-entry cooldown, gas
(INSUFFICIENT GAS) and the risk plan. A blocked request lists every reason.
There is no override control (task #138 stays blocked).

Manual SELL uses the existing operator exit (POST /api/trade/sell/<id>,
exit_requested), which data-evm's position manager executes at the
executable sell quote of the position's current venue.

EVM LIVE execution is locked: a manual EVM buy is always PAPER.

Request status lives in Redis (yx:evm:manual:req:<id>, 24 h):
QUEUED -> EVALUATING -> BLOCKED | PAPER_POSITION_OPEN | FAILED | EXPIRED.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.db.models import EvmToken

CHAINS = ("bsc", "robinhood")
QUEUE = "yx:evm:manual:queue:{chain}"
PREFIX = "yx:evm:manual:req:"
RECENT = "yx:evm:manual:recent"
TTL_SECONDS = 24 * 3600
EXPIRES_AFTER = timedelta(minutes=10)  # a request no worker took within this is not executed late
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
FINAL = {"BLOCKED", "PAPER_POSITION_OPEN", "FAILED", "EXPIRED"}


class ManualTradeError(ValueError):
    pass


async def get(redis, request_id: str) -> dict[str, Any] | None:
    raw = await redis.get(PREFIX + request_id)
    return json.loads(raw) if raw else None


async def update(redis, request_id: str, status: str, at: datetime, **fields: Any) -> dict[str, Any] | None:
    req = await get(redis, request_id)
    if req is None:
        return None
    req.update(fields, status=status)
    req.setdefault("history", []).append({"status": status, "at": at.isoformat()})
    await redis.set(PREFIX + request_id, json.dumps(req, default=str), ex=TTL_SECONDS)
    return req


async def find_token(session: AsyncSession, chain: str, token: str) -> EvmToken | None:
    return (await session.execute(select(EvmToken).where(
        EvmToken.chain == chain, func.lower(EvmToken.token) == token.lower()))).scalar_one_or_none()


async def create_request(session: AsyncSession, redis, chain: str, token: str, requested_by: str,
                         now: datetime) -> dict[str, Any]:
    if chain not in CHAINS:
        raise ManualTradeError(f"chain must be one of {list(CHAINS)}")
    if not ADDRESS.match(token or ""):
        raise ManualTradeError("not a valid 0x token address")
    row = await find_token(session, chain, token)
    if row is None:
        raise ManualTradeError(f"this token has not been discovered on {chain} (only launchpad tokens are traded)")
    spec = LAUNCHPADS.get(row.launchpad)
    if spec is None or not spec.supports_trading:
        raise ManualTradeError(f"{spec.name if spec else row.launchpad} is observe only: it is not traded")
    rid = uuid.uuid4().hex
    req = {"id": rid, "chain": chain, "token": row.token, "symbol": row.symbol, "launchpad": row.launchpad,
           "mode": "PAPER", "status": "QUEUED", "requested_by": requested_by, "requested_at": now.isoformat(),
           "history": [{"status": "QUEUED", "at": now.isoformat()}]}
    await redis.set(PREFIX + rid, json.dumps(req), ex=TTL_SECONDS)
    await redis.lpush(QUEUE.format(chain=chain), rid)
    await redis.lpush(RECENT, rid)
    await redis.ltrim(RECENT, 0, 49)
    return req


async def next_request(redis, chain: str) -> str | None:
    rid = await redis.rpop(QUEUE.format(chain=chain))
    return rid.decode() if isinstance(rid, bytes) else rid


def expired(req: dict[str, Any], now: datetime) -> bool:
    return now - datetime.fromisoformat(req["requested_at"]) > EXPIRES_AFTER
