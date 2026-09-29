"""Discovery funnel: turns the pump.fun event stream into a small number of
TradingCandidates worth a full (RPC-costly) safety assessment.

Stage 1 (free, from Redis): every create event lands in the stream store.
Stage 2 (free): every new launch is FRESH_OBSERVING for the configured
window (solana.observation): its trades are compared at T0 / T+half /
T+window, and the outcome — PROMOTE, CONTINUE_MONITORING, REJECT, NO_TRADE
(expired) or MIGRATION_DETECTED — is stored with its full context in
token_observations. Liquidity is not a precondition here: a fresh token has
no DEX pool, the bonding curve is its market. Stage 3 (budgeted): at most
max_active_candidates tokens are under full gate assessment at once, since
each costs several RPC calls per evaluation.

Migrated tokens (CompletePumpAmmMigrationEvent) become candidates of the
migration engine directly, including tokens that were being observed as
fresh launches. Everything is counted in yx:pump:funnel, so the dashboard
can show where opportunities go.
"""

import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import deployer_intel, events, opportunities, wallet_intel
from yonixalpha_core.db.models import Token, TokenEvent, TokenObservation, TradingCandidate
from yonixalpha_core.logging import get_logger
from yonixalpha_core.safety.settings import SafetySettings
from yonixalpha_core.solana import intel, launch_features, observation, pump_stream
from yonixalpha_core.solana.codec import db_safe
from yonixalpha_core.solana.flow import acceleration, in_window
from yonixalpha_core.state_machine import CandidateState

log = get_logger("engine-solana-discovery.funnel")

SOURCE = "pump_stream"
FUNNEL = f"{pump_stream.PREFIX}:funnel"
MAX_CANDIDATE_AGE_SECONDS = 30 * 60
MIN_AGE_SECONDS = 60
PREFILTER_WINDOW_SECONDS = 300
MAX_ACTIVE_CANDIDATES = 25  # default of SafetySettings.max_active_candidates
OBSERVATION_RETENTION_DAYS = 3
OBS_REPORT_TTL = 6 * 3600
OUTCOME_COUNTER = {observation.REJECT: "rejected", observation.NO_TRADE: "expired",
                   observation.MIGRATION_DETECTED: "migration_detected"}
