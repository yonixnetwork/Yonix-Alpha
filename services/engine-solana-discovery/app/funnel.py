"""Discovery funnel: turns the pump.fun event stream into a small number of
TradingCandidates worth a full (RPC-costly) safety assessment.

Stage 1 (free, from Redis): every create event lands in the stream store.
Stage 2 (free): a mint is promoted only once its own trades show enough
activity to be assessable at all — the same minimum trade/buyer counts the
gate itself enforces, so promotion never admits something the gate would
refuse for thin activity alone. Stage 3 (budgeted): at most
MAX_ACTIVE_CANDIDATES are under assessment at once, since each costs
several RPC calls per evaluation.

Migrated tokens (CompletePumpAmmMigrationEvent) become candidates of the
migration engine directly. Everything dropped is counted in
yx:pump:funnel, so the dashboard can show where opportunities go.
"""

from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events
from yonixalpha_core.db.models import Token, TokenEvent, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.settings import SafetySettings
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.flow import acceleration, in_window
from yonixalpha_core.state_machine import CandidateState

log = get_logger("engine-solana-discovery.funnel")

SOURCE = "pump_stream"
FUNNEL = f"{pump_stream.PREFIX}:funnel"
MAX_CANDIDATE_AGE_SECONDS = 30 * 60
MIN_AGE_SECONDS = 60
PREFILTER_WINDOW_SECONDS = 300
MAX_ACTIVE_CANDIDATES = 25
ACTIVE_STATES = [
    CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value, CandidateState.ANALYZING.value,
    CandidateState.WAITING_FOR_LIQUIDITY.value, CandidateState.WAITING_FOR_APPROVAL.value,
]
ENGINE_STRATEGY = {"discovery": "solana_fresh", "migration": "solana_migration", "momentum": "solana_momentum"}
MOMENTUM_MIN_AGE_SECONDS = 30 * 60
MOMENTUM_ACTIVE_WITHIN_SECONDS = 120
MOMENTUM_MIN_TX_ACCELERATION = 1.5
MOMENTUM_REPROMOTE_SECONDS = 3600


def prefilter(trades: list, now: datetime, created_ts: int, settings: SafetySettings) -> tuple[bool, dict[str, Any]]:
    window = in_window(trades, now, PREFILTER_WINDOW_SECONDS)
    buyers = {t.trader for t in window if t.is_buy}
    age = now.timestamp() - created_ts
    stats = {"age_seconds": int(age), "trades": len(window), "unique_buyers": len(buyers)}
    ok = age >= MIN_AGE_SECONDS and len(window) >= settings.min_trades_in_window and len(buyers) >= settings.min_unique_buyers
    return ok, stats


async def active_candidate_count(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(TradingCandidate).where(
            TradingCandidate.state.in_(ACTIVE_STATES), TradingCandidate.detail["source"].astext == SOURCE
        )
    )
    return result.scalar_one()


async def _upsert_token(session: AsyncSession, mint: str, meta: dict[str, str], now: datetime) -> Token:
    await session.execute(
        insert(Token)
        .values(
            mint_address=mint,
            creator_address=meta.get("creator") or None,
            symbol=(meta.get("symbol") or None) and meta["symbol"][:32],
            name=(meta.get("name") or None) and meta["name"][:128],
            metadata_uri=(meta.get("uri") or None) and meta["uri"][:512],
            first_seen_source=SOURCE,
            last_event_at=now,
        )
        .on_conflict_do_nothing(index_elements=["mint_address"])
    )
    return (await session.execute(select(Token).where(Token.mint_address == mint))).scalar_one()


async def create_candidate(
    session: AsyncSession, engine: str, mint: str, meta: dict[str, str], now: datetime, reason: str, detail: dict[str, Any]
) -> TradingCandidate | None:
    """One open candidate per (token, engine): returns None if one exists."""
    token = await _upsert_token(session, mint, meta, now)
    existing = await session.execute(
        select(TradingCandidate.id).where(
            TradingCandidate.token_id == token.id,
            TradingCandidate.engine == engine,
            TradingCandidate.state.notin_([CandidateState.CLOSED.value, CandidateState.REJECTED.value]),
        )
    )
    if existing.first() is not None:
        return None
    if meta.get("signature"):
        await session.execute(
            insert(TokenEvent)
            .values(token_id=token.id, event_type="created", source=SOURCE, occurred_at=now,
                    signature=meta["signature"], trader_address=meta.get("creator") or None, payload=meta)
            .on_conflict_do_nothing()
        )
    candidate = TradingCandidate(
        token_id=token.id,
        engine=engine,
        state=CandidateState.DISCOVERED.value,
        state_history=[{"state": CandidateState.DISCOVERED.value, "at": now.isoformat(), "reason": reason}],
        detail={"source": SOURCE, "strategy": ENGINE_STRATEGY[engine], "mint": mint, **detail},
    )
    session.add(candidate)
    await session.commit()
    return candidate


