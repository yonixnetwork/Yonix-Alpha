"""Simulated execution failures for PAPER trades.

Live orders fail: a transaction is dropped, not confirmed before its
blockhash expires, or rejected on chain (slippage). A paper book that always
fills looks better than live ever could, so paper applies a failure rate
to every entry and exit attempt:

- a failed ENTRY never opens the position (the candidate is rejected, as a
  failed live buy does);
- a failed EXIT leaves the position open: marks, trailing ratchet and stop
  tightening are still recorded, and the exit is attempted again on the next
  tick at *that* price, which is where the real cost of a failed exit shows
  up (a stop that fills later, lower).

Price drift (2026-10-09): a paper fill happens at the price the decision
saw; a live order lands seconds later, after other traders moved the price
(buys pay more, sells receive less). Every confirmed LIVE order records that
movement (execution_analysis: decision -> build -> landing for buys, expected
vs received for sells). With at least MIN_LIVE_SAMPLE measured orders of a
side, the median adverse movement is charged to every paper fill of that
side (charge_measured_live_drift); a favourable median is never credited.

Rates are never invented. The operator sets them (DB, dashboard; default
0%). Once at least MIN_LIVE_SAMPLE live orders of a side have a final
outcome, the rate measured from `execution_orders` is used instead (unless
switched off). Each attempt draws deterministically from a hash of
(position/assessment, attempt), so a replay of the same data gives the same
result and tests are exact.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ExecutionOrder, PlatformSetting

SETTINGS_KEY = "paper_execution"
MIN_LIVE_SAMPLE = 20
MAX_FAILURE_PCT = Decimal(50)
MAX_DRIFT_PCT = Decimal(25)
DRIFT_SAMPLE = 200  # newest measured orders per side
FINAL_STATUSES = ("CONFIRMED", "FAILED", "EXPIRED")  # CANCELLED orders never reached the chain


@dataclass
class PaperExecutionSettings:
    entry_failure_pct: Decimal = Decimal(0)
    exit_failure_pct: Decimal = Decimal(0)
    use_measured_live_rates: bool = True
    # Solana paper trades pay what a LIVE round trip pays regardless of size
    # (live_trading.fixed_trade_costs: buy and sell network + priority fees,
    # rent reclaim or unreclaimed rent): counted in the risk plan exactly as
    # for LIVE and charged to the paper book. Off, paper sizes and books as
    # before (audit 2026-10-07: paper skipped them, so it took trades LIVE
    # refused and reported a better result than LIVE could get).
    charge_live_fixed_costs: bool = True
    # The price LIVE loses between decision and landing (module docstring).
    charge_measured_live_drift: bool = True
    # Pump-curve paper buys fill at the stream price measured LIVE latency
    # after the decision (schedule_entry_delay / settle_entry_delay).
    simulate_entry_delay: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"entry_failure_pct": str(self.entry_failure_pct), "exit_failure_pct": str(self.exit_failure_pct),
                "use_measured_live_rates": self.use_measured_live_rates,
                "charge_live_fixed_costs": self.charge_live_fixed_costs,
                "charge_measured_live_drift": self.charge_measured_live_drift,
                "simulate_entry_delay": self.simulate_entry_delay}


def parse_settings(data: dict[str, Any]) -> tuple[PaperExecutionSettings, list[str]]:
    s, errors = PaperExecutionSettings(), []
    for key, value in (data or {}).items():
        if key in ("use_measured_live_rates", "charge_live_fixed_costs", "charge_measured_live_drift",
                   "simulate_entry_delay"):
            if isinstance(value, bool):
                setattr(s, key, value)
            else:
                errors.append(f"{key}: must be true/false")
        elif key in ("entry_failure_pct", "exit_failure_pct"):
            try:
                d = Decimal(str(value))
            except Exception:  # noqa: BLE001
                errors.append(f"{key}: must be a number")
                continue
            if isinstance(value, bool) or not d.is_finite() or not Decimal(0) <= d <= MAX_FAILURE_PCT:
                errors.append(f"{key}: must be between 0 and {MAX_FAILURE_PCT}")
                continue
            setattr(s, key, d)
        else:
            errors.append(f"{key}: unknown setting")
    return s, errors


async def load_settings(session: AsyncSession) -> PaperExecutionSettings:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_settings((row.value if row else None) or {})
    return s if not errors else PaperExecutionSettings()


async def measured_live_rates(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Failure rate of LIVE orders per side, from final outcomes only, counted
    once per position and side: the first final attempt decides. Retries of
    one stuck exit are not separate trials (2026-10-07: 6 450 retries of one
    position's sell, failing on the same program error, had pushed the
    measured sell failure rate, and with it every paper exit, to the 50 % cap).
    Orders without a position count individually."""
    from sqlalchemy import literal_column

    o = ExecutionOrder
    base = (o.mode == "LIVE", o.provider == "pumpportal_local", o.status.in_(FINAL_STATUSES), o.side.in_(("BUY", "SELL")))
    first = (select(o.side, o.status)
             .where(*base, o.position_id.is_not(None))
             .distinct(o.position_id, o.side)
             .order_by(o.position_id, o.side, o.created_at)).subquery()
    loose = select(o.side, o.status).where(*base, o.position_id.is_(None)).subquery()
    rows = []
    for sub in (first, loose):
        rows += (await session.execute(select(sub.c.side, sub.c.status, func.count(literal_column("*")))
                                       .group_by(sub.c.side, sub.c.status))).all()
    out: dict[str, dict[str, Any]] = {}
    for side in ("BUY", "SELL"):
        total = sum(n for sd, _, n in rows if sd == side)
        failed = sum(n for sd, st, n in rows if sd == side and st != "CONFIRMED")
        pct = (Decimal(failed) * 100 / total).quantize(Decimal("0.01")) if total else None
        out[side] = {"orders": total, "failed": failed, "failure_pct": pct, "usable": total >= MIN_LIVE_SAMPLE,
                     "counted": "first final attempt per position"}
    return out


