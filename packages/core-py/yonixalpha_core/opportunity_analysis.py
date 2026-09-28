"""Opportunity ledger v2: what happened after a decision, measured the way a
trade would have experienced it.

All functions are pure and read only the recorded trade stream:

  path_point        price, market cap, flow and running peak / drawdown at a
                    horizon (T+5s … T+60m)
  round_trip        executable return of a reference-size trade next to the
                    theoretical (mid-price) return: pump.fun fee on both
                    legs, curve price impact, entry latency, fixed network
                    costs. Only where constant-product math holds (not
                    Mayhem, not after migration); otherwise UNKNOWN.
  counterfactual    for a rejected / expired opportunity: would a simple,
                    non-hindsight rule (take profit at +win%, stop at
                    -stop%, whichever the stream hit first) have made money
                    AFTER costs? MISSED_WIN / REJECTION_JUSTIFIED_DRAWDOWN /
                    CORRECT_REJECTION / UNEXECUTABLE / UNKNOWN.
  exit_analysis     for a closed trade: the price 5/15/30/60 min after the
                    exit. GOOD_EXIT / POSSIBLY_EARLY / POSSIBLY_LATE /
                    RISK_CORRECT / UNKNOWN.
  recovery          trough, time to trough, whether and when the price came
                    back, and the flow right after the trough.
  labels            multi-target labels for ML (upside, migration, fast
                    dump, rug, recovery, executable return), all from after
                    the decision, stamped with when they became known.

The reference values below are analysis yardsticks, not trading rules:
nothing here is read by the gate, and a MISSED_WIN is a question for
review, never a reason to loosen the rule that rejected it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from yonixalpha_core.solana import launch_features as lf

ANALYSIS_VERSION = "ledger-2026.09.2"
LAMPORTS = 1_000_000_000
REFERENCE_SIZE_SOL = Decimal("0.05")
REFERENCE_LATENCY_SECONDS = 2  # decision → fill
# Network + default priority fee (0.0001 SOL) of a buy and a sell, plus the
# rent-reclaim transaction (live_trading.fixed_trade_costs with defaults).
REFERENCE_FIXED_COST_SOL = Decimal("0.000225")
WIN_PCT = 30.0  # the ledger's "later performed well" (opportunities.REJECTED_WINNER_PEAK_PCT)
STOP_PCT = 25.0
MIN_EXECUTABLE_WIN_PCT = 10.0  # a take-profit that nets less than this after costs is UNEXECUTABLE
EARLY_UPSIDE_PCT = 30.0
LATE_GIVEBACK_PCT = 30.0
RISK_CORRECT_DROP_PCT = 20.0
POST_EXIT_HORIZONS: tuple[tuple[str, int], ...] = (("X+5m", 300), ("X+15m", 900), ("X+30m", 1800), ("X+60m", 3600))
PRIMARY_HORIZON = "T+5m"


def _p(t) -> float | None:
    return t.virtual_sol / t.virtual_token if t.virtual_token else None


def _last(trades: list, at: datetime):
    best = None
    for t in trades:
        if t.at <= at:
            best = t
        else:
            break
    return best


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else round((a / b - 1) * 100, 2)


def _series(trades: list, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    return [(t.at, p) for t in trades if start < t.at <= end and (p := _p(t)) is not None]


def window_flow(trades: list, start: datetime, end: datetime) -> dict[str, Any]:
    w = [t for t in trades if start < t.at <= end]
    buys = [t for t in w if t.is_buy]
    sells = [t for t in w if not t.is_buy]
    return {"trades": len(w), "buys": len(buys), "sells": len(sells),
            "buyers": len({t.trader for t in buys}), "sellers": len({t.trader for t in sells}),
            "buy_sol": round(sum(t.sol_lamports for t in buys) / LAMPORTS, 6),
            "sell_sol": round(sum(t.sol_lamports for t in sells) / LAMPORTS, 6)}


def path_point(trades: list, decided_at: datetime, base: float | None, prev_at: datetime, at: datetime,
               supply_raw: int | None) -> dict[str, Any]:
    """The state at one horizon; flow counts the interval since the previous one."""
    last = _last(trades, at)
    out: dict[str, Any] = {"at": at.isoformat(), **window_flow(trades, prev_at, at)}
    if last is None or base is None:
        out["unknown"] = "no priced trade at or before this time" if last is None else "no decision price"
        return out
    price = _p(last)
    out["change_pct"] = _pct(price, base)
    out["price_age_seconds"] = round((at - last.at).total_seconds(), 1)
    if supply_raw:
        out["market_cap_sol"] = round(price * supply_raw / LAMPORTS, 2)
    seen = [p for _, p in _series(trades, decided_at, at)]
    if seen:
        out["peak_so_far_pct"] = _pct(max(seen), base)
        out["drawdown_so_far_pct"] = _pct(min(seen), base)
    return out


# --- executable return ------------------------------------------------------------------

def _curve_ok(trades: list, at: datetime, mayhem: bool | None, migrated_at: datetime | None) -> str | None:
    """None when a curve trade at `at` can be simulated, else why not."""
    if migrated_at is not None and at >= migrated_at:
        return "migrated before this time (PumpSwap pool: not simulated here)"
    cm = lf.curve_math([t for t in trades if t.at <= at], mayhem)
    if cm["valid"] is not True:
        return f"curve math does not apply: {cm['reason']}"
    return None


def round_trip(trades: list, entry_at: datetime, exit_at: datetime, *, fee_bps: int | None, mayhem: bool | None,
               migrated_at: datetime | None = None, size_sol: Decimal = REFERENCE_SIZE_SOL,
               fixed_cost_sol: Decimal = REFERENCE_FIXED_COST_SOL) -> dict[str, Any]:
    """Buy `size_sol` on the curve state at `entry_at`, sell everything on the
    state at `exit_at`. Our own footprint on later trades is ignored (small
    reference size: INFERENCE, stated in the result)."""
    if not fee_bps:
        return {"unknown": "pump.fun fee rate unknown for this token (never assumed 0)"}
    for when in (entry_at, exit_at):
        why = _curve_ok(trades, when, mayhem, migrated_at)
        if why:
            return {"unknown": why}
    a, b = _last(trades, entry_at), _last(trades, exit_at)
    if a is None or b is None:
        return {"unknown": "no curve state at entry or exit time"}
    fee = fee_bps / 10_000
    size = int(size_sol * LAMPORTS)
    net_in = int(size / (1 + fee))
    k1 = a.virtual_sol * a.virtual_token
    tokens = a.virtual_token - k1 // (a.virtual_sol + net_in)
    if tokens <= 0:
        return {"unknown": "no tokens at the entry state"}
    k2 = b.virtual_sol * b.virtual_token
    gross_out = b.virtual_sol - k2 // (b.virtual_token + tokens)
    net_out = gross_out * (1 - fee)
    fixed = float(fixed_cost_sol) * LAMPORTS
    spot_entry = _p(a)
    avg_entry = net_in / tokens
    return {
        "executable_return_pct": round((net_out - size - fixed) / size * 100, 2),
        "theoretical_return_pct": _pct(_p(b), spot_entry),
        "entry_impact_pct": _pct(avg_entry, spot_entry),
        "size_sol": str(size_sol), "fee_bps": fee_bps, "fixed_cost_sol": str(fixed_cost_sol),
        "entry_state_at": a.at.isoformat(), "exit_state_at": b.at.isoformat(),
        "basis": "constant-product curve simulation from recorded reserves; own footprint on later trades ignored",
    }


# --- counterfactual ---------------------------------------------------------------------

def counterfactual(trades: list, decided_at: datetime, base: float | None, end: datetime, *, fee_bps: int | None,
                   mayhem: bool | None, migrated_at: datetime | None, reasons: list[str] | None,
                   win_pct: float = WIN_PCT, stop_pct: float = STOP_PCT,
                   latency: int = REFERENCE_LATENCY_SECONDS) -> dict[str, Any]:
    """What a take-profit / stop rule entered at the decision would have got."""
    out: dict[str, Any] = {"rejecting_rule": (reasons or [None])[0], "win_pct": win_pct, "stop_pct": stop_pct,
                           "rule": f"enter at decision + {latency}s; take profit at +{win_pct:g}%, stop at -{stop_pct:g}%, "
                                   "whichever the stream reaches first (no hindsight)"}
    series = _series(trades, decided_at, end)
    if base is None or not series:
        return {**out, "classification": "UNKNOWN", "reason": "no priced trades after the decision"}
    pcts = [(at, (p / base - 1) * 100) for at, p in series]
    peak_at, peak = max(pcts, key=lambda x: x[1])
    before_peak = [x for at, x in pcts if at <= peak_at]
    trough_after = min((x for at, x in pcts if at > decided_at), default=None)
    later = min(pcts, key=lambda x: x[1]) if pcts else None
    out.update({"peak_pct": round(peak, 2), "time_to_peak_seconds": round((peak_at - decided_at).total_seconds(), 1),
                "drawdown_before_peak_pct": round(min(0.0, min(before_peak)), 2),
                "max_drawdown_pct": round(min(0.0, trough_after), 2) if trough_after is not None else None})
    if later is not None and later[1] < -5 and later[0] < peak_at:
        out["better_later_entry"] = {"at": later[0].isoformat(), "pct_below_decision": round(later[1], 2)}
    hit = next(((at, "TP" if x >= win_pct else "STOP") for at, x in pcts if x >= win_pct or x <= -stop_pct), None)
    entry_at = decided_at + timedelta(seconds=latency)
    if hit is None:
        out["rule_exit"] = "neither level reached within the window"
        return {**out, "classification": "CORRECT_REJECTION"}
    at, kind = hit
    sim = round_trip(trades, entry_at, at + timedelta(seconds=latency), fee_bps=fee_bps, mayhem=mayhem, migrated_at=migrated_at)
    out.update({"rule_exit": kind, "rule_exit_at": at.isoformat(), "rule_exit_executable": sim})
    if kind == "STOP":
        return {**out, "classification": "REJECTION_JUSTIFIED_DRAWDOWN" if peak >= win_pct else "CORRECT_REJECTION"}
    if "unknown" in sim:
        return {**out, "classification": "UNKNOWN", "reason": sim["unknown"]}
    if sim["executable_return_pct"] < MIN_EXECUTABLE_WIN_PCT:
        return {**out, "classification": "UNEXECUTABLE",
                "reason": f"the take profit nets {sim['executable_return_pct']}% after fees, impact and latency"}
    return {**out, "classification": "MISSED_WIN",
            "note": "a question for review, not a reason to loosen the rejecting rule on its own"}


# --- exits ----------------------------------------------------------------------------------

STOP_WORDS = ("stop", "emergency", "risk", "liquidity", "rug", "kill", "dump", "deteriorat")


def exit_analysis(trades: list, exit_at: datetime | None, now: datetime, *, pnl_pct: float | None, mfe_pct: float | None,
                  exit_reason: str | None) -> dict[str, Any]:
    """Market price after the exit (vs the market price at the exit)."""
    if exit_at is None:
        return {"classification": "UNKNOWN", "reason": "exit time unknown"}
    ref = _last(trades, exit_at)
    if ref is None:
        return {"classification": "UNKNOWN", "reason": "no stream trades for this token around the exit"}
    base = _p(ref)
    out: dict[str, Any] = {"exit_at": exit_at.isoformat(), "exit_market_price_raw": base, "horizons": {}}
    for name, sec in POST_EXIT_HORIZONS:
        t = exit_at + timedelta(seconds=sec)
        if now < t:
            continue
        last = _last(trades, t)
        out["horizons"][name] = {"change_pct": _pct(_p(last), base)} if last is not None else {"unknown": "no trade"}
    series = [(at, (p / base - 1) * 100) for at, p in _series(trades, exit_at, exit_at + timedelta(seconds=3600))]
    if not series:
        return {**out, "classification": "UNKNOWN", "reason": "no trades after the exit"}
    post_peak = max(x for _, x in series)
    post_min = min(x for _, x in series)
    first_up = next((at for at, x in series if x >= EARLY_UPSIDE_PCT), None)
    stop_before_up = first_up is not None and any(x <= -STOP_PCT for at, x in series if at < first_up)
    out.update({"post_exit_peak_pct": round(post_peak, 2), "post_exit_min_pct": round(post_min, 2)})
    flags = []
    if any(w in (exit_reason or "").lower() for w in STOP_WORDS) and post_min <= -RISK_CORRECT_DROP_PCT:
        flags.append("RISK_CORRECT")
    if first_up is not None and not stop_before_up:
        flags.append("POSSIBLY_EARLY")
    if pnl_pct is not None and mfe_pct is not None and ((1 + pnl_pct / 100) / (1 + mfe_pct / 100) - 1) * 100 <= -LATE_GIVEBACK_PCT:
        flags.append("POSSIBLY_LATE")
    primary = next((c for c in ("RISK_CORRECT", "POSSIBLY_EARLY", "POSSIBLY_LATE") if c in flags), "GOOD_EXIT")
    return {**out, "classification": primary, "flags": flags}


# --- recovery and labels ------------------------------------------------------------------

def recovery(trades: list, decided_at: datetime, base: float | None, end: datetime) -> dict[str, Any]:
    series = _series(trades, decided_at, end)
    if base is None or not series:
        return {"unknown": "no priced trades after the decision"}
    trough_at, trough = min(series, key=lambda x: x[1])
    peak_before = max((p for at, p in series if at <= trough_at), default=base)
    back = next((at for at, p in series if at > trough_at and p >= base), None)
    if trough >= base:
        back = None  # never below the decision price: nothing to recover from
    return {"mae_pct": _pct(min(trough, base), base), "mfe_pct": _pct(max(p for _, p in series), base),
            "time_to_trough_seconds": round((trough_at - decided_at).total_seconds(), 1),
            "peak_before_trough_pct": _pct(peak_before, base),
            "recovered": None if trough >= base else back is not None, "time_to_recovery_seconds": round((back - trough_at).total_seconds(), 1) if back else None,
            "flow_before_trough_60s": window_flow(trades, trough_at - timedelta(seconds=60), trough_at),
            "flow_after_trough_60s": window_flow(trades, trough_at, trough_at + timedelta(seconds=60))}


def labels(trades: list, decided_at: datetime, base: float | None, end: datetime, *, migrated_at: datetime | None,
           executable_primary: float | None, rec: dict[str, Any]) -> dict[str, Any]:
    """Targets, all from after the decision; `available_at` is when the last
    of them became known (use it for time-based splits)."""
    series = _series(trades, decided_at, end)
    out: dict[str, Any] = {"available_at": end.isoformat(), "version": ANALYSIS_VERSION,
                           "migrate_60m": bool(migrated_at and decided_at < migrated_at <= end),
                           "executable_return_primary_pct": executable_primary, "primary_horizon": PRIMARY_HORIZON,
                           "manipulation": None, "manipulation_note": "no ground truth; the score at decision is a feature"}
    if base is None or not series:
        out["unknown"] = "no priced trades after the decision"
        return out
    pcts = [(at, (p / base - 1) * 100) for at, p in series]
    peak = max(x for _, x in pcts)
    for level in (50, 100, 200):
        out[f"upside_{level}"] = peak >= level
    first50 = next((at for at, x in pcts if x >= 50), None)
    out["time_to_upside_50_seconds"] = round((first50 - decided_at).total_seconds(), 1) if first50 else None
    fast = [x for at, x in pcts if at <= decided_at + timedelta(seconds=300)]
    out["fast_dump"] = bool(fast) and min(fast) <= -50
    out["rug_60m"] = pcts[-1][1] <= -80
    out["recovery"] = bool(rec.get("recovered")) and (rec.get("mae_pct") or 0) <= -20
    out["max_drawdown_pct"] = round(min(0.0, min(x for _, x in pcts)), 2)
    out["peak_pct"] = round(peak, 2)
    return out
