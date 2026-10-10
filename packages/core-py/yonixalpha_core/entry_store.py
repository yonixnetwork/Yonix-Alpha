"""Storage and orchestration for early-entry intelligence (SHADOW).

  settings        platform setting `entry_intelligence`: the strategy
                  thresholds (entry_intel.EntryConfig) and one mode per
                  strategy: SHADOW (record only, default), PAPER (a CANDIDATE
                  also creates a gate candidate whose entry can only be a
                  PAPER trade; the safety gate still decides), PAUSED.
  record_signal   the first CANDIDATE per (mint, strategy), baselines and
                  migrated variants -> entry_signals
  wallet_evidence smart-wallet quality of a token's buyers, from launch
                  outcomes resolved BEFORE the decision (launch_buyers)
  label_due       outcomes of signals older than the label horizon
  migrated pool   PumpSwap reserve samples for recently migrated tokens
  samples         (one getMultipleAccounts per pass for all of them)

Nothing here can place an order. LIVE use of these strategies is not
available in this release.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_outcomes as eo
from yonixalpha_core.db.models import EntrySignal, ExecutionOrder, LaunchBuyer, PaperPosition, PlatformSetting, TradingCandidate
from yonixalpha_core.solana import pump_stream, pumpswap
from yonixalpha_core.solana.launch_features import coverage

SETTINGS_KEY = "entry_intelligence"
MODES = ("SHADOW", "PAPER", "PAUSED")
STATE_KEY = "yx:ee:state:"  # per-mint JSON: features summary, phase, strategy decisions (dashboard)
STATE_TTL = 3600
STATE_INDEX = "yx:ee:active"  # zset mint -> last evaluated ts
RECORDED_KEY = "yx:ee:rec:"  # SET NX per (strategy, mint): one record each
WALLET_CACHE = "yx:ee:wq:"
WALLET_CACHE_TTL = 600
LATENCY_CACHE = "yx:ee:latency"
MIG_SAMPLES = "yx:ee:mig:"
MIG_VAULTS = "yx:ee:vaults:"
MIG_DONE = "yx:ee:migdone:"
MIG_TRACK_SECONDS = 30 * 60
MIG_TTL = 3 * 3600
WALLET_HISTORY_DAYS = 30
MIN_CLOSED_FOR_PROVEN = 5
LAMPORTS = Decimal(1_000_000_000)


# --- settings -------------------------------------------------------------------------------

def parse_settings(value: dict | None) -> dict[str, Any]:
    value = value or {}
    modes = {s: "SHADOW" for s in ei.STRATEGIES + ei.MIGRATED_VARIANTS}
    for k, v in (value.get("modes") or {}).items():
        if k in modes and v in MODES:
            modes[k] = v
    return {"config": ei.EntryConfig.from_dict(value.get("config")), "modes": modes,
            "enabled": bool(value.get("enabled", True))}


async def load_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    return parse_settings(row.value if row else None)


def validate_update(body: dict) -> list[str]:
    errors: list[str] = []
    for k, v in (body.get("modes") or {}).items():
        if k not in ei.STRATEGIES + ei.MIGRATED_VARIANTS:
            errors.append(f"unknown strategy {k}")
        elif v not in MODES:
            errors.append(f"mode of {k} must be one of {MODES}")
        elif v == "PAPER" and k == ei.SMART_WALLET_CONFIRMATION:
            errors.append("SMART_WALLET_CONFIRMATION confirms other strategies and cannot open paper trades by itself")
        elif v == "PAPER" and k in ei.MIGRATED_VARIANTS:
            errors.append("migrated variants are measured in SHADOW only in this release")
    if "config" in body:
        errors += ei.validate_config(body.get("config") or {})
    return errors


# --- latency used by the labeller ----------------------------------------------------------

async def measured_latency(session: AsyncSession, redis) -> tuple[float, str]:
    """Median decision -> confirmation of confirmed LIVE buys (newest 200),
    cached 10 minutes. The default when none exists is stated."""
    if redis is not None:
        cached = await redis.get(LATENCY_CACHE)
        if cached:
            v, src = cached.split("|", 1)
            return float(v), src
    rows = (await session.execute(
        select(ExecutionOrder.diagnostics).where(ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "BUY",
                                                 ExecutionOrder.status == "CONFIRMED")
        .order_by(ExecutionOrder.created_at.desc()).limit(200))).scalars().all()
    vals = [float(ms) / 1000 for d in rows if (ms := ((d or {}).get("timing") or {}).get("decision_to_confirm_ms")) is not None]
    if len(vals) >= 5:
        out = (round(statistics.median(vals), 2), f"MEASURED: median of {len(vals)} confirmed LIVE buys")
    else:
        out = (eo.DEFAULT_LATENCY_SECONDS, f"DEFAULT: {len(vals)} measured LIVE buys (need 5)")
    if redis is not None:
        await redis.set(LATENCY_CACHE, f"{out[0]}|{out[1]}", ex=600)
    return out


# --- recording ---------------------------------------------------------------------------------

async def claim(redis, strategy: str, mint: str) -> bool:
    """One record per (strategy, mint), across processes and restarts (the
    unique constraint is the final guard)."""
    if redis is None:
        return True
    return bool(await redis.set(f"{RECORDED_KEY}{strategy}:{mint}", "1", nx=True, ex=3 * 86400))


async def record_signal(session: AsyncSession, *, mint: str, strategy: str, lifecycle: str, decision: str,
                        decided_at: datetime, phase: str | None = None, score: float | None = None,
                        evidence_level: str | None = None, size_factor: float | None = None,
                        launch_at: datetime | None = None, price_raw: float | None = None,
                        features: dict | None = None, reasons: list | None = None, evidence: dict | None = None,
                        candidate_id=None, ml_probability: float | None = None) -> bool:
    res = await session.execute(insert(EntrySignal).values(
        mint=mint, strategy=strategy, lifecycle=lifecycle, decision=decision, phase=phase,
        score=None if score is None else Decimal(str(round(score, 4))),
        evidence_level=evidence_level, size_factor=None if size_factor is None else Decimal(str(size_factor)),
        ml_probability=None if ml_probability is None else Decimal(str(round(ml_probability, 6))),
        decided_at=decided_at, launch_at=launch_at,
        price_raw=None if price_raw is None else Decimal(repr(float(price_raw))),
        features=json.loads(json.dumps(features, default=str)) if features is not None else None,
        reasons=reasons, evidence=json.loads(json.dumps(evidence, default=str)) if evidence is not None else None,
        candidate_id=candidate_id,
    ).on_conflict_do_nothing(constraint="uq_entry_signals_mint_strategy"))
    return bool(res.rowcount)


async def record_baseline(session: AsyncSession, redis, *, mint: str, strategy: str, decided_at: datetime,
                          price_raw: float | None, launch_at: datetime | None = None,
                          detail: dict | None = None) -> bool:
    """CURRENT_PROMOTE / CURRENT_GATE_ENTRY / NAIVE_SAMPLE: the existing
    pipeline's moments, labelled by the same labeller."""
    if not await claim(redis, strategy, mint):
        return False
    return await record_signal(session, mint=mint, strategy=strategy, lifecycle="FRESH", decision="BASELINE",
                               decided_at=decided_at, launch_at=launch_at, price_raw=price_raw,
                               features=detail or {}, reasons=None)


