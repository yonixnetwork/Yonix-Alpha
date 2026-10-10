"""Entry latency: one common timeline per token from its launch to a
confirmed entry, and where the time went.

Timeline fields (an unknown time stays None and says why; it is never
invented):

  launch_observed_at        on-chain creation time (CreateEvent block time)
  event_received_at         when our stream received the create event
  token_decoded_at          same handler, same moment: decoding happens on
                            receipt (recorded once, equal by construction)
  features_ready_at         first early-momentum feature computation
  first_candidate_at        first moment any entry strategy (shadow) said
                            CANDIDATE, or the funnel promoted the token
  signal_created_at         the gate candidate was created (funnel promote)
  first_gate_eval_at        first safety-gate evaluation finished
  risk_completed_at         the evaluation that approved the entry finished
  quote_requested_at /      LIVE: order picked up / venue resolved and
  quote_received_at         reserves read (the native builder quotes from
                            the reserves it just read)
  execution_queued_at       LIVE BUY order created (PAPER: the fill time)
  transaction_built_at, transaction_signed_at, transaction_submitted_at,
  transaction_landed_at (first seen), transaction_confirmed_at

Waiting is split by cause from the gate's own findings between the first
evaluation and the approving one:
  strategy   the entry signal or the trend checks were not satisfied
  data       a measurement was missing or too young (volatility, price,
             liquidity, decimals)
  risk       account and risk limits (positions, cooldown, size, costs)
  execution  route, impact or execution readiness
  safety     token, holder or creator safety findings
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, RiskAssessment, TokenObservation, TradingCandidate

TIMELINE_KEY = "yx:ee:tl:"  # hash per mint (Redis), first-write-wins (HSETNX)
TIMELINE_TTL = 3 * 86400

FIELDS = ("launch_observed_at", "event_received_at", "token_decoded_at", "features_ready_at", "first_candidate_at",
          "signal_created_at", "first_gate_eval_at", "risk_completed_at", "quote_requested_at", "quote_received_at",
          "execution_queued_at", "transaction_built_at", "transaction_signed_at", "transaction_submitted_at",
          "transaction_landed_at", "transaction_confirmed_at")

STRATEGY_CODES = {"SIGNAL_NOT_QUALIFIED", "ACTIVITY_DETERIORATING", "NO_RECENT_ACTIVITY", "ENTRY_DETERIORATION",
                  "EXIT_SIGNAL_AT_ENTRY", "LOW_ACTIVITY", "FEW_BUYERS", "LOW_VOLUME", "BUYERS_STOPPED", "ML_BELOW_MIN",
                  "NO_SIGNAL", "CUSTOM_RULE", "STRATEGY_OFF"}
DATA_CODES = {"PRICE_UNAVAILABLE", "VOLATILITY_UNAVAILABLE", "VOLATILITY_LOW_CONFIDENCE", "LIQUIDITY_UNKNOWN",
              "DATA_STALE", "STALE_DATA", "DECIMALS_UNKNOWN", "TAX_UNKNOWN", "BALANCE_UNAVAILABLE", "EQUITY_UNAVAILABLE",
              "EXPOSURE_UNAVAILABLE", "COSTS_UNDEFINED", "AUTO_SL_NO_VOLATILITY", "MAYHEM_FLAG_UNKNOWN",
              "CREATOR_HISTORY_UNKNOWN", "DAILY_PNL_UNAVAILABLE"}
RISK_CODES = {"MAX_OPEN_POSITIONS", "LOSS_COOLDOWN", "DAILY_LOSS_LIMIT", "SIZE_BELOW_MINIMUM", "FIXED_COSTS_EXCEED_RISK", "FIXED_COSTS_TOO_HIGH",
              "STOP_INSIDE_COSTS", "KILL_SWITCH", "TRADING_CONTROL_OFF", "INSUFFICIENT_GAS", "VOLATILITY_EXCEEDS_MAX_STOP",
              "MANUAL_SIZE_INVALID", "LOSS_UNDEFINED"}
EXECUTION_CODES = {"NO_BUY_ROUTE", "NO_SELL_ROUTE", "ROUTE_UNVERIFIED", "ENTRY_IMPACT", "EXIT_IMPACT", "ROUND_TRIP_LOSS",
                   "BOOK_TOO_THIN", "LIVE_NOT_READY", "LIVE_NOT_PERMITTED", "WAITING_FOR_LIQUIDITY", "INSUFFICIENT_LIQUIDITY",
                   "MIGRATION_PENDING"}


def wait_category(code: str) -> str:
    if code in STRATEGY_CODES or code.startswith("SIGNAL") or code.startswith("ACTIVITY_"):
        return "strategy"
    if code in DATA_CODES or code.endswith("_UNAVAILABLE") or code.endswith("_UNKNOWN"):
        return "data"
    if code in RISK_CODES:
        return "risk"
    if code in EXECUTION_CODES:
        return "execution"
    return "safety"


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _s(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(b - a, 3)


def blocking_codes(assessment: dict) -> list[str]:
    """The findings that decided a non-executable assessment."""
    decision = assessment.get("decision")
    codes = [f.get("code") for f in assessment.get("findings") or [] if f.get("action") == decision and f.get("code")]
    if not codes and not assessment.get("executable") and assessment.get("qualified") is False:
        codes = ["SIGNAL_NOT_QUALIFIED"]
    return codes


def waiting_split(evals: list[dict]) -> dict[str, Any]:
    """Time between consecutive evaluations before the approving one,
    attributed to the category of the codes that blocked each one.
    `evals`: [{"at": ts, "executable": bool, "codes": [...]}] in time order."""
    split = {"strategy": 0.0, "data": 0.0, "risk": 0.0, "execution": 0.0, "safety": 0.0}
    counts: dict[str, int] = {}
    for a, b in zip(evals, evals[1:]):
        if a["executable"]:
            break
        cats = {wait_category(c) for c in a["codes"]} or {"strategy"}
        for c in a["codes"]:
            counts[c] = counts.get(c, 0) + 1
        dt = max(0.0, b["at"] - a["at"])
        for cat in cats:
            split[cat] += dt / len(cats)
    return {"seconds": {k: round(v, 1) for k, v in split.items()},
            "blocking_codes": dict(sorted(counts.items(), key=lambda x: -x[1])[:10])}


def derive(t: dict[str, float | None]) -> dict[str, Any]:
    """Latencies (seconds) between the timeline points."""
    return {
        "detection_latency_s": _s(t.get("launch_observed_at"), t.get("event_received_at")),
        "feature_latency_s": _s(t.get("event_received_at"), t.get("features_ready_at")),
        "launch_to_first_candidate_s": _s(t.get("launch_observed_at"), t.get("first_candidate_at")),
        "promotion_latency_s": _s(t.get("event_received_at"), t.get("signal_created_at")),
        "signal_to_first_gate_eval_s": _s(t.get("signal_created_at"), t.get("first_gate_eval_at")),
        "gate_wait_s": _s(t.get("first_gate_eval_at"), t.get("risk_completed_at")),
        "risk_to_queue_s": _s(t.get("risk_completed_at"), t.get("execution_queued_at")),
        "queue_delay_s": _s(t.get("execution_queued_at"), t.get("quote_requested_at")),
        "quote_latency_s": _s(t.get("quote_requested_at"), t.get("quote_received_at")),
        "build_and_sign_s": _s(t.get("quote_received_at"), t.get("transaction_signed_at")),
        "submission_latency_s": _s(t.get("transaction_signed_at"), t.get("transaction_submitted_at")),
        "landing_latency_s": _s(t.get("transaction_submitted_at"), t.get("transaction_landed_at")),
        "confirmation_latency_s": _s(t.get("transaction_submitted_at"), t.get("transaction_confirmed_at")),
        "launch_to_entry_s": _s(t.get("launch_observed_at"),
                                t.get("transaction_confirmed_at") or t.get("execution_queued_at")),
    }


async def redis_points(redis, mint: str) -> dict[str, float | None]:
    raw = await redis.hgetall(TIMELINE_KEY + mint) if redis is not None else {}
    out: dict[str, float | None] = {}
    for k, v in (raw or {}).items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def mark(redis, mint: str, field: str, ts: float) -> None:
    """First time only (HSETNX): a timeline point never moves later."""
    if redis is None:
        return
    key = TIMELINE_KEY + mint
    await redis.hsetnx(key, field, f"{ts:.3f}")
    await redis.expire(key, TIMELINE_TTL)


async def candidate_timeline(session: AsyncSession, redis, candidate: TradingCandidate) -> dict[str, Any]:
    mint = (candidate.detail or {}).get("mint")
    t: dict[str, float | None] = {f: None for f in FIELDS}
    unknown: dict[str, str] = {}
    pts = await redis_points(redis, mint) if mint else {}
    t.update({k: v for k, v in pts.items() if k in t})
    obs = (await session.execute(select(TokenObservation.launched_at).where(TokenObservation.mint == mint))).scalar_one_or_none() \
        if mint else None
    if t["launch_observed_at"] is None and obs is not None:
        t["launch_observed_at"] = obs.timestamp()
    t["signal_created_at"] = candidate.created_at.timestamp() if candidate.created_at else None
    rows = (await session.execute(
        select(RiskAssessment.evaluated_at, RiskAssessment.executable, RiskAssessment.assessment, RiskAssessment.id)
        .where(RiskAssessment.candidate_id == candidate.id).order_by(RiskAssessment.evaluated_at).limit(400))).all()
    evals = []
    approved_id = None
    for at, ex, a, aid in rows:
        evals.append({"at": at.timestamp(), "executable": bool(ex), "codes": [] if ex else blocking_codes(a or {})})
        if ex and approved_id is None:
            approved_id = aid
            t["risk_completed_at"] = at.timestamp()
    if evals:
        t["first_gate_eval_at"] = evals[0]["at"]
    pos = None
    if approved_id is not None:
        pos = (await session.execute(select(PaperPosition).where(PaperPosition.assessment_id == approved_id))).scalars().first()
        order = (await session.execute(select(ExecutionOrder).where(
            ExecutionOrder.assessment_id == approved_id, ExecutionOrder.side == "BUY").order_by(ExecutionOrder.created_at))).scalars().first()
        if order is not None:
            ts = ((order.diagnostics or {}).get("timing") or {}).get("timestamps") or {}
            t["execution_queued_at"] = order.created_at.timestamp()
            t["quote_requested_at"] = _ts(ts.get("worker_pickup_at"))
            t["quote_received_at"] = _ts(ts.get("quote_at"))
            t["transaction_built_at"] = _ts(ts.get("transaction_built_at"))
            t["transaction_signed_at"] = _ts(ts.get("signed_at"))
            t["transaction_submitted_at"] = _ts(ts.get("submitted_at")) or (order.submitted_at.timestamp() if order.submitted_at else None)
            t["transaction_landed_at"] = _ts(ts.get("first_seen_at"))
            t["transaction_confirmed_at"] = _ts(ts.get("confirmed_at")) or (order.confirmed_at.timestamp() if order.confirmed_at else None)
        elif pos is not None:
            t["execution_queued_at"] = pos.entry_at.timestamp() if pos.entry_at else None
            unknown["transaction_*"] = "PAPER entry: filled at the decision, no transaction"
    if t["event_received_at"] is None:
        unknown["event_received_at"] = "not recorded (tokens seen before this upgrade, or the record expired)"
    else:
        t["token_decoded_at"] = t["event_received_at"]
    if t["launch_observed_at"] is None:
        unknown["launch_observed_at"] = "on-chain creation time not held"
    first_cand = [v for k, v in pts.items() if k.startswith("first_candidate_at") and v]
    if first_cand:
        t["first_candidate_at"] = min(first_cand)
    elif t["signal_created_at"]:
        t["first_candidate_at"] = t["signal_created_at"]
    out = {"mint": mint, "candidate_id": str(candidate.id), "engine": candidate.engine, "state": candidate.state,
           "timeline": {k: _iso(v) for k, v in t.items()}, "latency": derive(t), "waiting": waiting_split(evals),
           "evaluations": len(evals), "unknown": unknown,
           "entered": pos is not None, "mode": pos.execution_mode if pos is not None else None,
           "position_id": str(pos.id) if pos is not None else None}
    if pos is not None:
        dec = (pos.plan or {}).get("decision") or {}
        out["decision_price"] = dec.get("price_sol")
        out["entry_price"] = str(pos.entry_price) if pos.entry_price is not None else None
    return out


def _q(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 3)


async def latency_summary(session: AsyncSession, redis, since: datetime, limit: int = 150,
                          entered_only: bool = False) -> dict[str, Any]:
    """Median / p90 of every latency over recent pump-stream candidates."""
    q = select(TradingCandidate).where(TradingCandidate.created_at >= since,
                                       TradingCandidate.detail["source"].astext == "pump_stream")
    cands = (await session.execute(q.order_by(TradingCandidate.created_at.desc()).limit(limit))).scalars().all()
    rows = [await candidate_timeline(session, redis, c) for c in cands]
    if entered_only:
        rows = [r for r in rows if r["entered"]]
    keys = list(derive({}).keys())
    stats: dict[str, Any] = {}
    for k in keys:
        vals = [r["latency"][k] for r in rows if r["latency"].get(k) is not None]
        stats[k] = {"n": len(vals), "median": _q(vals, 0.5), "p90": _q(vals, 0.9)}
    wait: dict[str, list[float]] = {}
    codes: dict[str, int] = {}
    for r in rows:
        for k, v in r["waiting"]["seconds"].items():
            wait.setdefault(k, []).append(v)
        for c, n in r["waiting"]["blocking_codes"].items():
            codes[c] = codes.get(c, 0) + n
    return {"since": since.isoformat(), "candidates": len(rows), "entered": sum(1 for r in rows if r["entered"]),
            "latency_seconds": stats,
            "waiting_seconds_median": {k: _q(v, 0.5) for k, v in wait.items()},
            "waiting_seconds_total": {k: round(sum(v), 1) for k, v in wait.items()},
            "top_blocking_codes": dict(sorted(codes.items(), key=lambda x: -x[1])[:15]),
            "examples": rows[:20]}


def window_since(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def summarize_values(vals: list[float]) -> dict[str, Any]:
    return {"n": len(vals), "median": round(statistics.median(vals), 3) if vals else None, "p90": _q(vals, 0.9)}


# --- late entries ----------------------------------------------------------------------------

def _raw_at(trades: list, at: float | None) -> float | None:
    if at is None:
        return None
    best = None
    for t in trades:
        if t.at.timestamp() <= at:
            best = t
        else:
            break
    return best.virtual_sol / best.virtual_token if best is not None and best.virtual_token else None


def _chg(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else round((a / b - 1) * 100, 2)


async def late_entries(session: AsyncSession, redis, since: datetime, limit: int = 40) -> dict[str, Any]:
    """For entered pump-stream tokens: first detection, first qualified
    signal (any entry strategy, shadow), actual entry, the price change
    between them (from the held stream; UNKNOWN once it expired) and whether
    the token was already decelerating at the entry (the gate's own entry
    quality and trend findings)."""
    from yonixalpha_core.db.models import EntrySignal
    from yonixalpha_core.solana import pump_stream

    positions = (await session.execute(select(PaperPosition).where(
        PaperPosition.entry_at >= since, PaperPosition.engine.in_(("solana_fresh", "solana_momentum", "solana_migration")),
        PaperPosition.status != "pending_entry").order_by(PaperPosition.entry_at.desc()).limit(limit))).scalars().all()
    out = []
    for p in positions:
        sig = (await session.execute(select(EntrySignal.strategy, EntrySignal.decided_at, EntrySignal.price_raw)
                                     .where(EntrySignal.mint == p.asset_id).order_by(EntrySignal.decided_at))).all()
        first_signal = next(((s, at, pr) for s, at, pr in sig if s in ("EARLY_ACCELERATION", "MOMENTUM_CONTINUATION",
                                                                        "SMART_WALLET_CONFIRMATION")), None)
        promote = next(((s, at, pr) for s, at, pr in sig if s == "CURRENT_PROMOTE"), None)
        pts = await redis_points(redis, p.asset_id) if redis is not None else {}
        detected = pts.get("event_received_at") or (promote[1].timestamp() if promote else None)
        trades = sorted(await pump_stream.load_trades(redis, p.asset_id), key=lambda t: t.at) if redis is not None else []
        entry_ts = p.entry_at.timestamp() if p.entry_at else None
        a = (await session.execute(select(RiskAssessment.assessment).where(RiskAssessment.id == p.assessment_id))).scalar_one_or_none() \
            if p.assessment_id else None
        snap = (a or {}).get("inputs_snapshot") or {}
        eq = snap.get("entry_quality") or {}
        trend = (snap.get("observation") or {}).get("trend")
        p_detect = _raw_at(trades, detected) if trades else (float(promote[2]) if promote and promote[2] is not None else None)
        p_signal = _raw_at(trades, first_signal[1].timestamp()) if trades and first_signal else \
            (float(first_signal[2]) if first_signal and first_signal[2] is not None else None)
        p_entry = _raw_at(trades, entry_ts) if trades else None
        source = "stream" if trades else None
        if not trades:
            # History from before this upgrade / expired stream: the
            # observation window's last checkpoint (promotion) and the
            # decision's planned price, both recorded in the database.
            obs = (await session.execute(select(TokenObservation.report, TokenObservation.launched_at)
                                         .where(TokenObservation.mint == p.asset_id))).first()
            cps = ((obs[0] if obs else None) or {}).get("checkpoints") or []
            if p_detect is None and cps and cps[-1].get("price_raw"):
                p_detect = float(cps[-1]["price_raw"])
                detected = detected or _ts(cps[-1].get("at"))
            plan = (a or {}).get("plan") or {}
            dec = (((p.plan or {}).get("venue") or {}).get("decimals"))
            if plan.get("entry_price") and dec is not None:
                p_entry = float(plan["entry_price"]) * 1e9 / 10 ** int(dec)
            if obs and obs[1] is not None and not pts.get("launch_observed_at"):
                pts["launch_observed_at"] = obs[1].timestamp()
            source = "database (observation checkpoint, decision price)" if (p_detect or p_entry) else None
        row = {"position_id": str(p.id), "mint": p.asset_id, "symbol": p.symbol, "mode": p.execution_mode,
               "engine": p.engine, "launch_at": _iso(pts.get("launch_observed_at")), "first_detection_at": _iso(detected),
               "first_signal_at": first_signal[1].isoformat() if first_signal else None,
               "first_signal_strategy": first_signal[0] if first_signal else None,
               "entry_at": p.entry_at.isoformat() if p.entry_at else None,
               "seconds_detection_to_entry": _s(detected, entry_ts),
               "seconds_signal_to_entry": _s(first_signal[1].timestamp(), entry_ts) if first_signal else None,
               "price_change_detection_to_entry_pct": _chg(p_entry, p_detect),
               "price_change_signal_to_entry_pct": _chg(p_entry, p_signal),
               "decelerating_at_entry": bool(eq.get("indicators")) or trend == "DETERIORATING",
               "deceleration_evidence": (eq.get("evidence") or []) + ([f"trend {trend}"] if trend else []),
               "prices_from": source,
               "realized_pnl_pct": round(float(p.realized_pnl_pct) * 100, 2) if p.realized_pnl_pct is not None else None}
        if not trades and source is None:
            row["unknown"] = "stream trades expired (kept 3 h) and no recorded prices: the price change is unknown"
        out.append(row)
    late = [r for r in out if (r.get("price_change_detection_to_entry_pct") or 0) >= 50]
    return {"since": since.isoformat(), "entries": len(out), "late_by_50pct_or_more": len(late), "rows": out}