async def effective_rates(session: AsyncSession) -> dict[str, Any]:
    """{'entry_pct', 'exit_pct', 'entry_source', 'exit_source', 'measured', 'settings'}"""
    s = await load_settings(session)
    measured = await measured_live_rates(session)
    out: dict[str, Any] = {"settings": s.to_dict(), "measured": measured}
    for kind, side, configured in (("entry", "BUY", s.entry_failure_pct), ("exit", "SELL", s.exit_failure_pct)):
        m = measured[side]
        if s.use_measured_live_rates and m["usable"]:
            out[f"{kind}_pct"] = min(m["failure_pct"], MAX_FAILURE_PCT)
            out[f"{kind}_source"] = f"measured from {m['orders']} live {side} orders"
        else:
            out[f"{kind}_pct"] = configured
            out[f"{kind}_source"] = "operator setting"
    return out


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def buy_drift_pct(diagnostics: dict[str, Any] | None) -> Decimal | None:
    """Market movement against a confirmed LIVE buy before it landed, in %:
    decision -> build, then build -> just before our trade (our own impact
    and the fees are not in it: paper charges those itself)."""
    comp = (((diagnostics or {}).get("price") or {}).get("components_pct")) or {}
    a, b = _dec(comp.get("decision_to_build_pct")), _dec(comp.get("build_to_landing_pct"))
    if a is None and b is None:
        return None
    return ((1 + (a or Decimal(0)) / 100) * (1 + (b or Decimal(0)) / 100) - 1) * 100


def sell_drift_pct(diagnostics: dict[str, Any] | None, amount: str | None, decimals: Any) -> Decimal | None:
    """How much less a confirmed LIVE sell received than its decision expected,
    in % (network fee added back: paper charges it separately)."""
    d = diagnostics or {}
    expected = _dec((d.get("decision") or {}).get("price_sol"))
    price = d.get("price") or {}
    all_in, fee = _dec(price.get("all_in_price_sol")), _dec(price.get("network_fee_sol")) or Decimal(0)
    raw, dec = _dec(amount), _dec(decimals)
    if not expected or expected <= 0 or all_in is None or raw is None or dec is None or raw <= 0:
        return None
    tokens = raw / (Decimal(10) ** int(dec))
    return (1 - (all_in + fee / tokens) / expected) * 100


def _median(values: list[Decimal]) -> Decimal:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


async def measured_live_drift(session: AsyncSession) -> dict[str, Any]:
    """{'buy_pct', 'sell_pct', 'buy_n', 'sell_n', 'source'}: the median adverse
    price movement of the newest DRIFT_SAMPLE measured confirmed LIVE orders
    per side, clamped to [0, MAX_DRIFT_PCT]; None while fewer than
    MIN_LIVE_SAMPLE orders of that side are measured, or when switched off."""
    s = await load_settings(session)
    out: dict[str, Any] = {"buy_pct": None, "sell_pct": None, "buy_n": 0, "sell_n": 0,
                           "source": "switched off" if not s.charge_measured_live_drift else None}
    if not s.charge_measured_live_drift:
        return out
    o = ExecutionOrder
    rows = (await session.execute(select(o.side, o.diagnostics, o.amount, o.limits).where(
        o.mode == "LIVE", o.status == "CONFIRMED", o.side.in_(("BUY", "SELL")))
        .order_by(o.created_at.desc()).limit(DRIFT_SAMPLE * 4))).all()
    buys: list[Decimal] = []
    sells: list[Decimal] = []
    for side, diag, amount, limits in rows:
        if side == "BUY" and len(buys) < DRIFT_SAMPLE and (v := buy_drift_pct(diag)) is not None:
            buys.append(v)
        elif side == "SELL" and len(sells) < DRIFT_SAMPLE and \
                (v := sell_drift_pct(diag, amount, (limits or {}).get("decimals"))) is not None:
            sells.append(v)
    for key, values in (("buy", buys), ("sell", sells)):
        out[f"{key}_n"] = len(values)
        if len(values) >= MIN_LIVE_SAMPLE:
            out[f"{key}_pct"] = min(max(_median(values), Decimal(0)), MAX_DRIFT_PCT).quantize(Decimal("0.0001"))
    out["source"] = (f"median of {out['buy_n']} live buys / {out['sell_n']} live sells "
                     f"(at least {MIN_LIVE_SAMPLE} needed per side)")
    return out


