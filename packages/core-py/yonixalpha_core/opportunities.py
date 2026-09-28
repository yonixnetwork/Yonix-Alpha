"""Outcomes of every opportunity, traded or not, for learning and review.

One row per decision episode (opportunity_outcomes):

  OBSERVATION  a fresh token the observation funnel rejected or let expire
  GATE         a candidate the safety gate finally rejected, let expire, or
               entered (traded=True, linked to its position)

Each row stores the state the decision saw (price, market cap, liquidity,
flow, volatility and its confidence, risk, signal, ML score, deterioration
signs, data freshness) and then, from the recorded trade stream, what the
price did at T+5s, 10s, 30s, 60s, 5m, 15m and 30m, the peak and the drawdown
within 30 minutes, and whether the token migrated. Traded rows also get the
trade result (PnL, MFE/MAE, exit reason, execution quality) and, for a
loss, a LOSS_ANALYSIS with its classification.

Prices are decimals-free (lamports per raw token unit), so changes need no
token metadata. A horizon that cannot be priced says why; nothing is
interpolated. Nothing here is read by the gate: ML learns from it only
through the existing dataset → training → validation → challenger → shadow
→ controlled-promotion process.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ExecutionOrder, OpportunityOutcome, TokenObservation
from yonixalpha_core.solana import pump_stream

HORIZONS: tuple[tuple[str, int], ...] = (("T+5s", 5), ("T+10s", 10), ("T+30s", 30), ("T+60s", 60),
                                         ("T+5m", 300), ("T+15m", 900), ("T+30m", 1800))
TRACK_SECONDS = 1800
LAMPORTS = Decimal(1_000_000_000)
PUMP_SUPPLY_RAW = Decimal(10**15)  # 1,000,000,000 tokens × 10^6
REJECTED_WINNER_PEAK_PCT = Decimal("30")  # "a rejected token that later performed well"


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _pct(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return ((a / b - 1) * 100).quantize(Decimal("0.01"))


def market_cap_sol(price_raw: Decimal | None, supply_raw: Decimal | None) -> Decimal | None:
    """price (lamports per raw unit) × supply (raw units) → SOL."""
    if price_raw is None or supply_raw is None:
        return None
    return (price_raw * supply_raw / LAMPORTS).quantize(Decimal("0.01"))


# --- snapshots ----------------------------------------------------------------------

def gate_snapshot(inp: Any, evidence: dict[str, Any], a: Any, decision_ctx: dict | None = None) -> dict[str, Any]:
    """What the gate decided on. `inp` is the AssessmentInput."""
    m, tok, fl, h = inp.market, inp.token, inp.flow, inp.holders
    dec = tok.decimals if tok is not None else None
    supply = Decimal(tok.supply_raw) if tok is not None and tok.supply_raw is not None else None
    price = Decimal(m.price) if m is not None and m.price is not None else None
    price_raw = price * LAMPORTS / Decimal(10) ** dec if price is not None and dec is not None else None
    sig = inp.signal
    ml = inp.ml
    q = inp.entry_quality or {}
    buyers = fl.unique_buyers if fl is not None else None
    sellers = fl.unique_sellers if fl is not None else None
    return {
        "price_sol": _s(price), "price_raw": _s(price_raw), "decimals": dec, "supply_raw": _s(supply),
        "market_cap_sol": _s(market_cap_sol(price_raw, supply)),
        "liquidity_sol": _s(m.liquidity_quote) if m is not None else None,
        "age_seconds": m.age_seconds if m is not None else None,
        "migrated": bool(m.migrated) if m is not None and m.migrated is not None else None,
        "curve_progress": _s(m.curve_progress) if m is not None else None,
        "volatility": _s(m.volatility) if m is not None else None,
        "volatility_confidence": m.volatility_confidence if m is not None else None,
        "buyers": buyers, "sellers": sellers,
        "buy_sell_ratio": _s((Decimal(buyers) / Decimal(sellers)).quantize(Decimal("0.01"))) if buyers is not None and sellers else None,
        "buy_volume_sol": _s(fl.buy_volume_quote) if fl is not None else None,
        "sell_volume_sol": _s(fl.sell_volume_quote) if fl is not None else None,
        "trades": fl.trade_count if fl is not None else None,
        "creator_sold": fl.creator_sold if fl is not None else None,
        "creator_launches_24h": getattr(fl, "creator_launches_24h", None) if fl is not None else None,
        "top10_share": _s(h.top10_share) if h is not None else None,
        "creator_share": _s(h.creator_share) if h is not None else None,
        "overall_risk": a.overall_risk.value if hasattr(a.overall_risk, "value") else _s(a.overall_risk),
        "signal_qualified": sig.qualified if sig is not None else None,
        "signal_strength": sig.strength if sig is not None else None,
        "signal_reasons": list(sig.reasons)[:6] if sig is not None else None,
        "ml_score": ml.score if ml is not None else None,
        "deterioration_indicators": q.get("indicators"),
        "entry_exit_check": (inp.entry_exit_check or {}).get("action"),
        "data_errors": [str(e)[:160] for e in (evidence.get("errors") or [])][:6],
        "price_age_seconds": (decision_ctx or {}).get("price_age_seconds"),
        "decision_eval_ms": (decision_ctx or {}).get("decision_eval_ms"),
        "operator_request": bool(getattr(inp, "operator_request", False)),
    }


def observation_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    cps = report.get("checkpoints") or []
    last = cps[-1] if cps else {}
    price_raw = next((Decimal(str(cp["price_raw"])) for cp in reversed(cps) if cp.get("price_raw")), None)
    m = report.get("metrics") or {}
    return {
        "price_raw": _s(price_raw), "supply_raw": _s(PUMP_SUPPLY_RAW),
        "market_cap_sol": _s(market_cap_sol(price_raw, PUMP_SUPPLY_RAW)), "market_cap_basis": "Pump.fun fixed supply",
        "age_seconds": report.get("age_seconds"), "trend": report.get("trend"),
        "buyers": last.get("unique_buyers"), "sellers": last.get("unique_sellers"),
        "buy_volume_sol": last.get("buy_volume_sol"), "sell_volume_sol": last.get("sell_volume_sol"),
        "creator_sold": m.get("creator_sold"), "sell_pressure": m.get("sell_pressure_latest_half"),
        "price_drawdown_from_peak_pct": m.get("price_drawdown_from_peak_pct"),
    }


async def record(session: AsyncSession, *, key: str, mint: str, symbol: str | None, engine: str, stage: str,
                 decision: str, traded: bool, reasons: list[str], decided_at: datetime, snapshot: dict[str, Any],
                 candidate_id=None, assessment_id=None, position_id=None, execution_mode: str | None = None) -> None:
    """One row per decision episode; a repeated key is ignored."""
    await session.execute(insert(OpportunityOutcome).values(
        key=key[:160], mint=mint, symbol=(symbol or None) and symbol[:64], engine=engine, stage=stage, decision=decision[:32],
        traded=traded, execution_mode=execution_mode, candidate_id=candidate_id, assessment_id=assessment_id,
        position_id=position_id, reasons=[str(r)[:300] for r in reasons][:12], decided_at=decided_at, snapshot=snapshot,
        horizons={}, status="TRACKING",
    ).on_conflict_do_nothing(index_elements=["key"]))


async def discovery_market_cap(session: AsyncSession, mint: str, supply_raw: Decimal | None) -> str | None:
    """Market cap at the first observation checkpoint (discovery)."""
    obs = (await session.execute(select(TokenObservation).where(TokenObservation.mint == mint))).scalar_one_or_none()
    cps = ((obs.report or {}).get("checkpoints") if obs is not None else None) or []
    first = next((Decimal(str(cp["price_raw"])) for cp in cps if cp.get("price_raw")), None)
    return _s(market_cap_sol(first, supply_raw or PUMP_SUPPLY_RAW))


# --- horizons ------------------------------------------------------------------------

def _price_raw(t) -> Decimal | None:
    return Decimal(t.virtual_sol) / Decimal(t.virtual_token) if t.virtual_token else None


def price_at(trades: list, at: datetime):
    """(price_raw, trade time) of the last trade at or before `at`."""
    best = None
    for t in trades:
        if t.at <= at:
            best = t
        else:
            break
    return (_price_raw(best), best.at) if best is not None else (None, None)


async def track(session: AsyncSession, redis, now: datetime, limit: int = 300) -> int:
    """Fills due horizons, the running peak/drawdown and migration time of
    rows still TRACKING. Returns how many rows changed."""
    rows = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.status == "TRACKING")
                                  .order_by(OpportunityOutcome.decided_at).limit(limit))).scalars().all()
    changed = 0
    for row in rows:
        start = row.decided_at
        end = start + timedelta(seconds=TRACK_SECONDS)
        trades = sorted(await pump_stream.load_trades(redis, row.mint), key=lambda t: t.at)
        base = Decimal(row.snapshot["price_raw"]) if (row.snapshot or {}).get("price_raw") else price_at(trades, start)[0]
        hz = dict(row.horizons or {})
        before = dict(hz)
        for name, sec in HORIZONS:
            if name in hz or now < start + timedelta(seconds=sec):
                continue
            t = start + timedelta(seconds=sec)
            price, at = price_at(trades, t)
            if price is None:
                reason = ("no stream trades for this token (PumpSwap tokens and expired history are not in the stream)"
                          if not trades else "stream history no longer reaches this time")
                hz[name] = {"unavailable": reason}
            else:
                hz[name] = {"price_raw": str(price), "change_pct": _s(_pct(price, base)),
                            "price_at": at.isoformat(), "source": "pump_stream trades"}
        if base is not None:
            window = [p for p in (_price_raw(x) for x in trades if start < x.at <= min(now, end)) if p is not None]
            if window:
                peak, trough = _pct(max(window), base), _pct(min(window), base)
                if peak is not None and (row.peak_pct is None or peak > row.peak_pct):
                    row.peak_pct = peak
                    changed += 1
                if trough is not None and (row.drawdown_pct is None or trough < row.drawdown_pct):
                    row.drawdown_pct = trough
                    changed += 1
        if row.migrated_at is None:
            ts = await redis.zscore(pump_stream.MIGRATED, row.mint)
            if ts is not None:
                when = datetime.fromtimestamp(float(ts), tz=start.tzinfo)
                if start < when <= end:
                    row.migrated_at = when
                    changed += 1
        if hz != before:
            row.horizons = hz
            changed += 1
        if now >= end + timedelta(seconds=60) and all(n in hz for n, _ in HORIZONS):
            row.status, row.completed_at = "COMPLETE", now
            changed += 1
        row.updated_at = now
    await session.commit()
    return changed


# --- trade results and loss analysis -------------------------------------------------------

LOSS_CLASSES = ("DATA_FAILURE", "EXECUTION_QUALITY", "RISK_MODEL_FAILURE", "LIQUIDITY_COLLAPSE", "MARKET_REVERSAL",
                "BAD_ENTRY", "SIGNAL_FAILURE", "UNKNOWN")
EXECUTION_SLIPPAGE_PCT = Decimal("10")  # paid this much above the decision price
SLOW_ENTRY_MS = 10_000
REVERSAL_MFE_PCT = Decimal("5")  # it went this far up before the loss
NO_UPSIDE_MFE_PCT = Decimal("2")


def classify_loss(snapshot: dict[str, Any], result: dict[str, Any], entry_diag: dict[str, Any] | None,
                  max_loss_sol: Decimal | None) -> dict[str, Any]:
    """The conditions around a losing trade and ONE primary class, with every
    contributing flag. Order: data problems first (the decision could not
    see the market), then execution, then risk control, then market."""
    flags: list[str] = []
    ev: list[str] = []
    timing = (entry_diag or {}).get("timing") or {}
    price = (entry_diag or {}).get("price") or {}
    total_vs_decision = price.get("components_pct", {}).get("total_vs_decision_pct") if price else None
    mfe = result.get("mfe_pct")
    mfe_d = Decimal(mfe) if mfe is not None else None
    pnl = Decimal(result["pnl_sol"]) if result.get("pnl_sol") is not None else None

    if snapshot.get("data_errors") or (snapshot.get("price_age_seconds") or 0) > 10 \
            or snapshot.get("volatility_confidence") in ("LOW_CONFIDENCE", "UNAVAILABLE"):
        flags.append("DATA_FAILURE")
        ev.append(f"data at decision: errors {len(snapshot.get('data_errors') or [])}, price age "
                  f"{snapshot.get('price_age_seconds')}s, volatility {snapshot.get('volatility_confidence')}")
    if (total_vs_decision is not None and Decimal(total_vs_decision) > EXECUTION_SLIPPAGE_PCT) \
            or (timing.get("decision_to_confirm_ms") or 0) > SLOW_ENTRY_MS:
        flags.append("EXECUTION_QUALITY")
        ev.append(f"entry paid {total_vs_decision}% vs the decision price, decision→confirm "
                  f"{timing.get('decision_to_confirm_ms')} ms ({price.get('classification')})")
    if pnl is not None and max_loss_sol is not None and max_loss_sol > 0 and -pnl > max_loss_sol * Decimal("1.2"):
        flags.append("RISK_MODEL_FAILURE")
        ev.append(f"loss {-pnl} SOL exceeded the planned maximum {max_loss_sol} SOL by more than 20%")
    if "liquidity" in (result.get("exit_reason") or "").lower():
        flags.append("LIQUIDITY_COLLAPSE")
        ev.append(f"exit reason {result.get('exit_reason')}")
    if mfe_d is not None and mfe_d >= REVERSAL_MFE_PCT:
        flags.append("MARKET_REVERSAL")
        ev.append(f"went +{mfe}% before reversing to {result.get('pnl_pct')}")
    warned = bool(snapshot.get("deterioration_indicators")) or snapshot.get("entry_exit_check") not in (None, "HOLD")
    if mfe_d is not None and mfe_d < NO_UPSIDE_MFE_PCT:
        if warned:
            flags.append("BAD_ENTRY")
            ev.append(f"never went up (MFE {mfe}%) and warnings existed at entry: "
                      f"{snapshot.get('deterioration_indicators')} / exit check {snapshot.get('entry_exit_check')}")
        elif snapshot.get("signal_qualified"):
            flags.append("SIGNAL_FAILURE")
            ev.append(f"never went up (MFE {mfe}%) although the signal qualified (strength {snapshot.get('signal_strength')})"
                      " and no deterioration was visible")
    primary = next((c for c in LOSS_CLASSES if c in flags), "UNKNOWN")
    return {"classification": primary, "flags": flags, "evidence": ev}


async def on_position_closed(session: AsyncSession, position: Any) -> None:
    """Trade result (and LOSS_ANALYSIS for a loss) on the opportunity row of
    a closed position. Never raises into the close."""
    row = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.position_id == position.id))).scalar_one_or_none()
    if row is None and position.candidate_id is not None:
        row = (await session.execute(select(OpportunityOutcome).where(
            OpportunityOutcome.candidate_id == position.candidate_id, OpportunityOutcome.traded.is_(True)))).scalars().first()
    if row is None:
        return
    entry = Decimal(position.entry_price) if position.entry_price else None
    snap = row.snapshot or {}
    dec = snap.get("decimals")
    supply = Decimal(snap["supply_raw"]) if snap.get("supply_raw") else None
    to_raw = (LAMPORTS / Decimal(10) ** int(dec)) if dec is not None else None

    def mcap(price) -> str | None:
        return _s(market_cap_sol(Decimal(price) * to_raw, supply)) if price is not None and to_raw is not None else None

    hold = (position.exit_at - position.entry_at).total_seconds() if position.exit_at and position.entry_at else None
    entry_order = (await session.execute(select(ExecutionOrder).where(
        ExecutionOrder.position_id == position.id, ExecutionOrder.side == "BUY").order_by(ExecutionOrder.created_at))).scalars().first()
    diag = (entry_order.diagnostics or {}) if entry_order is not None else {}
    result = {
        "pnl_sol": _s(position.realized_pnl), "pnl_pct": _s(position.realized_pnl_pct),
        "entry_price_sol": _s(position.entry_price), "exit_price_sol": _s(position.exit_price),
        "market_cap_entry_sol": mcap(position.entry_price), "market_cap_exit_sol": mcap(position.exit_price),
        "mfe_pct": _s(_pct(Decimal(position.highest_price), entry)) if position.highest_price and entry else None,
        "mae_pct": _s(_pct(Decimal(position.lowest_price), entry)) if position.lowest_price and entry else None,
        "mfe_mae_resolution": "position marks (every management tick, ~15 s)",
        "exit_reason": position.exit_reason, "hold_seconds": hold, "execution_mode": position.execution_mode,
        "entry_execution": {"decision_to_confirm_ms": (diag.get("timing") or {}).get("decision_to_confirm_ms"),
                            "price_classification": (diag.get("price") or {}).get("classification"),
                            "total_vs_decision_pct": ((diag.get("price") or {}).get("components_pct") or {}).get("total_vs_decision_pct")},
    }
    row.trade_result = result
    if position.realized_pnl is not None and position.realized_pnl < 0:
        la = classify_loss(snap, result, diag, Decimal(position.max_loss_quote) if position.max_loss_quote else None)
        row.loss_analysis = {
            "type": "LOSS_ANALYSIS", **la, "token": row.symbol or row.mint, "entry_time": _s(position.entry_at),
            "entry_market_cap_sol": result["market_cap_entry_sol"], "entry_liquidity_sol": snap.get("liquidity_sol"),
            "entry_price_sol": result["entry_price_sol"], "buy_sell_ratio": snap.get("buy_sell_ratio"),
            "volume_sol": {"buy": snap.get("buy_volume_sol"), "sell": snap.get("sell_volume_sol")},
            "volatility": snap.get("volatility"), "volatility_confidence": snap.get("volatility_confidence"),
            "signal_strength": snap.get("signal_strength"), "creator_launches_24h": snap.get("creator_launches_24h"),
            "creator_sold": snap.get("creator_sold"), "top10_share": snap.get("top10_share"),
            "execution_latency_ms": result["entry_execution"]["decision_to_confirm_ms"],
            "price_vs_decision_pct": result["entry_execution"]["total_vs_decision_pct"],
            "max_adverse_excursion_pct": result["mae_pct"], "exit_reason": position.exit_reason,
            "migration": "migrated" if snap.get("migrated") else "bonding curve",
            "data_freshness": {"price_age_seconds": snap.get("price_age_seconds"), "errors": snap.get("data_errors")},
        }


# --- comparisons -------------------------------------------------------------------------

COMPARE_FIELDS = ("market_cap_sol", "liquidity_sol", "buy_volume_sol", "buy_sell_ratio", "volatility", "top10_share",
                  "creator_launches_24h", "signal_strength", "ml_score", "age_seconds", "decision_eval_ms")
RISK_ORDINAL = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def _avg(values: list) -> str | None:
    nums = []
    for v in values:
        try:
            if v is not None:
                nums.append(Decimal(str(v)))
        except Exception:  # noqa: BLE001
            continue
    return _s((sum(nums) / len(nums)).quantize(Decimal("0.0001"))) if nums else None


def group_stats(rows: list[OpportunityOutcome]) -> dict[str, Any]:
    snaps = [r.snapshot or {} for r in rows]
    out: dict[str, Any] = {"n": len(rows)}
    for f in COMPARE_FIELDS:
        out[f] = _avg([s.get(f) for s in snaps])
    out["risk_score"] = _avg([RISK_ORDINAL.get(s.get("overall_risk") or "", None) for s in snaps])
    out["entry_latency_ms"] = _avg([((r.trade_result or {}).get("entry_execution") or {}).get("decision_to_confirm_ms") for r in rows])
    out["price_vs_decision_pct"] = _avg([((r.trade_result or {}).get("entry_execution") or {}).get("total_vs_decision_pct") for r in rows])
    out["peak_pct"] = _avg([r.peak_pct for r in rows])
    out["drawdown_pct"] = _avg([r.drawdown_pct for r in rows])
    migrated = [(r.migrated_at - r.decided_at).total_seconds() for r in rows if r.migrated_at]
    out["migrated_share"] = _s((Decimal(len(migrated)) / len(rows)).quantize(Decimal("0.01"))) if rows else None
    out["seconds_to_migration"] = _avg(migrated)
    return out


async def comparison(session: AsyncSession, since: datetime) -> dict[str, Any]:
    rows = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.decided_at >= since))).scalars().all()
    traded = [r for r in rows if r.traded and r.trade_result]
    winners = [r for r in traded if Decimal(r.trade_result.get("pnl_sol") or 0) > 0]
    losers = [r for r in traded if Decimal(r.trade_result.get("pnl_sol") or 0) <= 0]
    rejected = [r for r in rows if not r.traded]
    rejected_winners = [r for r in rejected if r.peak_pct is not None and r.peak_pct >= REJECTED_WINNER_PEAK_PCT]
    loss_classes: dict[str, int] = {}
    for r in losers:
        c = (r.loss_analysis or {}).get("classification")
        if c:
            loss_classes[c] = loss_classes.get(c, 0) + 1
    return {
        "since": since.isoformat(), "rows": len(rows), "tracking": sum(1 for r in rows if r.status == "TRACKING"),
        "winning_trades": group_stats(winners), "losing_trades": group_stats(losers),
        "traded": group_stats(traded), "rejected": group_stats(rejected),
        "rejected_later_up": group_stats(rejected_winners),
        "rejected_later_up_threshold_pct": str(REJECTED_WINNER_PEAK_PCT),
        "loss_classes": loss_classes,
        "note": "Averages over rows that have the value; a group with n < 20 is anecdotal. Observation data only — "
                "no live threshold changes from it.",
    }
