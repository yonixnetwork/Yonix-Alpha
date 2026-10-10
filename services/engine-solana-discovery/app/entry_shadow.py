"""Early-entry intelligence pass (SHADOW) inside the discovery service.

Every PASS_INTERVAL_SECONDS, for tokens being observed and tokens with
recent trades: recompute the early-momentum features ONLY when the token
had new trades (or REFRESH_SECONDS passed), classify the momentum phase,
run every entry strategy (strategy_registry lists them), route the token
by category, write the per-mint state the dashboard and the gate's event
trigger read, and record the first CANDIDATE of each strategy
(entry_signals). Only the routed strategy may create a gate candidate, only
when it is in PAPER mode, and at most once per token (strategy_registry.
claim_route); that entry can only be a PAPER trade and the safety gate
decides as for any other candidate. Nothing here places an order.

Background parts, slower: label signals whose horizon passed (60 s), sample
recently migrated PumpSwap pools and record the migrated entry variants
(30 s), demote reliably losing PAPER strategies to SHADOW (10 min). All of
it pauses at CRITICAL resource pressure and never blocks the funnel or the
stream.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_store, entry_timing, gate_events, resources
from yonixalpha_core import strategy_registry as reg
from yonixalpha_core.db.models import ModelVersion
from yonixalpha_core.logging import get_logger
from yonixalpha_core.solana import pump_stream

log = get_logger("engine-solana-discovery.entry_shadow")

PASS_INTERVAL_SECONDS = 3.0
LABEL_INTERVAL_SECONDS = 60.0
MIG_INTERVAL_SECONDS = 30.0
DEMOTE_INTERVAL_SECONDS = 600.0
SETTINGS_REFRESH_SECONDS = 30.0
REFRESH_SECONDS = 30.0
ACTIVE_WITHIN_SECONDS = 120
MAX_MINTS_PER_PASS = 150
SUPPLY_RAW = 10 ** 15
PASS_STATS = "yx:ee:pass"
WHY_KEY, ROUTE_STATS, STATS_TTL = reg.WHY_KEY, reg.ROUTE_STATS, reg.STATS_TTL


class Shadow:
    def __init__(self) -> None:
        self.fp: dict[str, tuple[int, float, float]] = {}  # mint -> (trades, last trade ts, evaluated monotonic)
        self.settings: dict | None = None
        self.settings_at = 0.0
        self.model: dict | None = None
        self.last_label = 0.0
        self.last_mig = 0.0
        self.last_demote = 0.0

    def prune(self, keep: set[str]) -> None:
        if len(self.fp) > 5000:
            self.fp = {m: v for m, v in self.fp.items() if m in keep}


async def _settings(shadow: Shadow, session_factory) -> dict:
    if shadow.settings is None or time.monotonic() - shadow.settings_at > SETTINGS_REFRESH_SECONDS:
        async with session_factory() as session:
            shadow.settings = await entry_store.load_settings(session)
            row = (await session.execute(select(ModelVersion.metrics).where(ModelVersion.name == "entry_timing")
                                         .order_by(ModelVersion.version.desc()).limit(1))).scalar_one_or_none()
            shadow.model = (row or {}).get("coefficients") if row else None
        shadow.settings_at = time.monotonic()
    return shadow.settings


def _state(mint: str, meta: dict, f: dict, res: dict, wallets: dict | None, now: datetime, ml: float | None) -> dict:
    price = f.get("price_raw")
    return {
        "mint": mint, "symbol": meta.get("symbol"), "name": meta.get("name"), "evaluated_at": now.isoformat(),
        "age_seconds": f.get("age_seconds"), "phase": res["phase"], "phase_evidence": res["phase_evidence"],
        "price_raw": price, "market_cap_sol": round(price * SUPPLY_RAW / 1e9, 4) if price else None,
        "curve_progress": f.get("curve_progress"), "inflow_sol_per_s": f.get("inflow_sol_per_s"),
        "inflow_acceleration": f.get("inflow_acceleration_sol_per_s2"), "trade_rate_per_s": f.get("trade_rate_per_s"),
        "buyer_growth_10s": f.get("buyer_growth_10s"), "seller_growth_10s": f.get("seller_growth_10s"),
        "displacement_pct": f.get("displacement_pct"), "drawdown_from_peak_pct": f.get("drawdown_from_peak_pct"),
        "round_trip_cost_pct": f.get("round_trip_cost_pct"), "last_trade_age_seconds": f.get("last_trade_age_seconds"),
        "stream_age_seconds": f.get("stream_age_seconds"), "complete_history": f.get("complete_history"),
        "strategies": res["strategies"], "category": res.get("category"), "route": res.get("route"),
        "paper_route": res.get("paper_route"), "ml_probability": ml,
        "smart_wallets": {k: wallets.get(k) for k in ("status", "reason", "proven_wallets", "evaluated_wallets")} if wallets else None,
        "features": ei.compact_features(f),
    }


async def shadow_pass(redis, session_factory, shadow: Shadow, now: datetime, funnel_create=None) -> dict:
    """One pass. `funnel_create(session, mint, meta, now, strategy)` creates a
    PAPER-only gate candidate (or returns None)."""
    s = await _settings(shadow, session_factory)
    if not s.get("enabled", True):
        return {"skipped": "disabled"}
    cfg: ei.EntryConfig = s["config"]
    modes: dict = s["modes"]
    observing = [m for m, _ in await pump_stream.open_for_observation(redis, now, cfg.ea_max_age_seconds + 900, limit=400)]
    active = await pump_stream.active_mints(redis, now, ACTIVE_WITHIN_SECONDS, limit=300)
    mints = list(dict.fromkeys(observing + active))[:MAX_MINTS_PER_PASS * 2]
    hb = await pump_stream.heartbeat(redis)
    started_raw = await redis.get(pump_stream.STREAM_STARTED)
    started_ts = int(started_raw) if started_raw else None
    counts = {"considered": len(mints), "evaluated": 0, "recorded": 0, "paper_candidates": 0}
    why: dict[str, dict[str, int]] = {}
    routes: dict[str, int] = {}
    evaluated = 0
    async with session_factory() as session:
        for mint in mints:
            if evaluated >= MAX_MINTS_PER_PASS:
                break
            trades = await pump_stream.load_trades(redis, mint)
            if not trades:
                continue
            last_ts = max(t.at for t in trades).timestamp()
            prev = shadow.fp.get(mint)
            if prev and prev[0] == len(trades) and prev[1] == last_ts and time.monotonic() - prev[2] < REFRESH_SECONDS:
                continue  # no new event: nothing to recompute
            meta = await pump_stream.load_meta(redis, mint) or {}
            curve = await pump_stream.load_curve(redis, mint)
            if curve is not None and (curve.pool or curve.complete):
                shadow.fp[mint] = (len(trades), last_ts, time.monotonic())
                continue  # migrated / completing: the migrated variants cover it
            created_ts = int(meta["created_at"]) if meta.get("created_at") else None
            complete = entry_store.complete_history(trades, meta, started_ts)
            f = ei.early_features(trades, now, created_ts=created_ts, creator=meta.get("creator") or None,
                                  complete_history=complete, fee_bps=curve.fee_bps if curve else None, cfg=cfg,
                                  stream_heartbeat=hb)
            res = ei.evaluate(f, cfg)
            wallets = None
            if res["route"]["selected"]:
                try:
                    async with session.begin_nested():
                        wallets = await entry_store.wallet_evidence(session, redis, trades, now,
                                                                    fee_bps=curve.fee_bps if curve and curve.fee_bps else cfg.fee_bps)
                except Exception as exc:  # noqa: BLE001 - evidence only
                    wallets = {"status": "UNKNOWN", "reason": f"wallet evidence failed: {type(exc).__name__}"}
                res = ei.evaluate(f, cfg, wallets)
            # The paper route: the same router over the strategies in PAPER mode only (a SHADOW or
            # PAUSED strategy never trades), so one token gets one paper decision.
            paper_route = ei.route({n: ei.StrategyDecision(**d) for n, d in res["strategies"].items()
                                    if modes.get(n) == "PAPER"}, res["category"])
            res["paper_route"] = paper_route
            _count(why, routes, res)
            ml = None
            evaluated += 1
            shadow.fp[mint] = (len(trades), last_ts, time.monotonic())
            launch_at = datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None
            if created_ts:
                await entry_timing.mark(redis, mint, "launch_observed_at", float(created_ts))
            if meta.get("received_at"):
                await entry_timing.mark(redis, mint, "event_received_at", float(meta["received_at"]))
            await entry_timing.mark(redis, mint, "features_ready_at", now.timestamp())
            for name in ei.STRATEGIES:
                d = res["strategies"][name]
                if d["decision"] != ei.CANDIDATE or modes.get(name) == "PAUSED":
                    continue
                if shadow.model:
                    ml = ei.logistic_predict(shadow.model, ei.model_vector(f, d.get("score")))
                if not await entry_store.claim(redis, name, mint):
                    continue
                await entry_timing.mark(redis, mint, f"first_candidate_at:{name}", now.timestamp())
                cand_id = None
                routed = paper_route["selected"] == name
                if (routed and modes.get(name) == "PAPER" and name != ei.SMART_WALLET_CONFIRMATION
                        and funnel_create is not None and await reg.claim_route(redis, mint, name)):
                    try:
                        cand = await funnel_create(session, mint, meta, now, name)
                        if cand is not None:
                            cand_id = cand.id
                            counts["paper_candidates"] += 1
                            await gate_events.wake(redis, cand.id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("entry_shadow.paper_candidate_failed", mint=mint, error=f"{type(exc).__name__}: {exc}")
                    # The claim is kept even when no candidate was created (budget full, or the
                    # token already has an open candidate whose trade may lose): never a second try.
                ok = await entry_store.record_signal(
                    session, mint=mint, strategy=name, lifecycle="FRESH", decision=ei.CANDIDATE, decided_at=now,
                    phase=res["phase"], score=d.get("score"), evidence_level=d.get("evidence_level"),
                    size_factor=d.get("size_factor"), launch_at=launch_at, price_raw=f.get("price_raw"),
                    features=ei.compact_features(f), reasons=d.get("reasons", []) + d.get("positives", []),
                    evidence={"smart_wallets": wallets, "mode": modes.get(name), "phase_evidence": res["phase_evidence"],
                              "strategy_version": reg.version_of(name), "category": res["category"],
                              "route": res["route"], "paper_route": paper_route},
                    candidate_id=cand_id, ml_probability=ml)
                counts["recorded"] += int(ok)
            if (ei.naive_sampled(mint, cfg.naive_sample_every) and f.get("trades_total", 0) >= 10
                    and complete is not False):
                counts["recorded"] += int(await entry_store.record_baseline(
                    session, redis, mint=mint, strategy=ei.NAIVE_SAMPLE, decided_at=now, price_raw=f.get("price_raw"),
                    launch_at=launch_at, detail=ei.compact_features(f)))
            await entry_store.write_state(redis, mint, now, _state(mint, meta, f, res, wallets, now, ml))
        await session.commit()
    counts["evaluated"] = evaluated
    await _flush_counters(redis, now, why, routes)
    shadow.prune(set(mints))
    await redis.hset(PASS_STATS, mapping={"last_pass_at": now.isoformat(), **{k: v for k, v in counts.items()}})
    return counts


def _count(why: dict[str, dict[str, int]], routes: dict[str, int], res: dict) -> None:
    for name, d in res["strategies"].items():
        if d["decision"] != ei.CANDIDATE and d.get("reasons"):
            k = reg.reason_key(d["reasons"][0])
            why.setdefault(name, {})[k] = why.get(name, {}).get(k, 0) + 1
    r = res["route"]
    k = f"selected:{r['selected']}" if r["selected"] else f"no_trade:{r['category'] or 'UNKNOWN'}"
    routes[k] = routes.get(k, 0) + 1


async def _flush_counters(redis, now: datetime, why: dict[str, dict[str, int]], routes: dict[str, int]) -> None:
    if not why and not routes:
        return
    day = now.strftime("%Y%m%d")
    pipe = redis.pipeline(transaction=False)
    for name, reasons in why.items():
        key = f"{WHY_KEY}{name}:{day}"
        for k, v in reasons.items():
            pipe.hincrby(key, k, v)
        pipe.expire(key, STATS_TTL)
    for k, v in routes.items():
        pipe.hincrby(f"{ROUTE_STATS}{day}", k, v)
    pipe.expire(f"{ROUTE_STATS}{day}", STATS_TTL)
    await pipe.execute()


top_reasons = reg.top_reasons


async def background(redis, session_factory, rpc, shadow: Shadow, now: datetime) -> dict:
    out: dict = {}
    s = await _settings(shadow, session_factory)
    if time.monotonic() - shadow.last_label >= LABEL_INTERVAL_SECONDS:
        shadow.last_label = time.monotonic()
        async with session_factory() as session:
            out["labels"] = await entry_store.label_due(session, redis, now)
    if rpc is not None and time.monotonic() - shadow.last_mig >= MIG_INTERVAL_SECONDS:
        shadow.last_mig = time.monotonic()
        out["migrated"] = await entry_store.sample_migrated(redis, rpc, now)
        async with session_factory() as session:
            out["migrated_recorded"] = await entry_store.record_migrated_variants(session, redis, now, s["modes"])
    if time.monotonic() - shadow.last_demote >= DEMOTE_INTERVAL_SECONDS:
        shadow.last_demote = time.monotonic()
        async with session_factory() as session:
            dem = await reg.apply_demotions(session, now)
        if dem.get("demoted"):
            out["demoted"] = dem
            shadow.settings = None  # reload the modes now
    return out


async def run(redis, session_factory, rpc, app_settings, stop_event: asyncio.Event, funnel_create=None) -> None:
    shadow = Shadow()
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            level, _ = resources.level(resources.sample(), app_settings)
        except Exception:  # noqa: BLE001
            level = "UNKNOWN"
        if level != resources.CRITICAL:
            now = datetime.now(timezone.utc)
            try:
                await shadow_pass(redis, session_factory, shadow, now, funnel_create)
            except Exception as exc:  # noqa: BLE001 - shadow work never stops the service
                log.warning("entry_shadow.pass_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            try:
                bg = await background(redis, session_factory, rpc, shadow, now)
                if bg:
                    log.info("entry_shadow.background", **{k: json.dumps(v, default=str) for k, v in bg.items()})
            except Exception as exc:  # noqa: BLE001
                log.warning("entry_shadow.background_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        else:
            await redis.hset(PASS_STATS, mapping={"paused_at": datetime.now(timezone.utc).isoformat(),
                                                  "paused_reason": "resource level CRITICAL"})
        wait = max(0.5, PASS_INTERVAL_SECONDS - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


async def signal_counts(session) -> dict:
    from yonixalpha_core.db.models import EntrySignal

    rows = (await session.execute(select(EntrySignal.strategy, func.count()).group_by(EntrySignal.strategy))).all()
    return dict(rows)