ACTIVE_STATES = [
    CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value, CandidateState.ANALYZING.value,
    CandidateState.WAITING_FOR_LIQUIDITY.value, CandidateState.WAITING_FOR_APPROVAL.value,
]
ENGINE_STRATEGY = {"discovery": "solana_fresh", "migration": "solana_migration", "momentum": "solana_momentum"}
MOMENTUM_MIN_AGE_SECONDS = 30 * 60  # default of SafetySettings.momentum_min_age_seconds
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
            TradingCandidate.state.notin_([CandidateState.CLOSED.value, CandidateState.REJECTED.value,
                                           CandidateState.MIGRATED.value]),
        )
    )
    if existing.first() is not None:
        return None
    if meta.get("signature"):
        await session.execute(
            insert(TokenEvent)
            .values(token_id=token.id, event_type="created", source=SOURCE, occurred_at=now,
                    signature=meta["signature"], trader_address=meta.get("creator") or None, payload=db_safe(meta))
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


def momentum_prefilter(trades: list, now: datetime, created_ts: int | None, settings: SafetySettings,
                       near_migration: bool = False) -> tuple[bool, dict[str, Any]]:
    """Cheap stream-only screen: old enough not to be a launch (or
    approaching migration), active now, and trading faster than in the
    previous window. The full multi-factor momentum signal runs later
    inside the gate."""
    age = now.timestamp() - created_ts if created_ts else None
    current, prior, ratio = acceleration(trades, now, PREFILTER_WINDOW_SECONDS)
    stats = {"age_seconds": int(age) if age else None, "trades": current, "prior_trades": prior,
             "tx_acceleration": round(ratio, 2) if ratio else None, "near_migration": near_migration}
    old_enough = age is not None and (age >= settings.momentum_min_age_seconds or near_migration)
    ok = (old_enough and current >= settings.min_trades_in_window
          and prior > 0 and ratio is not None and ratio >= MOMENTUM_MIN_TX_ACCELERATION)
    return ok, stats


async def _finalize(redis: Redis, rows: list[dict], mint: str, meta: dict[str, str], report: observation.ObservationReport,
                    now: datetime, candidate_id=None) -> None:
    """A final observation outcome: never re-evaluated, stored with its full
    context for the dashboard ("why didn't the bot trade this token?")."""
    await redis.zadd(pump_stream.OBS_FINAL, {mint: int(now.timestamp())})
    await redis.zrem(pump_stream.OBS_LIVE, mint)
    await redis.hdel(pump_stream.OBS_SINCE, mint)
    await redis.set(pump_stream.obs_report_key(mint), json.dumps(report.to_dict(), default=str), ex=OBS_REPORT_TTL)
    launched = datetime.fromtimestamp(int(meta["created_at"]), tz=timezone.utc) if meta.get("created_at") else None
    rows.append({"mint": mint, "symbol": (meta.get("symbol") or None) and meta["symbol"][:32],
                 "name": (meta.get("name") or None) and meta["name"][:128], "creator": meta.get("creator") or None,
                 "launched_at": launched, "outcome": report.outcome, "trend": report.trend, "reasons": report.reasons,
                 "report": json.loads(json.dumps(report.to_dict(), default=str)), "candidate_id": candidate_id,
                 "decided_at": now})


async def _ledger_intel(redis: Redis, mint: str, now: datetime, settings: SafetySettings, session=None) -> dict[str, Any] | None:
    """The same causal launch intelligence the gate records (solana.intel +
    wallet_intel), for an opportunity the observation window passed on.
    Evidence only; a failure is recorded in place of it."""
    try:
        meta = await pump_stream.load_meta(redis, mint) or {}
        trades = [t for t in await pump_stream.load_trades(redis, mint) if t.at <= now]
        started = await redis.get(pump_stream.STREAM_STARTED)
        started_ts = int(started) if started else None
        created_ts = int(meta["created_at"]) if meta.get("created_at") else None
        complete = launch_features.coverage(trades, created_ts, started_ts)["complete"] is True
        try:
            wallets = await wallet_intel.assess(
                redis, trades, now, wallet_intel.config(settings), complete_history=complete, mint=mint,
                created_at=datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None)
        except Exception as exc:  # noqa: BLE001 - wallet history failing must not drop the launch features
            wallets = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        rec = intel.curve_intel(trades, now, settings, meta=meta, curve=None, curve_from_chain=False, decimals=6,
                                supply_raw=int(opportunities.PUMP_SUPPLY_RAW), stream_started_ts=started_ts, funding=None,
                                duplicate_of=None, dump_cluster=wallets.get("dump_cluster"),
                                recycled_wallets=set(wallets.get("recycled_wallets") or []),
                                stream_heartbeat=await pump_stream.heartbeat(redis))
        rec["wallets"] = wallets
        await intel.add_relationships(rec, redis, trades, now, settings, creator=meta.get("creator") or None, mint=mint,
                                      wallets=wallets, keep_wallets=0)
        if session is not None:
            try:
                async with session.begin_nested():
                    rec["deployer"] = await deployer_intel.features_asof(session, meta.get("creator") or None, now,
                                                                         exclude_mint=mint)
            except Exception as exc:  # noqa: BLE001 - evidence only
                rec["deployer"] = {"status": "UNAVAILABLE", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return rec
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


async def _store_observations(session_factory, rows: list[dict], now: datetime, redis: Redis,
                              settings: SafetySettings | None = None) -> None:
    if not rows:
        return
    async with session_factory() as session:
        await session.execute(insert(TokenObservation).values(db_safe(rows)).on_conflict_do_nothing(index_elements=["mint"]))
        # Every non-promoted outcome is an opportunity the system passed on:
        # what the token did afterwards is tracked for review and learning.
        for r in rows:
            if r["outcome"] in ("REJECT", "NO_TRADE"):
                snap = opportunities.observation_snapshot(r.get("report") or {})
                if settings is not None:
                    snap["intel"] = json.loads(json.dumps(await _ledger_intel(redis, r["mint"], now, settings, session), default=str))
                await opportunities.record(
                    session, key=f"obs:{r['mint']}", mint=r["mint"], symbol=db_safe(r.get("symbol")), engine="solana_fresh",
                    stage="OBSERVATION", decision=r["outcome"], traded=False, reasons=db_safe(r.get("reasons") or []),
                    decided_at=r["decided_at"], snapshot=db_safe(snap))
        # Retention: once an hour, drop outcomes older than the window.
        if await redis.set(f"{pump_stream.PREFIX}:obs_prune", "1", nx=True, ex=3600):
            await session.execute(delete(TokenObservation).where(
                TokenObservation.decided_at < now - timedelta(days=OBSERVATION_RETENTION_DAYS)))
        await session.commit()


async def observe_fresh(redis: Redis, session_factory, settings: SafetySettings, now: datetime, counts: dict[str, int],
                        active: int) -> int:
    """FRESH_OBSERVING for every new launch: see solana.observation. Returns
    the updated number of active gate candidates."""
    rows: list[dict] = []
    live: list[tuple[int, str, dict, observation.ObservationReport]] = []
    open_mints = await pump_stream.open_for_observation(redis, now, max(settings.fresh_max_monitoring_seconds,
                                                                        settings.fresh_observation_seconds) + 60)
    for mint, created_ts in open_mints:
        counts["considered"] += 1
        meta = await pump_stream.load_meta(redis, mint) or {}
        since_raw = await redis.hget(pump_stream.OBS_SINCE, mint)
        since = datetime.fromtimestamp(int(since_raw), tz=timezone.utc) if since_raw else None
        report = observation.evaluate(
            mint, await pump_stream.load_trades(redis, mint), created_ts, now, settings, creator=meta.get("creator") or None,
            curve=await pump_stream.load_curve(redis, mint),
            initial_real_token_reserves=int(meta.get("initial_real_token_reserves") or 0) or None, monitoring_since=since)
        if report.outcome == observation.PROMOTE and active >= settings.max_active_candidates:
            counts["budget_full"] += 1
            if (report.age_seconds or 0) >= settings.fresh_max_monitoring_seconds:
                report.outcome = observation.NO_TRADE
                report.reasons.append(f"NO_TRADE: gate budget stayed full (max_active_candidates="
                                      f"{settings.max_active_candidates}) until the monitoring limit")
            else:
                report.outcome = observation.CONTINUE_MONITORING
                report.reasons.append(f"CONTINUE_MONITORING: qualified, but the gate budget is full "
                                      f"(max_active_candidates={settings.max_active_candidates})")
        if report.outcome == observation.PROMOTE:
            if not await pump_stream.mark_promoted(redis, mint, now):
                continue
            stats = {"age_seconds": int(report.age_seconds or 0), "trades": report.metrics.get("trades_total"),
                     "unique_buyers": report.metrics.get("unique_buyers_total"), "trend": report.trend}
            async with session_factory() as session:
                cand = await create_candidate(session, "discovery", mint, meta, now,
                                              "observation window passed: " + report.reasons[-1][:200],
                                              {"prefilter": stats, "observation": json.loads(json.dumps(report.to_dict(), default=str))})
            await _finalize(redis, rows, mint, meta, report, now, cand.id if cand else None)
            if cand is not None:
                active += 1
                counts["promoted"] += 1
                log.info("funnel.promoted", mint=mint, **stats)
                await events.publish(redis, "token.discovered", {"mint": mint, "symbol": meta.get("symbol"), "engine": "solana_fresh",
                                                                 "candidate_id": str(cand.id), **stats}, "funnel")
            continue
        if report.outcome in observation.FINAL_OUTCOMES:
            counts[OUTCOME_COUNTER[report.outcome]] += 1
            counts["prefilter_failed"] += report.outcome in (observation.REJECT, observation.NO_TRADE)
            await _finalize(redis, rows, mint, meta, report, now)
            continue
        # OBSERVING / CONTINUE_MONITORING: still live.
        if report.outcome == observation.CONTINUE_MONITORING and since is None:
            await redis.hset(pump_stream.OBS_SINCE, mint, int(now.timestamp()))
        counts["observing" if report.outcome == observation.OBSERVING else "continue_monitoring"] += 1
        live.append((int(report.metrics.get("trades_total") or 0), mint, meta, report))

    # Capacity: beyond fresh_max_monitored_tokens, the least active tokens
    # past their first window stop being monitored (recorded as NO_TRADE).
    if len(live) > settings.fresh_max_monitored_tokens:
        extra = len(live) - settings.fresh_max_monitored_tokens
        for _, mint, meta, report in sorted((x for x in live if x[3].outcome == observation.CONTINUE_MONITORING),
                                            key=lambda x: x[0])[:extra]:
            report.outcome = observation.NO_TRADE
            report.reasons.append(f"NO_TRADE: monitoring capacity reached (fresh_max_monitored_tokens="
                                  f"{settings.fresh_max_monitored_tokens}); least active token dropped")
            counts["continue_monitoring"] -= 1
            counts["expired"] += 1
            await _finalize(redis, rows, mint, meta, report, now)
    pipe = redis.pipeline(transaction=False)
    for _, mint, meta, report in live:
        if report.outcome in (observation.OBSERVING, observation.CONTINUE_MONITORING):
            created = int(meta["created_at"]) if meta.get("created_at") else int(now.timestamp())
            pipe.zadd(pump_stream.OBS_LIVE, {mint: created})
            pipe.set(pump_stream.obs_report_key(mint), json.dumps(report.to_dict(), default=str), ex=OBS_REPORT_TTL)
    await pipe.execute()
    await _store_observations(session_factory, rows, now, redis, settings)
    return active


async def run_funnel(redis: Redis, session_factory, settings: SafetySettings, now: datetime,
                     momentum_settings: SafetySettings | None = None) -> dict[str, int]:
    """`settings` are solana_fresh's (fresh observation, shared gate budget);
    `momentum_settings` are solana_momentum's, so a value saved on the
    Momentum tab of Risk Settings drives the momentum pre-filter."""
    mom = momentum_settings or settings
    counts = {"considered": 0, "prefilter_failed": 0, "promoted": 0, "budget_full": 0, "migrations": 0,
              "momentum_considered": 0, "momentum_promoted": 0, "observing": 0, "continue_monitoring": 0,
              "rejected": 0, "expired": 0, "migration_detected": 0}
    async with session_factory() as session:
        active = await active_candidate_count(session)

    active = await observe_fresh(redis, session_factory, settings, now, counts, active)
    budget = settings.max_active_candidates

    since = int((now - timedelta(seconds=MAX_CANDIDATE_AGE_SECONDS)).timestamp())
    for mint, migrated_ts in await pump_stream.migrated_since(redis, since):
        if active >= budget:
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
        curve = await pump_stream.load_curve(redis, mint)
        if curve is None or curve.complete:
            continue
        progress = Decimal(observation.curve_context(curve, int(meta.get("initial_real_token_reserves") or 0) or None)
                           .get("curve_progress") or 0)
        near = progress >= mom.momentum_near_migration_progress
        if created_ts and now.timestamp() - created_ts < mom.momentum_min_age_seconds:
            # A young token belongs to the fresh engine, unless it is close to
            # migration and the fresh engine did not take it.
            if (not near or await redis.zscore(pump_stream.PROMOTED, mint) is not None
                    or await redis.zscore(pump_stream.OBS_LIVE, mint) is not None):
                continue
        counts["momentum_considered"] += 1
        ok, stats = momentum_prefilter(await pump_stream.load_trades(redis, mint), now, created_ts, mom, near)
        if not ok:
            continue
        if active >= budget:
            counts["budget_full"] += 1
            continue
        if not await redis.set(f"{pump_stream.PREFIX}:mom_seen:{mint}", "1", nx=True, ex=MOMENTUM_REPROMOTE_SECONDS):
            continue
        async with session_factory() as session:
            reason = ("momentum prefilter: accelerating activity, approaching migration "
                      f"({progress:.0%} of the curve sold)" if near else "momentum prefilter: accelerating activity")
            cand = await create_candidate(session, "momentum", mint, meta, now, reason, {"prefilter": stats})
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
