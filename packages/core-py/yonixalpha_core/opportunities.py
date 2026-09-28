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

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import opportunity_analysis as oa
from yonixalpha_core.db.models import ExecutionOrder, OpportunityOutcome, TokenObservation
from yonixalpha_core.solana import launch_features as lf
from yonixalpha_core.solana import pump_stream

HORIZONS: tuple[tuple[str, int], ...] = (("T+5s", 5), ("T+10s", 10), ("T+30s", 30), ("T+60s", 60),
                                         ("T+5m", 300), ("T+15m", 900), ("T+30m", 1800), ("T+60m", 3600))
PEAK_WINDOW_SECONDS = 1800  # peak_pct / drawdown_pct keep their 30-minute meaning
TRACK_SECONDS = 3600
POST_EXIT_GIVE_UP_SECONDS = 2 * 3600  # the stream keeps trades 3 h: later is unmeasurable
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
        # Launch / pool intelligence at the decision (solana.intel): causal
        # features for ML and review, with their own feature_version.
        "intel": json.loads(json.dumps(evidence.get("intel"), default=str)) if evidence.get("intel") else None,
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
        horizons={}, status="TRACKING", feature_version=((snapshot.get("intel") or {}).get("feature_version")),
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
    """Fills due horizons (with their path point), the 30-minute peak /
    drawdown, migration time, regime tags and the launch's early buyers of
    rows still TRACKING; at T+30m resolves the buyers' launch outcome; at
    T+60m (and 60 min after a traded row's exit) writes the analysis.
    Returns how many rows changed."""
    from yonixalpha_core import wallet_intel
    from yonixalpha_core.safety.store import GLOBAL_SCOPE, load_settings

    rows = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.status == "TRACKING")
                                  .order_by(OpportunityOutcome.decided_at).limit(limit))).scalars().all()
    if not rows:
        return 0
    settings, _ = await load_settings(session, GLOBAL_SCOPE)
    wcfg = wallet_intel.config(settings)
    started = await redis.get(pump_stream.STREAM_STARTED)
    started_ts = int(started) if started else None
    recorded = await _recorded_mints(session, {r.mint for r in rows})
    changed = 0
    for row in rows:
        start = row.decided_at
        end = start + timedelta(seconds=TRACK_SECONDS)
        trades = sorted(await pump_stream.load_trades(redis, row.mint), key=lambda t: t.at)
        # Token metadata is only needed until the regime and early buyers are recorded.
        meta = (await pump_stream.load_meta(redis, row.mint) or {}) if row.regime is None or row.mint not in recorded else {}
        base = Decimal(row.snapshot["price_raw"]) if (row.snapshot or {}).get("price_raw") else price_at(trades, start)[0]
        hz = dict(row.horizons or {})
        before = dict(hz)
        path = dict(row.path or {})
        supply = int(Decimal(row.snapshot["supply_raw"])) if (row.snapshot or {}).get("supply_raw") else None
        prev_at = start
        for name, sec in HORIZONS:
            t = start + timedelta(seconds=sec)
            if name in hz or now < t:
                prev_at = t
                continue
            price, at = price_at(trades, t)
            if price is None:
                reason = ("no stream trades for this token (PumpSwap tokens and expired history are not in the stream)"
                          if not trades else "stream history no longer reaches this time")
                hz[name] = {"unavailable": reason}
            else:
                hz[name] = {"price_raw": str(price), "change_pct": _s(_pct(price, base)),
                            "price_at": at.isoformat(), "source": "pump_stream trades"}
            if trades:
                path[name] = oa.path_point(trades, start, float(base) if base is not None else None, prev_at, t, supply)
            prev_at = t
        if base is not None:
            window = [p for p in (_price_raw(x) for x in trades
                                  if start < x.at <= min(now, start + timedelta(seconds=PEAK_WINDOW_SECONDS))) if p is not None]
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
        if row.regime is None:
            row.regime = await _regime(redis, row, meta)
            changed += 1
        if hz != before:
            row.horizons = hz
            row.path = path
            changed += 1
        changed += await _wallets(session, redis, row, trades, meta, started_ts, recorded, now, wcfg)
        if now >= end + timedelta(seconds=60) and all(n in hz for n, _ in HORIZONS) \
                and _finalize(row, trades, await _fee_bps(redis, row.mint), now):
            row.status, row.completed_at = "COMPLETE", now
            changed += 1
        row.updated_at = now
    await session.commit()
    return changed