def momentum_prefilter(trades: list, now: datetime, created_ts: int | None, settings: SafetySettings) -> tuple[bool, dict[str, Any]]:
    """Cheap stream-only screen for established tokens: old enough not to be
    a launch, active now, and trading faster than in the previous window.
    The full multi-factor momentum signal runs later inside the gate."""
    age = now.timestamp() - created_ts if created_ts else None
    current, prior, ratio = acceleration(trades, now, PREFILTER_WINDOW_SECONDS)
    stats = {"age_seconds": int(age) if age else None, "trades": current, "prior_trades": prior,
             "tx_acceleration": round(ratio, 2) if ratio else None}
    ok = (age is not None and age >= MOMENTUM_MIN_AGE_SECONDS and current >= settings.min_trades_in_window
          and prior > 0 and ratio is not None and ratio >= MOMENTUM_MIN_TX_ACCELERATION)
    return ok, stats


async def run_funnel(redis: Redis, session_factory, settings: SafetySettings, now: datetime) -> dict[str, int]:
    counts = {"considered": 0, "prefilter_failed": 0, "promoted": 0, "budget_full": 0, "migrations": 0,
              "momentum_considered": 0, "momentum_promoted": 0}
    async with session_factory() as session:
        active = await active_candidate_count(session)

    for mint, created_ts in await pump_stream.recent_unpromoted(redis, now, MAX_CANDIDATE_AGE_SECONDS):
        counts["considered"] += 1
        trades = await pump_stream.load_trades(redis, mint)
        ok, stats = prefilter(trades, now, created_ts, settings)
        if not ok:
            counts["prefilter_failed"] += 1
            continue
        if active >= MAX_ACTIVE_CANDIDATES:
            counts["budget_full"] += 1
            continue
        if not await pump_stream.mark_promoted(redis, mint, now):
            continue
        meta = await pump_stream.load_meta(redis, mint) or {}
        async with session_factory() as session:
            cand = await create_candidate(session, "discovery", mint, meta, now, "passed stream prefilter", {"prefilter": stats})
        if cand is not None:
            active += 1
            counts["promoted"] += 1
            log.info("funnel.promoted", mint=mint, **stats)
            await events.publish(redis, "token.discovered", {"mint": mint, "symbol": meta.get("symbol"), "engine": "solana_fresh",
                                                             "candidate_id": str(cand.id), **stats}, "funnel")

    since = int((now - timedelta(seconds=MAX_CANDIDATE_AGE_SECONDS)).timestamp())
    for mint, migrated_ts in await pump_stream.migrated_since(redis, since):
        if active >= MAX_ACTIVE_CANDIDATES:
            counts["budget_full"] += 1
            continue  # not marked seen: retried next run while still recent
        if not await redis.set(f"{pump_stream.PREFIX}:mig_seen:{mint}", "1", nx=True, ex=86400):
            continue
        meta = await pump_stream.load_meta(redis, mint) or {}
        curve = await pump_stream.load_curve(redis, mint)
        async with session_factory() as session:
            cand = await create_candidate(session, "migration", mint, meta, now, "pump.fun migration event",
                                          {"pool": curve.pool if curve else None, "migrated_ts": migrated_ts})
        if cand is not None:
            active += 1
            counts["migrations"] += 1
            await events.publish(redis, "migration.detected", {"mint": mint, "symbol": meta.get("symbol"),
                                                               "pool": curve.pool if curve else None,
                                                               "candidate_id": str(cand.id)}, "funnel")

    # Momentum: established tokens (not launches) trading faster right now.
    for mint in await pump_stream.active_mints(redis, now, MOMENTUM_ACTIVE_WITHIN_SECONDS):
        meta = await pump_stream.load_meta(redis, mint)
        if not meta or not meta.get("bonding_curve"):
            continue  # launched before this stream started: curve address unknown
        created_ts = int(meta["created_at"]) if meta.get("created_at") else None
        if created_ts and now.timestamp() - created_ts < MOMENTUM_MIN_AGE_SECONDS:
            continue  # still a fresh launch; the discovery engine owns it
        curve = await pump_stream.load_curve(redis, mint)
        if curve is None or curve.complete:
            continue
        counts["momentum_considered"] += 1
        ok, stats = momentum_prefilter(await pump_stream.load_trades(redis, mint), now, created_ts, settings)
        if not ok:
            continue
        if active >= MAX_ACTIVE_CANDIDATES:
            counts["budget_full"] += 1
            continue
        if not await redis.set(f"{pump_stream.PREFIX}:mom_seen:{mint}", "1", nx=True, ex=MOMENTUM_REPROMOTE_SECONDS):
            continue
        async with session_factory() as session:
            cand = await create_candidate(session, "momentum", mint, meta, now, "momentum prefilter: accelerating activity",
                                          {"prefilter": stats})
        if cand is not None:
            active += 1
            counts["momentum_promoted"] += 1
            await events.publish(redis, "token.discovered", {"mint": mint, "symbol": meta.get("symbol"),
                                                             "engine": "solana_momentum", "candidate_id": str(cand.id), **stats},
                                 "funnel")

    pipe = redis.pipeline(transaction=False)
    for k, v in counts.items():
        if v:
            pipe.hincrby(FUNNEL, k, v)
    pipe.hset(FUNNEL, "last_run_at", now.isoformat())
    await pipe.execute()
    await pump_stream.prune(redis, now)
    return counts