# --- smart-wallet evidence ----------------------------------------------------------------------

def _wallet_stats(rows: list[Any], fee_bps: int, t: datetime) -> dict[str, Any]:
    """Realized results of one wallet's resolved launch buys (FIFO on a
    single lot per launch: cost of the tokens sold = its SOL in times the
    sold share), net of an estimated pump.fun fee on both legs."""
    fee = fee_bps / 10_000
    closed = []
    arrivals = []
    for r in rows:
        led = r.ledger or {}
        if r.launch_created_at is not None:
            arrivals.append((r.first_buy_at - r.launch_created_at).total_seconds())
        if not led.get("covered"):
            continue
        tok_in = float(r.tokens_in) + float(led.get("tokens_in_later") or 0)
        sol_in = float(r.sol_in) + float(led.get("sol_in_later") or 0)
        tok_out = float(led.get("tokens_out") or 0)
        sol_out = float(led.get("sol_out") or 0)
        if tok_in <= 0 or sol_in <= 0 or tok_out < 0.9 * tok_in:
            continue  # still holding most of it: not a closed trade
        cost = sol_in * min(1.0, tok_out / tok_in) * (1 + fee)
        pnl = sol_out * (1 - fee) - cost
        closed.append({"at": r.outcome_resolved_at, "pnl_sol": pnl, "ret_pct": pnl / cost * 100 if cost else 0.0,
                       "outcome": r.outcome})
    closed.sort(key=lambda x: x["at"])
    out: dict[str, Any] = {"resolved_launches": len(rows), "closed_trades": len(closed),
                           "median_arrival_seconds": round(statistics.median(arrivals), 1) if arrivals else None,
                           "fee_bps_assumed": fee_bps}
    if not closed:
        return out
    rets = [c["ret_pct"] for c in closed]
    wins = [c["pnl_sol"] for c in closed if c["pnl_sol"] > 0]
    losses = [c["pnl_sol"] for c in closed if c["pnl_sol"] <= 0]
    eq = peak = mdd = 0.0
    for c in closed:
        eq += c["pnl_sol"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    total_win = sum(wins)
    windows = {}
    for name, days in (("24h", 1), ("7d", 7), ("30d", 30)):
        sub = [c["ret_pct"] for c in closed if c["at"] >= t - timedelta(days=days)]
        windows[name] = {"n": len(sub), "median_return_pct": round(statistics.median(sub), 2) if sub else None}
    out.update({
        "win_rate": round(len(wins) / len(closed), 4),
        "median_return_pct": round(statistics.median(rets), 2),
        "realized_pnl_sol": round(sum(c["pnl_sol"] for c in closed), 6),
        "profit_factor": round(total_win / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "max_drawdown_sol": round(mdd, 6),
        "outlier_dependence": round(max(wins) / total_win, 4) if wins and total_win > 0 else None,
        "windows": windows,
        "before_move_rate": round(sum(1 for c in closed if c["outcome"] == "WIN") / len(closed), 4),
    })
    return out


def is_proven(s: dict[str, Any]) -> bool:
    pf = s.get("profit_factor")
    return (s.get("closed_trades", 0) >= MIN_CLOSED_FOR_PROVEN and (s.get("median_return_pct") or 0) > 0
            and (pf is None or pf > 1.2) and (s.get("outlier_dependence") is None or s["outlier_dependence"] < 0.6))


async def wallet_evidence(session: AsyncSession, redis, trades: list, t: datetime, *, fee_bps: int = 125,
                          max_wallets: int = 60) -> dict[str, Any]:
    """Which of this token's buyers (up to `t`) have a proven record in
    launches resolved before `t`, when they entered, and whether they sold."""
    held = sorted((x for x in trades if x.at <= t), key=lambda x: x.at)
    first_buy: dict[str, datetime] = {}
    bought: dict[str, int] = {}
    sold: dict[str, int] = {}
    for x in held:
        if x.is_buy:
            first_buy.setdefault(x.trader, x.at)
            bought[x.trader] = bought.get(x.trader, 0) + x.token_raw
        else:
            sold[x.trader] = sold.get(x.trader, 0) + x.token_raw
    wallets = list(first_buy)[:max_wallets]
    if not wallets:
        return {"status": "UNKNOWN", "reason": "no buyers yet"}
    stats: dict[str, dict] = {}
    missing = []
    if redis is not None:
        cached = await redis.mget([WALLET_CACHE + w for w in wallets])
        for w, c in zip(wallets, cached):
            if c:
                stats[w] = json.loads(c)
            else:
                missing.append(w)
    else:
        missing = wallets
    if missing:
        rows = (await session.execute(select(LaunchBuyer).where(
            LaunchBuyer.wallet.in_(missing), LaunchBuyer.outcome_resolved_at.is_not(None),
            LaunchBuyer.outcome_resolved_at < t,
            LaunchBuyer.outcome_resolved_at >= t - timedelta(days=WALLET_HISTORY_DAYS)).limit(5000))).scalars().all()
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r.wallet, []).append(r)
        pipe = redis.pipeline() if redis is not None else None
        for w in missing:
            stats[w] = _wallet_stats(by.get(w, []), fee_bps, t)
            if pipe is not None:
                pipe.set(WALLET_CACHE + w, json.dumps(stats[w], default=str), ex=WALLET_CACHE_TTL)
        if pipe is not None:
            await pipe.execute()
    with_history = [w for w in wallets if stats[w].get("closed_trades", 0) > 0]
    if not with_history:
        return {"status": "UNKNOWN", "evaluated_wallets": len(wallets),
                "reason": "none of the buyers has a closed launch trade in this system's history"}
    proven = [w for w in wallets if is_proven(stats[w])]
    entries = [{"wallet": w, "first_buy_at": first_buy[w].isoformat(),
                "seconds_ago": round((t - first_buy[w]).total_seconds(), 1), "stats": stats[w]} for w in proven]
    exits = [{"wallet": w} for w in proven if bought.get(w) and sold.get(w, 0) >= 0.5 * bought[w]]
    coordinated = False
    if redis is not None and len(proven) >= 2:
        from yonixalpha_core.wallet_intel import MATES

        for w in proven:
            mates = await redis.zrangebyscore(MATES + w, 2, "+inf")
            if any((m.decode() if isinstance(m, bytes) else m) in proven for m in mates):
                coordinated = True
                break
    return {"status": "MEASURED", "evaluated_wallets": len(wallets), "wallets_with_history": len(with_history),
            "proven_wallets": len(proven), "proven_entries": entries, "proven_exits": exits, "coordinated": coordinated,
            "as_of": t.isoformat(), "rule": f">= {MIN_CLOSED_FOR_PROVEN} closed trades, median return > 0, profit factor "
                                         "> 1.2, best trade < 60% of profit, launches resolved before the decision only",
            "regime_note": "performance by market regime: NOT RECORDED per wallet in this release"}


# --- labelling ---------------------------------------------------------------------------------------

async def label_due(session: AsyncSession, redis, now: datetime, limit: int = 200) -> dict[str, int]:
    """Labels signals whose horizon has passed. Signals whose stream data
    expired are labelled UNKNOWN with the reason (never guessed)."""
    due = (await session.execute(select(EntrySignal).where(
        EntrySignal.outcome_at.is_(None),
        EntrySignal.decided_at <= now - timedelta(seconds=eo.LABEL_HORIZON_SECONDS)).order_by(EntrySignal.decided_at)
        .limit(limit))).scalars().all()
    if not due:
        return {"labelled": 0}
    latency, lat_src = await measured_latency(session, redis)
    counts = {"labelled": 0, "unknown": 0}
    trades_cache: dict[str, list] = {}
    for row in due:
        try:
            if row.lifecycle == "MIGRATED":
                samples = await load_mig_samples(redis, row.mint)
                pool = pumpswap.canonical_pool(row.mint)
                fee_raw = await redis.get(pumpswap.POOL_FEE_KEY + pool) if redis is not None else None
                if not samples and row.strategy != ei.MIGRATED_NO_TRADE:
                    out = {"version": eo.LABEL_VERSION, "unknown": "pool samples expired or never taken"}
                else:
                    out = eo.label_migrated(samples, row.decided_at.timestamp(), now.timestamp(), latency_s=latency,
                                            latency_source=lat_src, fee_bps=int(fee_raw) if fee_raw else None,
                                            strategy=row.strategy)
            else:
                if row.mint not in trades_cache:
                    trades_cache[row.mint] = await pump_stream.load_trades(redis, row.mint)
                trades = trades_cache[row.mint]
                curve = await pump_stream.load_curve(redis, row.mint)
                meta = await pump_stream.load_meta(redis, row.mint) or {}
                if not trades:
                    out = {"version": eo.LABEL_VERSION, "unknown": "stream trades for this token expired (kept 3 h)"}
                else:
                    out = eo.label_fresh(trades, row.decided_at, now, latency_s=latency, latency_source=lat_src,
                                         fee_bps=curve.fee_bps if curve else None,
                                         mayhem=bool(int(meta.get("is_mayhem_mode") or 0)) if meta else None,
                                         migrated_at=curve.migrated_at if curve else None, phase_at_decision=row.phase)
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the batch
            out = {"version": eo.LABEL_VERSION, "unknown": f"labelling failed: {type(exc).__name__}: {str(exc)[:160]}"}
        row.outcome = json.loads(json.dumps(out, default=str))
        row.outcome_at = now
        row.label_version = eo.LABEL_VERSION
        counts["labelled"] += 1
        counts["unknown"] += "unknown" in out
    await session.commit()
    return counts


# --- migrated tokens ---------------------------------------------------------------------------------

async def load_mig_samples(redis, mint: str) -> list[tuple[float, int, int]]:
    if redis is None:
        return []
    out = []
    for raw in await redis.lrange(MIG_SAMPLES + mint, 0, -1):
        try:
            ts, b, q = json.loads(raw)
            out.append((float(ts), int(b), int(q)))
        except (ValueError, TypeError):
            continue
    return out


async def sample_migrated(redis, rpc, now: datetime) -> dict[str, Any]:
    """One reserve sample per recently migrated token: a getAccountInfo for
    each new pool (once, cached) and ONE getMultipleAccounts for every vault
    of every tracked pool. Background RPC priority: shed when the providers
    are limited."""
    since = int(now.timestamp()) - MIG_TRACK_SECONDS
    tracked = await pump_stream.migrated_since(redis, since)
    if not tracked:
        return {"tracked": 0, "sampled": 0}
    vaults: dict[str, tuple[str, str, int]] = {}
    for mint, _ in tracked[:60]:
        raw = await redis.get(MIG_VAULTS + mint)
        if raw:
            bv, qv, vq = json.loads(raw)
            vaults[mint] = (bv, qv, int(vq))
            continue
        try:
            info = await rpc.call("getAccountInfo", [pumpswap.canonical_pool(mint), {"encoding": "base64", "commitment": "confirmed"}],
                                  priority="background")
            value = (info or {}).get("value")
            if not value or value.get("owner") != pumpswap.PUMP_AMM_PROGRAM:
                continue
            import base64

            acct = pumpswap.decode_pool(base64.b64decode(value["data"][0]))
            vaults[mint] = (acct.base_vault, acct.quote_vault, int(acct.virtual_quote_reserves))
            await redis.set(MIG_VAULTS + mint, json.dumps(list(vaults[mint])), ex=MIG_TTL)
        except Exception:  # noqa: BLE001 - shed or unreadable: try again next pass
            continue
    if not vaults:
        return {"tracked": len(tracked), "sampled": 0}
    keys = []
    for bv, qv, _ in vaults.values():
        keys += [bv, qv]
    try:
        res = await rpc.call("getMultipleAccounts", [keys, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                             priority="background")
    except Exception as exc:  # noqa: BLE001
        return {"tracked": len(tracked), "sampled": 0, "error": f"{type(exc).__name__}"}
    vals = (res or {}).get("value") or []
    pipe = redis.pipeline()
    sampled = 0
    for i, (mint, (_, _, vq)) in enumerate(vaults.items()):
        try:
            base = int(vals[2 * i]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            quote = int(vals[2 * i + 1]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) + vq
        except (IndexError, KeyError, TypeError):
            continue
        if base <= 0 or quote <= 0:
            continue
        pipe.rpush(MIG_SAMPLES + mint, json.dumps([round(now.timestamp(), 3), base, quote]))
        pipe.ltrim(MIG_SAMPLES + mint, -240, -1)
        pipe.expire(MIG_SAMPLES + mint, MIG_TTL)
        sampled += 1
    await pipe.execute()
    return {"tracked": len(tracked), "sampled": sampled}


async def record_migrated_variants(session: AsyncSession, redis, now: datetime, modes: dict[str, str]) -> int:
    """First trigger of each migrated variant, from the reserve samples."""
    since = int(now.timestamp()) - MIG_TRACK_SECONDS
    n = 0
    for mint, mig_ts in await pump_stream.migrated_since(redis, since):
        samples = await load_mig_samples(redis, mint)
        if not samples:
            continue
        done = {d.decode() if isinstance(d, bytes) else d for d in await redis.smembers(MIG_DONE + mint)}
        triggers = eo.migrated_triggers(samples, float(mig_ts), now.timestamp(), done)
        for variant, ts in triggers.items():
            await redis.sadd(MIG_DONE + mint, variant)
            await redis.expire(MIG_DONE + mint, MIG_TTL)
            if modes.get(variant) == "PAUSED":
                continue
            s = next((x for x in samples if x[0] == ts), None)
            price = s[2] / s[1] if s and s[1] else None
            if await record_signal(session, mint=mint, strategy=variant, lifecycle="MIGRATED",
                                   decision="BASELINE" if variant == ei.MIGRATED_NO_TRADE else ei.CANDIDATE,
                                   decided_at=datetime.fromtimestamp(ts, tz=timezone.utc), price_raw=price,
                                   features={"migrated_at": datetime.fromtimestamp(mig_ts, tz=timezone.utc).isoformat(),
                                             "seconds_after_migration": round(ts - mig_ts, 1),
                                             "samples_until_trigger": sum(1 for x in samples if x[0] <= ts),
                                             "note": "price and liquidity from pool reserves; BOOST buybacks also add SOL"},
                                   reasons=[variant]):
                n += 1
    if n:
        await session.commit()
    return n


# --- reads for the dashboard / evaluation -------------------------------------------------------------

def row_dict(r: EntrySignal) -> dict[str, Any]:
    return {"id": str(r.id), "mint": r.mint, "strategy": r.strategy, "lifecycle": r.lifecycle, "decision": r.decision,
            "phase": r.phase, "score": float(r.score) if r.score is not None else None,
            "evidence_level": r.evidence_level, "size_factor": float(r.size_factor) if r.size_factor is not None else None,
            "ml_probability": float(r.ml_probability) if r.ml_probability is not None else None,
            "decided_at": r.decided_at, "launch_at": r.launch_at.isoformat() if r.launch_at else None,
            "price_raw": float(r.price_raw) if r.price_raw is not None else None, "reasons": r.reasons,
            "features": r.features, "evidence": r.evidence, "candidate_id": str(r.candidate_id) if r.candidate_id else None,
            "outcome": r.outcome, "outcome_at": r.outcome_at.isoformat() if r.outcome_at else None}


async def labelled_rows(session: AsyncSession, since: datetime, limit: int = 20000) -> list[dict[str, Any]]:
    rows = (await session.execute(select(
        EntrySignal.strategy, EntrySignal.decided_at, EntrySignal.score, EntrySignal.ml_probability, EntrySignal.outcome)
        .where(EntrySignal.outcome_at.is_not(None), EntrySignal.decided_at >= since)
        .order_by(EntrySignal.decided_at).limit(limit))).all()
    return [{"strategy": s, "decided_at": d, "score": float(sc) if sc is not None else None,
             "ml_probability": float(p) if p is not None else None, "outcome": o} for s, d, sc, p, o in rows]


async def counts(session: AsyncSession, since: datetime) -> dict[str, Any]:
    rows = (await session.execute(select(EntrySignal.strategy, func.count(), func.count(EntrySignal.outcome_at))
                                  .where(EntrySignal.decided_at >= since).group_by(EntrySignal.strategy))).all()
    return {s: {"signals": n, "labelled": lab} for s, n, lab in rows}


async def paper_results(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Closed paper trades opened from a strategy in PAPER mode."""
    rows = (await session.execute(select(TradingCandidate.detail["entry_strategy"].astext, PaperPosition.realized_pnl_pct)
                                  .join(PaperPosition, PaperPosition.candidate_id == TradingCandidate.id)
                                  .where(TradingCandidate.detail["entry_strategy"].astext.is_not(None),
                                         and_(PaperPosition.status == "closed", PaperPosition.execution_mode == "PAPER")))).all()
    out: dict[str, dict[str, Any]] = {}
    for strat, pct in rows:
        if pct is None:
            continue
        out.setdefault(strat, {"returns": []})["returns"].append(float(pct) * 100)
    for v in out.values():
        r = v.pop("returns")
        v.update({"closed": len(r), "median_pnl_pct": round(statistics.median(r), 3) if r else None,
                  "win_rate": round(sum(1 for x in r if x > 0) / len(r), 4) if r else None})
    return out


async def write_state(redis, mint: str, now: datetime, state: dict[str, Any]) -> None:
    pipe = redis.pipeline()
    pipe.set(STATE_KEY + mint, json.dumps(state, default=str), ex=STATE_TTL)
    pipe.zadd(STATE_INDEX, {mint: now.timestamp()})
    pipe.zremrangebyscore(STATE_INDEX, "-inf", now.timestamp() - STATE_TTL)
    await pipe.execute()


async def read_state(redis, mint: str) -> dict[str, Any] | None:
    raw = await redis.get(STATE_KEY + mint)
    return json.loads(raw) if raw else None


async def active_states(redis, limit: int = 60) -> list[dict[str, Any]]:
    mints = await redis.zrevrange(STATE_INDEX, 0, limit - 1)
    if not mints:
        return []
    raws = await redis.mget([STATE_KEY + (m.decode() if isinstance(m, bytes) else m) for m in mints])
    return [json.loads(r) for r in raws if r]


def complete_history(trades: list, meta: dict, stream_started_ts: int | None) -> bool | None:
    created_ts = int(meta["created_at"]) if meta.get("created_at") else None
    return coverage(trades, created_ts, stream_started_ts)["complete"]