async def _recorded_mints(session: AsyncSession, mints: set[str]) -> set[str]:
    from yonixalpha_core.db.models import LaunchBuyer

    if not mints:
        return set()
    return set((await session.execute(select(LaunchBuyer.mint).where(LaunchBuyer.mint.in_(mints)).distinct())).scalars())


async def _fee_bps(redis, mint: str) -> int | None:
    curve = await pump_stream.load_curve(redis, mint)
    return curve.fee_bps if curve is not None else None


async def _regime(redis, row: OpportunityOutcome, meta: dict) -> dict[str, Any]:
    """Regime tags at the decision time (counts before it only)."""
    t = row.decided_at.timestamp()
    launches = await redis.zcount(pump_stream.RECENT, t - 3600, t)
    migrations = await redis.zcount(pump_stream.MIGRATED, t - 3600, t)
    intel = (row.snapshot or {}).get("intel") or {}
    mayhem = meta.get("is_mayhem_mode")
    return {"data_regime": lf.data_regime(row.decided_at), "stage": intel.get("stage") or row.stage,
            "mayhem": None if mayhem in (None, "") else str(mayhem) in ("1", "true", "True"),
            "launches_last_hour_seen": launches, "migrations_last_hour_seen": migrations,
            "fee_bps": await _fee_bps(redis, row.mint),
            "strategy": row.engine, "feature_version": intel.get("feature_version"), "analysis_version": oa.ANALYSIS_VERSION,
            "model": {"ml_score": (row.snapshot or {}).get("ml_score")},
            "note": "launch / migration counts are what this system's stream saw in the hour before the decision"}


