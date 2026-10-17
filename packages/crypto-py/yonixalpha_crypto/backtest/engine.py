"""Bar-by-bar simulation of one library module on one symbol (spot, LONG).

Execution assumptions (conservative, documented in every result):
- a signal on bar i (decided from bars <= i) fills at the OPEN of bar i+1,
  plus half the spread plus slippage; never at bar i's close;
- if bar i+1 opens at or below the planned stop, the entry is skipped;
- the size is limited to `max_participation` of the SIGNAL bar's (bar i)
  volume, the last volume known when the order is placed; a capped order is
  a PARTIAL FILL and the trade holds only what filled;
- while in a trade, each bar is checked for the stop (low) and the target
  (high). If both are inside the same bar the STOP is assumed first. A gap
  through the stop fills at the open, worse than the stop;
- stop-market exits pay slippage; a target is a limit order filled at the
  target price (no slippage);
- the module's exit rule, the time stop, trailing stops and partial exits
  are decided at bar close (trade management: private.management.decide,
  the same function the live executor uses) and executed at the next open;
- taker fees on every fill (entries, stop and market exits), maker fee on
  targets; spot has no funding (funding_bps_per_day applies when set);
- a position still open at the end of the data is closed at the last
  close and flagged `end_of_data`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from yonixalpha_crypto.private import management as mg
from yonixalpha_crypto.strategies.library import Module, Series

BPS = 10000.0


@dataclass(frozen=True)
class Costs:
    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 10.0
    spread_bps: float = 2.0  # full spread; half is paid on each fill
    slippage_bps: float = 5.0
    max_participation: float = 0.1  # share of a bar's volume an order may take
    funding_bps_per_day: float = 0.0
    min_notional: float = 5.0


@dataclass(frozen=True)
class RiskConfig:
    mode: str = "PERCENT"  # PERCENT of equity | FIXED quote amount
    value: float = 0.5  # 0.5 % of equity, or quote amount when FIXED
    max_position_quote: float = 1_000_000.0
    starting_equity: float = 10_000.0


@dataclass
class Trade:
    entry_i: int
    entry_time: str | None
    entry_px: float
    qty: float
    initial_stop: float
    target: float | None
    risk_quote: float
    partial_fill: bool
    exit_i: int | None = None
    exit_time: str | None = None
    exit_px: float | None = None
    exit_reason: str | None = None
    pnl: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0
    partials: list[dict[str, Any]] = field(default_factory=list)
    end_of_data: bool = False

    @property
    def r_multiple(self) -> float | None:
        return self.pnl / self.risk_quote if self.risk_quote > 0 else None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["r_multiple"] = self.r_multiple
        d["holding_bars"] = (self.exit_i - self.entry_i) if self.exit_i is not None else None
        return d


@dataclass
class Result:
    trades: list[Trade]
    equity: list[float]  # marked at each bar close, from start_i
    times: list[str]
    exposure_bars: int
    skipped: dict[str, int]
    notional_sum: float
    start_i: int
    end_i: int


def _t(s: Series, i: int) -> str | None:
    return s.times[i].isoformat() if s.times and i < len(s.times) else None


def simulate(s: Series, module: Module, params: dict[str, Any], exits: dict[str, Any] | None, costs: Costs,
             risk: RiskConfig, start_i: int = 0, end_i: int | None = None, bars_per_day: float = 24.0) -> Result:
    end_i = len(s) if end_i is None else min(end_i, len(s))
    plan = mg.validate_plan(exits)
    half = costs.spread_bps / 2 / BPS
    slip = costs.slippage_bps / BPS
    fee_t, fee_m = costs.taker_fee_bps / BPS, costs.maker_fee_bps / BPS
    cash = risk.starting_equity
    trades: list[Trade] = []
    equity: list[float] = []
    times: list[str] = []
    skipped: dict[str, int] = {}
    exposure, notional_sum = 0, 0.0
    pos: Trade | None = None
    stop = target = 0.0
    held = 0.0
    highest = 0.0
    pending: list[tuple[str, Any]] = []  # actions decided at a close, executed at the next open
    tick = Decimal("0.00000001")
    needs_bars = plan["trailing"]["method"] in ("atr", "swing")  # only these read recent bars

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    def sell(i: int, ref: float, qty: float, fee: float, why: str, slipped: bool) -> None:
        """Sell `qty` against the reference price `ref` (open, stop or close);
        market exits pay half the spread and the slippage, a target does not."""
        nonlocal cash, held, pos
        px = ref * (1 - half - slip) if slipped else ref
        proceeds = qty * px
        f = proceeds * fee
        cash += proceeds - f
        pos.fees += f
        if slipped:
            pos.slippage += qty * ref * slip
        cost = pos.entry_px * qty
        pos.pnl += proceeds - f - cost
        held -= qty
        if held <= 1e-12:
            pos.exit_i, pos.exit_time, pos.exit_reason = i, _t(s, i), why
            sold = qty + sum(p["qty"] for p in pos.partials)
            pos.exit_px = (sum(p["px"] * p["qty"] for p in pos.partials) + px * qty) / sold if sold else px
            trades.append(pos)
            pos = None

    i = start_i
    while i < end_i:
        o, h, lo, c = s.o[i], s.h[i], s.l[i], s.c[i]
        # 1) execute what was decided at the previous close, at this open
        if pos is not None and pending:
            for act, arg in pending:
                if pos is None:
                    break
                if act == "close":
                    sell(i, o, held, fee_t, arg, True)
                elif act == "partial":
                    q = held * arg[0]
                    if q * o >= costs.min_notional and (held - q) * o >= costs.min_notional:
                        px = o * (1 - half - slip)
                        pos.partials.append({"i": i, "qty": q, "px": px, "reason": arg[1]})
                        cash += q * px * (1 - fee_t)
                        pos.fees += q * px * fee_t
                        pos.slippage += q * o * slip
                        pos.pnl += q * px * (1 - fee_t) - pos.entry_px * q
                        held -= q
                elif act == "stop":
                    stop = max(stop, arg)
            pending = []
        elif pos is None and pending:
            act, arg = pending[0]
            pending = []
            if act == "enter":
                e = arg
                px = o * (1 + half + slip)
                stop_fill = e.stop * (1 - half - slip)  # how the stop will actually fill
                if o <= e.stop:
                    skip("next open at or below the stop")
                else:
                    equity_now = cash
                    budget = equity_now * risk.value / 100 if risk.mode == "PERCENT" else risk.value
                    per_unit = (px - stop_fill) + px * fee_t + stop_fill * fee_t
                    qty = budget / per_unit if per_unit > 0 else 0.0
                    qty = min(qty, risk.max_position_quote / px, cash / (px * (1 + fee_t)))
                    cap = costs.max_participation * s.v[i - 1]  # the signal bar: no look-ahead on volume
                    partial = qty > cap
                    qty = min(qty, cap)
                    if qty * px < costs.min_notional:
                        skip("size below the minimum order value")
                    else:
                        f = qty * px * fee_t
                        cash -= qty * px + f
                        notional_sum += qty * px
                        pos = Trade(i, _t(s, i), px, qty, e.stop, e.target, qty * per_unit, partial, fees=f,
                                    slippage=qty * o * slip, pnl=-f)  # the entry fee is part of the trade's P&L
                        held, stop, target, highest = qty, e.stop, e.target or 0.0, px
        # 2) intrabar stop / target on this bar (the entry bar included: it opened above the stop)
        if pos is not None:
            hit_stop = lo <= stop
            hit_target = target > 0 and h >= target
            if hit_stop:  # stop first when both are inside the bar
                why = "trailing stop" if stop > pos.initial_stop else "stop (gap through it)" if o < stop else "stop"
                sell(i, min(o, stop), held, fee_t, why, True)
            elif hit_target:
                sell(i, max(o, target), held, fee_m, "target", False)
        # 3) at the close: mark, then decide for the next open
        if pos is not None:
            exposure += 1
            highest = max(highest, h)
            if costs.funding_bps_per_day:
                fnd = held * c * costs.funding_bps_per_day / BPS / bars_per_day
                cash -= fnd
                pos.funding += fnd
                pos.pnl -= fnd
            why = module.should_exit(s, i, params)
            if why:
                pending.append(("close", why))
            else:
                snap = mg.TradeSnapshot(Decimal(str(pos.entry_px)), Decimal(str(pos.initial_stop)), Decimal(str(stop)),
                                        Decimal(str(held)), None, Decimal(str(highest)), i - pos.entry_i, plan,
                                        Decimal(str(costs.taker_fee_bps)))
                bars = [mg.Bar(Decimal(str(s.h[k])), Decimal(str(s.l[k])), Decimal(str(s.c[k])))
                        for k in range(max(0, i - 30), i + 1)] if needs_bars else []
                for d in mg.decide(snap, mg.MarketSnapshot(Decimal(str(c)), Decimal(str(c)), bars), tick):
                    if d.action == mg.CLOSE:
                        pending = [("close", d.reason)]
                        break
                    if d.action == mg.PARTIAL_EXIT:
                        pending.append(("partial", (float(d.detail["fraction"]), d.reason)))
                        parts = [dict(x) for x in plan["partials"]]
                        parts[int(d.detail["index"])]["done"] = True
                        plan = {**plan, "partials": parts}
                    if d.action == mg.MOVE_STOP:
                        pending.append(("stop", float(d.detail["new_stop"])))
        elif i + 1 < end_i:
            e, _ = module.decide(s, i, params)
            if e is not None:
                pending = [("enter", e)]
        equity.append(cash + (held * c if pos is not None else 0.0))
        times.append(_t(s, i) or str(i))
        i += 1
        if pos is None and not pending:
            plan = mg.validate_plan(exits)  # fresh partial flags for the next trade
    if pos is not None:
        sell(end_i - 1, s.c[end_i - 1], held, fee_t, "end of data", True)
        trades[-1].end_of_data = True
        equity[-1] = cash
    return Result(trades, equity, times, exposure, skipped, notional_sum, start_i, end_i)