def draw(key: str) -> Decimal:
    """Deterministic uniform value in [0, 1) for `key`."""
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return Decimal(h) / Decimal(2**64)


def simulated_failure(key: str, pct: Decimal) -> bool:
    return pct > 0 and draw(key) * 100 < pct


# --- migration crossing (R4, 2026-10-15) ------------------------------------------------

CROSSING_KEY = "migration_crossing"
SELL_LATENCY_CACHE = "yx:paper:sell_latency"
DEFAULT_SELL_LATENCY_SECONDS = 3.0  # until 5 LIVE sells are measured (stated with the value)


async def measured_sell_latency(session: AsyncSession, redis) -> tuple[float, str]:
    """Median submission -> confirmation of confirmed LIVE sells (newest 200),
    cached 10 minutes; the stated default while fewer than 5 are measured."""
    import statistics

    if redis is not None:
        cached = await redis.get(SELL_LATENCY_CACHE)
        if cached:
            v, src = cached.split("|", 1)
            return float(v), src
    o = ExecutionOrder
    rows = (await session.execute(select(o.created_at, o.confirmed_at).where(
        o.mode == "LIVE", o.side == "SELL", o.status == "CONFIRMED", o.confirmed_at.is_not(None))
        .order_by(o.created_at.desc()).limit(200))).all()
    vals = [(c - s).total_seconds() for s, c in rows if c >= s]
    if len(vals) >= 5:
        out = (round(statistics.median(vals), 2), f"MEASURED: median of {len(vals)} confirmed LIVE sells")
    else:
        out = (DEFAULT_SELL_LATENCY_SECONDS, f"DEFAULT: {len(vals)} measured LIVE sells (need 5)")
    if redis is not None:
        await redis.set(SELL_LATENCY_CACHE, f"{out[0]}|{out[1]}", ex=600)
    return out


def schedule_crossing(plan: dict | None, now: datetime, latency_s: float, source: str) -> dict:
    """A paper position whose token just migrated. A LIVE seller first has to
    notice the migration and route the sell to the pool: for latency_s the
    paper position is not managed, then it is managed at the pool price of
    that later tick, selling at most one take-profit level per tick."""
    return {**(plan or {}), CROSSING_KEY: {"migrated_at": now.isoformat(), "latency_s": round(latency_s, 3),
                                           "due_ts": round(now.timestamp() + latency_s, 3), "source": source}}


def crossing_pending(position, now: datetime) -> bool:
    d = (position.plan or {}).get(CROSSING_KEY) or {}
    return bool(d) and now.timestamp() < float(d["due_ts"])


# --- entry delay (regression audit 2026-10-10) ------------------------------------------

ENTRY_DELAY_KEY = "entry_delay"


def schedule_entry_delay(plan: dict | None, decision_ts: float, latency_s: float, source: str, venue: str = "curve",
                         fallback_drift_pct: Decimal | None = None) -> dict:
    """The plan of a paper buy that lands like a LIVE one: filled at the
    price `latency_s` after the decision, not at the decision price. Until
    then the position is not managed (a buy that has not landed cannot be
    sold). venue "curve": the pump.fun stream price; "pool" (R5,
    2026-10-15): recorded PumpSwap trades, and when they cannot prove the
    price at landing, `fallback_drift_pct` (the measured LIVE buy drift)
    is charged instead, so a pool entry is never more optimistic than before."""
    d = {"decision_ts": round(decision_ts, 3), "latency_s": round(latency_s, 3),
         "due_ts": round(decision_ts + latency_s, 3), "source": source, "settled": False, "venue": venue}
    if fallback_drift_pct is not None:
        d["fallback_drift_pct"] = str(fallback_drift_pct)
    return {**(plan or {}), ENTRY_DELAY_KEY: d}


def entry_delay_pending(position, now: datetime) -> bool:
    d = (position.plan or {}).get(ENTRY_DELAY_KEY) or {}
    return bool(d) and not d.get("settled") and now.timestamp() < float(d["due_ts"])


