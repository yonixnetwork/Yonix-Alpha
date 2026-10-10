"""Outcomes of recorded entry signals, measured the way a trade would have
experienced them, and the migrated-token entry variants.

A signal recorded at `decided_at` is labelled once LABEL_HORIZON_SECONDS
have passed, from the recorded trade stream only:

  entry        at decided_at + the measured live decision-to-fill latency
               (median of confirmed LIVE buys; a default when none exist,
               stated in the label): the price a real order could get, not
               the price the decision saw.
  rule exit    take profit at +TP%, stop at -SL%, or a time exit after
               MAX_HOLD seconds, whichever the stream reaches first after
               the entry (no hindsight), then the same latency again.
  executable   a reference-size round trip through the curve at those two
               states: pump.fun fee on both legs, price impact, fixed
               network costs (opportunity_analysis.round_trip). UNKNOWN
               where curve math does not apply.
  timing       where the entry sat between the first observable price and
               the peak (0 = at the start, 1 = at the peak), and whether the
               token was already decelerating.

Migrated tokens are priced from PumpSwap pool reserve samples
(quote / base): the same rule exit, a constant-product round trip when
the pool fee is known, the theoretical return otherwise (stated).

Nothing here is read by the safety gate. Labels feed the strategy
comparison (entry_eval) and the entry-timing model (services/ml) only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from yonixalpha_core import entry_intel as ei
from yonixalpha_core import opportunity_analysis as oa

LABEL_VERSION = "entry-label-2026.10.1"
LABEL_HORIZON_SECONDS = 900
TP_PCT = 30.0
SL_PCT = 20.0
MAX_HOLD_SECONDS = 300
DEFAULT_LATENCY_SECONDS = 3.0  # used until LIVE buys have been measured (stated in the label)
LATE_ENTRY_POSITION = 0.7
BAD_ENTRY_MAE_PCT = -20.0
HORIZONS = (("+30s", 30), ("+60s", 60), ("+5m", 300), ("+15m", 900))
LAMPORTS = 1_000_000_000


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
    return None if a is None or not b else round((a / b - 1) * 100, 3)


def _rule_exit(series: list[tuple[datetime, float]], entry_at: datetime, base: float, tp: float, sl: float,
               max_hold: int) -> tuple[datetime, str]:
    limit = entry_at + timedelta(seconds=max_hold)
    for at, p in series:
        if at <= entry_at:
            continue
        if at > limit:
            break
        ch = (p / base - 1) * 100
        if ch >= tp:
            return at, "TAKE_PROFIT"
        if ch <= -sl:
            return at, "STOP_LOSS"
    return limit, "TIME_EXIT"


def label_fresh(trades: list, decided_at: datetime, now: datetime, *, latency_s: float, latency_source: str,
                fee_bps: int | None, mayhem: bool | None, migrated_at: datetime | None,
                size_sol: Decimal = oa.REFERENCE_SIZE_SOL, fixed_cost_sol: Decimal = oa.REFERENCE_FIXED_COST_SOL,
                phase_at_decision: str | None = None, horizon: int = LABEL_HORIZON_SECONDS) -> dict[str, Any]:
    held = sorted(trades, key=lambda t: t.at)
    out: dict[str, Any] = {"version": LABEL_VERSION, "labelled_at": now.isoformat(), "horizon_seconds": horizon,
                           "latency_seconds": latency_s, "latency_source": latency_source,
                           "rule": f"enter at decision + latency; exit at +{TP_PCT:g}% / -{SL_PCT:g}% / {MAX_HOLD_SECONDS}s, "
                                   "whichever the stream reaches first, + latency"}
    dec = _last(held, decided_at)
    if dec is None:
        return {**out, "unknown": "no trade at or before the decision in the held stream"}
    entry_at = decided_at + timedelta(seconds=latency_s)
    ent = _last(held, entry_at)
    base_dec, base = _p(dec), _p(ent)
    out["decision_price_raw"], out["entry_price_raw"] = base_dec, base
    out["latency_displacement_pct"] = _pct(base, base_dec)
    end = decided_at + timedelta(seconds=horizon)
    series = [(t.at, p) for t in held if entry_at < t.at <= end and (p := _p(t)) is not None]
    if not series:
        out["no_trades_after_entry"] = True
    for name, sec in HORIZONS:
        last = _last(held, entry_at + timedelta(seconds=sec))
        out[f"return_{name}_pct"] = _pct(_p(last), base) if last is not None else None
    pcts = [(at, (p / base - 1) * 100) for at, p in series] if base else []
    if pcts:
        peak_at, peak = max(pcts, key=lambda x: x[1])
        out.update({"mfe_pct": round(max(0.0, peak), 3), "mae_pct": round(min(0.0, min(x for _, x in pcts)), 3),
                    "time_to_peak_seconds": round((peak_at - entry_at).total_seconds(), 1)})
    else:
        out.update({"mfe_pct": 0.0, "mae_pct": 0.0, "time_to_peak_seconds": None})
    exit_at, kind = _rule_exit(series, entry_at, base or 0.0, TP_PCT, SL_PCT, MAX_HOLD_SECONDS) if base else (entry_at, "NONE")
    out["rule_exit"], out["rule_exit_at"] = kind, exit_at.isoformat()
    sim = oa.round_trip(held, entry_at, exit_at + timedelta(seconds=latency_s), fee_bps=fee_bps, mayhem=mayhem,
                        migrated_at=migrated_at, size_sol=size_sol, fixed_cost_sol=fixed_cost_sol)
    if "unknown" in sim:
        out["executable_unknown"] = sim["unknown"]
        out["executable_return_pct"] = None
    else:
        out["executable_return_pct"] = sim["executable_return_pct"]
        out["entry_impact_pct"] = sim["entry_impact_pct"]
    # Entry timing against the token's own move (first observable price -> peak).
    first = next((p for t in held if (p := _p(t)) is not None), None)
    all_prices = [p for t in held if t.at <= end and (p := _p(t)) is not None]
    peak_all = max(all_prices) if all_prices else None
    out["first_price_raw"], out["peak_price_raw"] = first, peak_all
    if first and peak_all and base and peak_all > first:
        pos = (base - first) / (peak_all - first)
        out["entry_position"] = round(max(0.0, min(1.0, pos)), 4)
        out["late_entry"] = pos >= LATE_ENTRY_POSITION
        out["move_captured_pct"] = round(max(0.0, (peak_all - base) / (peak_all - first)) * 100, 2)
    else:
        out["entry_position"], out["late_entry"] = None, None
    out["decelerating_at_entry"] = phase_at_decision in (ei.EXHAUSTED, ei.DISTRIBUTION) if phase_at_decision else None
    ex = out.get("executable_return_pct")
    out["win"] = None if ex is None else ex > 0
    out["bad_entry"] = None if ex is None else (ex < 0 and (out.get("mae_pct") or 0) <= BAD_ENTRY_MAE_PCT)
    out["migrated_within_horizon"] = bool(migrated_at and decided_at < migrated_at <= end)
    return out


# --- migrated tokens ----------------------------------------------------------------------

def _sample_price(s: tuple) -> float | None:
    _, base, quote = s
    return quote / base if base else None


def migrated_triggers(samples: list[tuple[float, int, int]], migrated_ts: float, now_ts: float,
                      done: set[str] | None = None) -> dict[str, float]:
    """First time each variant's entry condition held, from pool reserve
    samples [(ts, base_raw, quote_lamports)] taken at or after migration.
    `done`: variants already recorded (skipped). Times are sample times."""
    done = done or set()
    s = sorted(x for x in samples if x[0] >= migrated_ts and x[0] <= now_ts)
    out: dict[str, float] = {}
    if not s:
        return out
    if ei.MIGRATED_NO_TRADE not in done:
        out[ei.MIGRATED_NO_TRADE] = s[0][0]
    if ei.MIGRATED_IMMEDIATE not in done:
        out[ei.MIGRATED_IMMEDIATE] = s[0][0]
    p0, q0 = _sample_price(s[0]), s[0][2]
    high, low_after_high, pulled = p0, None, False
    for i, x in enumerate(s):
        p = _sample_price(x)
        if p is None:
            continue
        age = x[0] - migrated_ts
        q_prev = next((y[2] for y in reversed(s[:i]) if x[0] - y[0] >= 60), s[0][2])
        liquidity_rising = x[2] > q_prev
        if (ei.MIGRATED_DELAYED_CONFIRMATION not in done and ei.MIGRATED_DELAYED_CONFIRMATION not in out
                and age >= 120 and p0 and p >= p0 and x[2] > q0):
            out[ei.MIGRATED_DELAYED_CONFIRMATION] = x[0]
        if (ei.MIGRATED_CONTINUATION not in done and ei.MIGRATED_CONTINUATION not in out
                and age >= 60 and p >= high and liquidity_rising and p > p0):
            out[ei.MIGRATED_CONTINUATION] = x[0]
        if high and p <= high * 0.9:
            pulled = True
            low_after_high = p if low_after_high is None else min(low_after_high, p)
        if (ei.MIGRATED_PULLBACK not in done and ei.MIGRATED_PULLBACK not in out and pulled and low_after_high
                and p >= low_after_high * 1.03 and liquidity_rising):
            out[ei.MIGRATED_PULLBACK] = x[0]
        if p > high:
            high, low_after_high, pulled = p, None, False
    return out


def label_migrated(samples: list[tuple[float, int, int]], decided_ts: float, now_ts: float, *, latency_s: float,
                   latency_source: str, fee_bps: int | None, strategy: str, size_sol: float = float(oa.REFERENCE_SIZE_SOL),
                   fixed_cost_sol: float = float(oa.REFERENCE_FIXED_COST_SOL),
                   horizon: int = LABEL_HORIZON_SECONDS) -> dict[str, Any]:
    out: dict[str, Any] = {"version": LABEL_VERSION, "horizon_seconds": horizon, "latency_seconds": latency_s,
                           "latency_source": latency_source, "price_source": "PumpSwap pool reserve samples (quote/base)",
                           "rule": f"enter at decision + latency; exit at +{TP_PCT:g}% / -{SL_PCT:g}% / {MAX_HOLD_SECONDS}s"}
    if strategy == ei.MIGRATED_NO_TRADE:
        return {**out, "executable_return_pct": 0.0, "win": None, "bad_entry": False,
                "note": "the no-trade baseline: nothing entered, nothing lost"}
    s = sorted(samples)
    ent = [x for x in s if x[0] <= decided_ts + latency_s]
    if not ent:
        return {**out, "unknown": "no pool sample at or before the entry"}
    e = ent[-1]
    base = _sample_price(e)
    out["entry_price_raw"] = base
    later = [(x[0], p) for x in s if x[0] > e[0] and x[0] <= decided_ts + horizon and (p := _sample_price(x))]
    pcts = [(at, (p / base - 1) * 100) for at, p in later] if base else []
    if pcts:
        out["mfe_pct"] = round(max(0.0, max(x for _, x in pcts)), 3)
        out["mae_pct"] = round(min(0.0, min(x for _, x in pcts)), 3)
    exit_ts, kind = decided_ts + latency_s + MAX_HOLD_SECONDS, "TIME_EXIT"
    for at, ch in pcts:
        if at > decided_ts + latency_s + MAX_HOLD_SECONDS:
            break
        if ch >= TP_PCT or ch <= -SL_PCT:
            exit_ts, kind = at, "TAKE_PROFIT" if ch >= TP_PCT else "STOP_LOSS"
            break
    xs = [x for x in s if x[0] <= exit_ts + latency_s]
    x = xs[-1]
    out["rule_exit"] = kind
    theoretical = (_sample_price(x) / base - 1) * 100 if base and _sample_price(x) else None
    out["theoretical_return_pct"] = round(theoretical, 3) if theoretical is not None else None
    if not fee_bps:
        out["executable_return_pct"] = None
        out["executable_unknown"] = "pool fee unknown: theoretical return only"
    else:
        fee = fee_bps / 10_000
        size = size_sol * LAMPORTS
        net_in = size / (1 + fee)
        b0, q0 = e[1], e[2]
        tokens = b0 - (b0 * q0) / (q0 + net_in)
        b1, q1 = x[1], x[2]
        gross = q1 - (b1 * q1) / (b1 + tokens) if tokens > 0 else 0
        net_out = gross * (1 - fee)
        out["executable_return_pct"] = round((net_out - size - fixed_cost_sol * LAMPORTS) / size * 100, 3)
    ex = out.get("executable_return_pct")
    out["win"] = None if ex is None else ex > 0
    out["bad_entry"] = None if ex is None else (ex < 0 and (out.get("mae_pct") or 0) <= BAD_ENTRY_MAE_PCT)
    return out