async def _wallets(session: AsyncSession, redis, row: OpportunityOutcome, trades: list, meta: dict, started_ts: int | None,
                   recorded: set[str], now: datetime, cfg) -> int:
    """Early buyers of the launch (once), their early sells while the window
    is open, and at T+30m the launch outcome. Failures are kept out of the
    ledger's own transaction."""
    from yonixalpha_core import wallet_intel

    changed = 0
    try:
        async with session.begin_nested():
            if row.mint not in recorded and trades:
                created = int(meta["created_at"]) if meta.get("created_at") else None
                if await wallet_intel.record_launch(session, redis, row.mint, trades, created, started_ts, now, cfg):
                    recorded.add(row.mint)
                    changed += 1
            elif row.mint in recorded:
                await wallet_intel.update_early(session, row.mint, trades, now, cfg)
            analysis = dict(row.analysis or {})
            if "wallet_outcome" not in analysis and now >= row.decided_at + timedelta(seconds=PEAK_WINDOW_SECONDS + 60):
                res = await wallet_intel.resolve(session, redis, row.mint, row.peak_pct, row.drawdown_pct,
                                                 row.migrated_at is not None, now, cfg) if row.mint in recorded else None
                analysis["wallet_outcome"] = res or {"outcome": None, "note": "no early buyers recorded or already resolved"}
                row.analysis = analysis
                changed += 1
    except Exception as exc:  # noqa: BLE001 - wallet intelligence never blocks the ledger
        analysis = dict(row.analysis or {})
        analysis["wallet_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        row.analysis = analysis
    return changed


def _finalize(row: OpportunityOutcome, trades: list, curve_fee_bps: int | None, now: datetime) -> bool:
    """Writes returns, recovery, labels and the counterfactual (untraded) or
    exit analysis (traded). False while a traded row still waits for its
    exit + 60 min."""
    start, end = row.decided_at, row.decided_at + timedelta(seconds=TRACK_SECONDS)
    result = row.trade_result or {}
    exit_at = datetime.fromisoformat(result["exit_at"]) if result.get("exit_at") else None
    if row.traded and now < start + timedelta(seconds=POST_EXIT_GIVE_UP_SECONDS):
        if not result or (exit_at is not None and now < exit_at + timedelta(seconds=3600 + 60)):
            return False
    base = float(Decimal(row.snapshot["price_raw"])) if (row.snapshot or {}).get("price_raw") else None
    if base is None:
        p, _ = price_at(trades, start)
        base = float(p) if p is not None else None
    fee = (row.regime or {}).get("fee_bps") or curve_fee_bps
    mayhem = (row.regime or {}).get("mayhem")
    latency = timedelta(seconds=oa.REFERENCE_LATENCY_SECONDS)
    path = {k: dict(v) for k, v in (row.path or {}).items()}  # new objects, so the JSONB change is detected
    for name, sec in HORIZONS:
        if name in path:
            path[name]["executable"] = oa.round_trip(trades, start + latency, start + timedelta(seconds=sec), fee_bps=fee,
                                                     mayhem=mayhem, migrated_at=row.migrated_at)
    row.path = path
    primary = (path.get(oa.PRIMARY_HORIZON) or {}).get("executable") or {}
    if "executable_return_pct" in primary:
        row.executable_return_pct = Decimal(str(primary["executable_return_pct"]))
    if primary.get("theoretical_return_pct") is not None:
        row.theoretical_return_pct = Decimal(str(primary["theoretical_return_pct"]))
    rec = oa.recovery(trades, start, base, end)
    analysis = dict(row.analysis or {})
    analysis.update({"version": oa.ANALYSIS_VERSION, "recovery": rec,
                     "signal_vs_execution": {"theoretical_return_pct": primary.get("theoretical_return_pct"),
                                             "executable_return_pct": primary.get("executable_return_pct"),
                                             "unknown": primary.get("unknown")}})
    if row.traded:
        row.post_exit = oa.exit_analysis(trades, exit_at, now, pnl_pct=_f(result.get("pnl_pct")),
                                         mfe_pct=_f(result.get("mfe_pct")), exit_reason=result.get("exit_reason"))
    else:
        analysis["counterfactual"] = oa.counterfactual(trades, start, base, end, fee_bps=fee, mayhem=mayhem,
                                                       migrated_at=row.migrated_at, reasons=row.reasons)
    row.analysis = analysis
    row.labels = oa.labels(trades, start, base, end, migrated_at=row.migrated_at,
                           executable_primary=primary.get("executable_return_pct"), rec=rec)
    return True


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


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
        fees = (price.get("components_pct") or {}).get("fees_pct") if price else None
        ev.append(f"loss {-pnl} SOL exceeded the planned maximum {max_loss_sol} SOL by more than 20%"
                  + (f"; entry costs were +{fees}% on top of the trade price and the planned maximum does not include "
                     "them" if fees is not None and Decimal(fees) >= EXECUTION_SLIPPAGE_PCT else ""))
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


def excursions(position: Any) -> dict[str, Any]:
    """MFE / MAE of a position against its MARKET entry price (for LIVE, the
    fill's trade price, not the cost basis that includes fees and rent)."""
    from yonixalpha_core.live_trading import market_reference  # import cycle: live_trading → paper_engine → here

    m = market_reference(position)
    ref, high, low = m["entry"], m["high"] if m["high_measured"] else None, m["low"]
    return {"mfe_pct": _s(_pct(Decimal(high), Decimal(ref))) if high is not None and ref else None,
            "mae_pct": _s(_pct(Decimal(low), Decimal(ref))) if low is not None and ref else None,
            "mfe_mae_basis": m["basis"], "mfe_mae_resolution": "position marks (every management tick, ~15 s)"}


async def on_position_closed(session: AsyncSession, position: Any) -> None:
    """Trade result (and LOSS_ANALYSIS for a loss) on the opportunity row of
    a closed position. Never raises into the close."""
    row = (await session.execute(select(OpportunityOutcome).where(OpportunityOutcome.position_id == position.id))).scalar_one_or_none()
    if row is None and position.candidate_id is not None:
        row = (await session.execute(select(OpportunityOutcome).where(
            OpportunityOutcome.candidate_id == position.candidate_id, OpportunityOutcome.traded.is_(True)))).scalars().first()
    if row is None:
        return
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
        **excursions(position),
        "exit_reason": position.exit_reason, "hold_seconds": hold, "execution_mode": position.execution_mode,
        "exit_at": position.exit_at.isoformat() if position.exit_at else None,
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


# --- ledger v2 review ----------------------------------------------------------------------

def category_filter(name: str):
    """SQL filter for one review category (None for an unknown name)."""
    from sqlalchemy import Numeric, and_

    o = OpportunityOutcome
    cf = o.analysis["counterfactual"]["classification"].astext
    exit_cls = o.post_exit["classification"].astext
    pnl = o.trade_result["pnl_sol"].astext.cast(Numeric)
    return {
        "observed": o.id.is_not(None),
        "traded": o.traded.is_(True),
        "rejected": o.traded.is_(False),
        "missed_win": cf == "MISSED_WIN",
        "rejection_justified_drawdown": cf == "REJECTION_JUSTIFIED_DRAWDOWN",
        "correct_rejection": cf == "CORRECT_REJECTION",
        "unexecutable": cf == "UNEXECUTABLE",
        "counterfactual_unknown": and_(o.traded.is_(False), cf == "UNKNOWN"),
        "true_positive": and_(o.traded.is_(True), pnl > 0),
        "false_positive": and_(o.traded.is_(True), pnl <= 0),
        "premature_exit": exit_cls == "POSSIBLY_EARLY",
        "late_exit": exit_cls == "POSSIBLY_LATE",
        "good_exit": exit_cls == "GOOD_EXIT",
        "risk_correct_exit": exit_cls == "RISK_CORRECT",
        "recovery": o.labels["recovery"].astext == "true",
        "tracking": o.status == "TRACKING",
    }.get(name)


REVIEW_CATEGORIES = ("observed", "traded", "rejected", "missed_win", "rejection_justified_drawdown", "correct_rejection",
                     "unexecutable", "counterfactual_unknown", "true_positive", "false_positive", "premature_exit",
                     "late_exit", "good_exit", "risk_correct_exit", "recovery", "tracking")


def _bucket_mcap(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "unknown"
    return "<30 SOL" if x < 30 else "30-60 SOL" if x < 60 else "60-100 SOL" if x < 100 else ">=100 SOL"


def _quantiles(values: list[float]) -> dict[str, float | None]:
    v = sorted(values)
    if not v:
        return {"n": 0, "median": None, "p90": None}
    return {"n": len(v), "median": round(v[len(v) // 2], 1), "p90": round(v[min(len(v) - 1, int(len(v) * 0.9))], 1)}


async def review(session: AsyncSession, since: datetime) -> dict[str, Any]:
    """ML Review: counts per category, missed wins by bucket, signal vs
    execution quality and snipe latency. Review data only."""
    from sqlalchemy import func

    o = OpportunityOutcome
    base = o.decided_at >= since
    counts = {}
    for name in REVIEW_CATEGORIES:
        counts[name] = (await session.execute(select(func.count()).select_from(o).where(base, category_filter(name)))).scalar_one()
    missed = (await session.execute(select(o.reasons, o.stage, o.engine, o.snapshot["market_cap_sol"].astext)
                                    .where(base, category_filter("missed_win")))).all()
    buckets: dict[str, dict[str, int]] = {"rejecting_rule": {}, "stage": {}, "market_cap_at_decision": {}}
    for reasons, stage, engine, mcap in missed:
        rule = (str((reasons or ["(none)"])[0]).split(":")[0])[:60]
        for key, val in (("rejecting_rule", rule), ("stage", f"{stage} / {engine}"), ("market_cap_at_decision", _bucket_mcap(mcap))):
            buckets[key][val] = buckets[key].get(val, 0) + 1
    sve = (await session.execute(select(func.count(), func.avg(o.theoretical_return_pct), func.avg(o.executable_return_pct))
                                 .where(base, o.executable_return_pct.is_not(None)))).one()
    traded = (await session.execute(select(o.snapshot["age_seconds"].astext, o.snapshot["decision_eval_ms"].astext,
                                           o.trade_result["entry_execution"]["decision_to_confirm_ms"].astext)
                                    .where(base, o.traded.is_(True)))).all()

    def nums(i: int) -> list[float]:
        out = []
        for r in traded:
            try:
                out.append(float(r[i]))
            except (TypeError, ValueError):
                continue
        return out

    return {
        "since": since.isoformat(), "counts": counts, "missed_win_buckets": buckets,
        "signal_vs_execution": {"rows": sve[0], "theoretical_return_avg_pct": _s(sve[1]), "executable_return_avg_pct": _s(sve[2]),
                                "horizon": oa.PRIMARY_HORIZON, "reference_size_sol": str(oa.REFERENCE_SIZE_SOL),
                                "note": "executable = reference-size curve round trip with fees, impact, latency and fixed costs"},
        "snipe_latency": {"creation_to_decision_s": _quantiles(nums(0)), "decision_eval_ms": _quantiles(nums(1)),
                          "decision_to_confirm_ms": _quantiles(nums(2))},
        "note": "Counterfactual classes use a fixed take-profit / stop rule entered at the decision (no hindsight); "
                "a MISSED_WIN is a question for review, never a reason to loosen the rule that rejected it.",
    }