def _stream_price_at(trades: list, ts: float) -> tuple[Decimal | None, float | None]:
    """Curve price (virtual SOL / virtual tokens, raw units) after the last
    trade at or before `ts` (on-chain seconds), and that trade's time."""
    last = None
    for t in trades:
        if t.at.timestamp() <= ts and t.virtual_token and (last is None or t.at >= last.at):
            last = t
    if last is None:
        return None, None
    return Decimal(last.virtual_sol) / Decimal(last.virtual_token), last.at.timestamp()


async def _pool_prices(redis, rpc, mint: str, decision_ts: float, due_ts: float):
    """((p0, t0), (p1, t1), why) from recorded PumpSwap trades. One fresh
    read of the pool (the read position management makes anyway) proves
    the span up to landing."""
    from yonixalpha_core.solana import pumpswap

    pool = pumpswap.canonical_pool(mint)
    if rpc is not None:
        try:
            await pumpswap.recent_pool_trades(rpc, redis, pool)
        except Exception:  # noqa: BLE001 - unmeasured then; the fallback applies
            pass
    trades, seen = await pumpswap.recorded_pool_history(redis, pool)
    p0, t0, why0 = pumpswap.covered_price_at(trades, datetime.fromtimestamp(decision_ts, timezone.utc), seen)
    p1, t1, why1 = pumpswap.covered_price_at(trades, datetime.fromtimestamp(due_ts, timezone.utc), seen)
    return (p0, t0), (p1, t1), why0 or why1


async def settle_entry_delay(session: AsyncSession, redis, position, now: datetime, rpc=None) -> dict | None:
    """Once the delay has passed, re-fill the paper buy at the stream price
    at decision + latency: the same SOL buys `1 / factor` as many tokens,
    factor = price at landing / price at the decision. Moves both ways (a
    falling price fills cheaper, as it would LIVE). Without stream trades
    at both times the decision fill is kept and marked unmeasured, never
    estimated. Returns what was done, or None when nothing was due."""
    from yonixalpha_core.safety.store import add_timeline_event
    from yonixalpha_core.solana import pump_stream

    d = dict((position.plan or {}).get(ENTRY_DELAY_KEY) or {})
    if not d or d.get("settled") or now.timestamp() < float(d["due_ts"]):
        return None
    pool = d.get("venue") == "pool"
    why = None
    if pool and redis is not None and position.asset_id:
        (p0, _), (p1, _), why = await _pool_prices(redis, rpc, position.asset_id, float(d["decision_ts"]),
                                                   float(d["due_ts"]))
    else:
        trades = await pump_stream.load_trades(redis, position.asset_id) if redis is not None and position.asset_id else []
        p0, _ = _stream_price_at(trades, float(d["decision_ts"]))
        p1, _ = _stream_price_at(trades, float(d["due_ts"]))
    out: dict[str, Any] = {"latency_s": d["latency_s"], "latency_source": d.get("source"),
                           "price_source": "pumpswap pool trades" if pool else "pump_stream trades"}
    factor = None
    if p0 and p1:
        factor = p1 / p0
        out.update(factor=str(factor.quantize(Decimal("0.000001"))), measured=True,
                   price_change_pct=str(((factor - 1) * 100).quantize(Decimal("0.0001"))))
    elif d.get("fallback_drift_pct") is not None:
        # The price at landing is not proven: charge the measured LIVE buy drift
        # instead (what pool entries were charged before R5), never nothing.
        pct = Decimal(d["fallback_drift_pct"])
        factor = 1 + pct / 100
        out.update(factor=str(factor.quantize(Decimal("0.000001"))), measured=False, charged_live_drift_pct=str(pct),
                   reason=f"{why or 'no price at landing'}: measured LIVE buy drift {pct}% charged instead")
    if factor is not None:
        for attr in ("quantity", "initial_quantity", "remaining_quantity"):
            v = getattr(position, attr)
            if v is not None:
                setattr(position, attr, v / factor)
        for attr in ("entry_price", "highest_price", "lowest_price", "last_price"):
            v = getattr(position, attr)
            if v is not None:
                setattr(position, attr, v * factor)
    else:
        out.update(factor="1", measured=False,
                   reason=(why if pool and why else "no stream trade at the decision or at landing")
                   + ": decision fill kept (unmeasured)")
    d.update(settled=True, settled_at=now.isoformat(), **{k: v for k, v in out.items() if k not in ("latency_s",)})
    position.plan = {**(position.plan or {}), ENTRY_DELAY_KEY: d}
    await add_timeline_event(session, "paper_entry_delay_fill", now, out, candidate_id=position.candidate_id,
                             assessment_id=position.assessment_id, position_id=position.id)
    return out
