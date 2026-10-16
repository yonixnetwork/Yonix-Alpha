"""Trade management decisions for an open LONG spot trade. Pure: it reads a
snapshot of the trade and the market and returns decisions with reasons;
the executor carries them out and records each one.

Rules that always hold:
- a stop only moves UP (it never widens the approved loss) and always stays
  below the bid by at least `min_stop_gap_bps`;
- partial exits only reduce exposure; the remainder keeps a stop;
- an early exit needs a stated reason: the strategy's own exit or
  invalidation, the time stop, or the engine-managed target;
- a single indicator wobble is not a reason: only the strategy's defined
  exit / invalidation (passed in as `strategy_exit`) closes a trade;
- nothing here predicts price; decisions state the evidence they used.

Plan (exec_trades.plan, validated by `validate_plan`):
  partials:  [{"at_r": 1.0, "fraction": 0.5, "stop_to": "breakeven" | null}]
  trailing:  {"method": "none" | "percent" | "atr" | "swing", "activate_r": 1.0,
              "percent": 1.0, "atr_mult": 2.0, "swing_bars": 5}
  time_stop_bars: int | null   (close if still below +0.5 R after N closed bars)
  engine_take_profit: bool     (target not held by the exchange: sell at market when the bid reaches it)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

HOLD, MOVE_STOP, PARTIAL_EXIT, CLOSE, ALERT = "HOLD", "MOVE_STOP", "PARTIAL_EXIT", "CLOSE", "ALERT"
BPS = Decimal(10000)
METHODS = ("none", "percent", "atr", "swing")


class PlanError(ValueError):
    pass


def validate_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    plan = dict(plan or {})
    out: dict[str, Any] = {"partials": [], "trailing": {"method": "none"}, "time_stop_bars": None,
                           "engine_take_profit": bool(plan.get("engine_take_profit", False)),
                           "min_stop_gap_bps": "20"}
    total = Decimal(0)
    for p in plan.get("partials") or []:
        at_r, frac = Decimal(str(p.get("at_r"))), Decimal(str(p.get("fraction")))
        if not (Decimal("0.25") <= at_r <= Decimal(20)) or not (Decimal("0.05") <= frac <= Decimal("0.9")):
            raise PlanError("partials: at_r 0.25-20 and fraction 0.05-0.9")
        if p.get("stop_to") not in (None, "breakeven"):
            raise PlanError("partials.stop_to: null or 'breakeven'")
        total += frac
        out["partials"].append({"at_r": str(at_r), "fraction": str(frac), "stop_to": p.get("stop_to"), "done": False})
    if total >= 1:
        raise PlanError("partials together must leave part of the position")
    t = dict(plan.get("trailing") or {"method": "none"})
    if t.get("method", "none") not in METHODS:
        raise PlanError(f"trailing.method: one of {METHODS}")
    tr = {"method": t.get("method", "none"), "activate_r": str(Decimal(str(t.get("activate_r", 1))))}
    if tr["method"] == "percent":
        tr["percent"] = str(Decimal(str(t.get("percent", 1))))
        if not Decimal("0.1") <= Decimal(tr["percent"]) <= Decimal(20):
            raise PlanError("trailing.percent: 0.1-20")
    if tr["method"] == "atr":
        tr["atr_mult"] = str(Decimal(str(t.get("atr_mult", 2))))
        if not Decimal("0.5") <= Decimal(tr["atr_mult"]) <= Decimal(10):
            raise PlanError("trailing.atr_mult: 0.5-10")
    if tr["method"] == "swing":
        tr["swing_bars"] = int(t.get("swing_bars", 5))
        if not 2 <= tr["swing_bars"] <= 50:
            raise PlanError("trailing.swing_bars: 2-50")
    out["trailing"] = tr
    if plan.get("time_stop_bars") is not None:
        n = int(plan["time_stop_bars"])
        if not 1 <= n <= 1000:
            raise PlanError("time_stop_bars: 1-1000")
        out["time_stop_bars"] = n
    gap = Decimal(str(plan.get("min_stop_gap_bps", "20")))
    if not Decimal(1) <= gap <= Decimal(500):
        raise PlanError("min_stop_gap_bps: 1-500")
    out["min_stop_gap_bps"] = str(gap)
    return out


@dataclass(frozen=True)
class Bar:
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class TradeSnapshot:
    entry: Decimal
    initial_stop: Decimal
    stop: Decimal
    qty: Decimal
    take_profit: Decimal | None
    highest: Decimal
    bars_held: int
    plan: dict[str, Any]
    fee_bps: Decimal = Decimal(0)


@dataclass
class MarketSnapshot:
    bid: Decimal
    ask: Decimal
    bars: list[Bar] = field(default_factory=list)  # closed bars, oldest first
    strategy_exit: str | None = None  # the strategy's own exit / invalidation reason
    max_spread_bps: Decimal | None = None


@dataclass
class Decision:
    action: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def atr(bars: list[Bar], n: int = 14) -> Decimal | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / n


def _floor(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def decide(t: TradeSnapshot, m: MarketSnapshot, tick: Decimal) -> list[Decision]:
    risk = t.entry - t.initial_stop
    if risk <= 0 or m.bid <= 0:
        return [Decision(ALERT, "risk or price unknown: no management decision possible",
                         {"entry": str(t.entry), "initial_stop": str(t.initial_stop), "bid": str(m.bid)})]
    r_now = (m.bid - t.entry) / risk
    ev = {"bid": str(m.bid), "r": f"{r_now:.2f}", "stop": str(t.stop), "highest": str(t.highest)}
    out: list[Decision] = []

    if m.bid <= t.stop:
        return [Decision(ALERT, "bid at or below the stop: the exchange-side stop should be executing; "
                                "the reconciler confirms the fill", ev)]
    if m.strategy_exit:
        return [Decision(CLOSE, f"strategy exit / invalidation: {m.strategy_exit}", ev)]
    if t.plan.get("engine_take_profit") and t.take_profit is not None and m.bid >= t.take_profit:
        return [Decision(CLOSE, f"target {t.take_profit} reached (engine-managed target)", ev)]
    n = t.plan.get("time_stop_bars")
    if n and t.bars_held >= n and r_now < Decimal("0.5"):
        return [Decision(CLOSE, f"time stop: {t.bars_held} closed bars and only {r_now:.2f} R", ev)]

    new_stop = t.stop
    why: list[str] = []
    for i, p in enumerate(t.plan.get("partials") or []):
        if p.get("done") or r_now < Decimal(p["at_r"]):
            continue
        out.append(Decision(PARTIAL_EXIT, f"partial target {p['at_r']} R reached ({r_now:.2f} R): sell {p['fraction']}",
                            {**ev, "index": i, "fraction": p["fraction"]}))
        if p.get("stop_to") == "breakeven":
            be = t.entry * (1 + 2 * t.fee_bps / BPS)
            if be > new_stop:
                new_stop, _ = be, why.append(f"breakeven after the {p['at_r']} R partial")

    tr = t.plan.get("trailing") or {}
    method = tr.get("method", "none")
    if method != "none" and r_now >= Decimal(tr.get("activate_r", "1")):
        cand = None
        if method == "percent":
            cand = t.highest * (1 - Decimal(tr["percent"]) / 100)
            label = f"{tr['percent']}% below the high {t.highest}"
        elif method == "atr":
            a = atr(m.bars)
            if a is None:
                out.append(Decision(HOLD, "ATR trailing waits for 15 closed bars", ev))
            else:
                cand = t.highest - Decimal(tr["atr_mult"]) * a
                label = f"{tr['atr_mult']} x ATR {a:.8f} below the high {t.highest}"
        elif method == "swing":
            k = int(tr["swing_bars"])
            if len(m.bars) >= k:
                cand = min(b.low for b in m.bars[-k:])
                label = f"lowest low of the last {k} closed bars"
        if cand is not None and cand > new_stop:
            new_stop, _ = cand, why.append(f"trailing: {label}")

    if new_stop > t.stop:
        ceiling = m.bid * (1 - Decimal(t.plan.get("min_stop_gap_bps", "20")) / BPS)
        new_stop = _floor(min(new_stop, ceiling), tick)
        if new_stop > t.stop:
            out.append(Decision(MOVE_STOP, "; ".join(why) + f" -> stop {t.stop} to {new_stop}",
                                {**ev, "new_stop": str(new_stop)}))
    if m.max_spread_bps is not None and m.ask > 0:
        spread = (m.ask - m.bid) / ((m.ask + m.bid) / 2) * BPS
        if spread > m.max_spread_bps:
            out.append(Decision(ALERT, f"spread {spread:.1f} bps above {m.max_spread_bps}: exits may slip", ev))
    return out or [Decision(HOLD, f"thesis intact: {r_now:.2f} R, stop {t.stop}, no exit condition met", ev)]
