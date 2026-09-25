"""Hyperliquid Grid — port of the grid math and circuit breakers in
yonixnetwork/hyperliquid-grid-trading-bot (src/grid.py, src/risk.py,
src/state.py), run as a PAPER simulation against live Hyperliquid mid
prices. Nothing is sent to Hyperliquid.

Unchanged from the repository:
- range: auto = mid ± range_pct%, or manual lower/upper;
- levels: grid_levels + 1 evenly spaced prices; size per level =
  capital * leverage / grid_levels / range midpoint;
- initial orders: buys below mid, sells above, skipping the level within
  half a spacing of mid;
- a filled buy is replaced by a sell one spacing above, and vice versa;
- weighted-average-entry position accounting, realized PnL on reductions;
- circuit breakers: pause at max_drawdown_pct, or when mid leaves the range
  by range_break_pct; flatten_on_pause closes the position.

Additions for safety: the worst-case loss (every buy filled, then the range
breaks downward, or every sell filled then it breaks upward) is computed
before start and must fit the risk budget; leverage is capped by the gate's
max_leverage; fills are simulated as maker (post-only) at the level price
with the configured maker fee, which assumes the resting order would have
been filled when price traded through it — an optimistic assumption stated
here and in the UI.
"""

from dataclasses import asdict, dataclass, field
from decimal import Decimal

NAME = "hyperliquid_grid"
VERSION = "1"
DEFAULTS = {
    "coin": "BTC",
    "range_mode": "auto",
    "range_pct": "0.5",
    "range_lower": None,
    "range_upper": None,
    "grid_levels": 7,
    "capital": "15",
    "leverage": "1",
    "max_drawdown_pct": "15",
    "range_break_pct": "1.5",
    "flatten_on_pause": True,
    "maker_fee_bps": "1.5",
    "taker_fee_bps": "4.5",
}
ZERO = Decimal(0)
# Operator start/stop requests from the dashboard, consumed by the paper grid
# loop on its next tick (it holds the live price the start/stop needs).
COMMAND_KEY = "yx:grid:cmd"


@dataclass
class GridOrder:
    price: Decimal
    size: Decimal
    is_buy: bool


@dataclass
class GridState:
    range_lower: Decimal
    range_upper: Decimal
    spacing: Decimal
    levels: list[Decimal]
    size_per_level: Decimal
    orders: list[GridOrder]
    starting_equity: Decimal
    net_position: Decimal = ZERO  # + long, - short
    avg_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    fills: int = 0
    mid: Decimal = ZERO
    peak_equity: Decimal = ZERO
    paused: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def unrealized(self) -> Decimal:
        if self.net_position == 0 or self.avg_entry == 0:
            return ZERO
        return (self.mid - self.avg_entry) * self.net_position

    @property
    def equity(self) -> Decimal:
        return self.starting_equity + self.realized_pnl - self.fees + self.unrealized

    @property
    def drawdown_pct(self) -> Decimal:
        peak = max(self.peak_equity, self.starting_equity)
        return (peak - self.equity) / peak * 100 if peak > 0 else ZERO

    def to_json(self) -> dict:
        d = asdict(self)
        return _stringify(d)


def _stringify(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _stringify(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_stringify(x) for x in v]
    return v


def from_json(d: dict) -> GridState:
    D = Decimal
    return GridState(
        range_lower=D(d["range_lower"]), range_upper=D(d["range_upper"]), spacing=D(d["spacing"]),
        levels=[D(x) for x in d["levels"]], size_per_level=D(d["size_per_level"]),
        orders=[GridOrder(D(o["price"]), D(o["size"]), bool(o["is_buy"])) for o in d["orders"]],
        starting_equity=D(d["starting_equity"]), net_position=D(d["net_position"]), avg_entry=D(d["avg_entry"]),
        realized_pnl=D(d["realized_pnl"]), fees=D(d["fees"]), fills=int(d["fills"]), mid=D(d["mid"]),
        peak_equity=D(d["peak_equity"]), paused=d.get("paused"), history=list(d.get("history") or [])[-200:],
    )


def build(params: dict, mid: Decimal, starting_equity: Decimal, max_leverage: Decimal) -> GridState:
    p = {**DEFAULTS, **params}
    levels_n = int(p["grid_levels"])
    if levels_n < 2:
        raise ValueError("grid_levels must be at least 2")
    if p["range_mode"] == "auto":
        pct = Decimal(str(p["range_pct"])) / 100
        lower, upper = mid * (1 - pct), mid * (1 + pct)
    else:
        lower, upper = Decimal(str(p["range_lower"])), Decimal(str(p["range_upper"]))
    if not (0 < lower < upper):
        raise ValueError("invalid grid range")
    spacing = (upper - lower) / levels_n
    levels = [lower + spacing * i for i in range(levels_n + 1)]
    leverage = min(Decimal(str(p["leverage"])), max_leverage)
    capital = Decimal(str(p["capital"]))
    size = capital * leverage / levels_n / ((lower + upper) / 2)
    orders = [GridOrder(lv, size, lv < mid) for lv in levels if abs(lv - mid) >= spacing / 2]
    return GridState(lower, upper, spacing, levels, size, orders, starting_equity, mid=mid, peak_equity=starting_equity)


def worst_case_loss(state: GridState, params: dict) -> Decimal:
    """Loss if every order on one side fills and price then runs to the
    range-break level on that side, where the breaker flattens with a taker
    fee. The larger of the two sides."""
    p = {**DEFAULTS, **params}
    brk = Decimal(str(p["range_break_pct"])) / 100
    taker = Decimal(str(p["taker_fee_bps"])) / 10000
    maker = Decimal(str(p["maker_fee_bps"])) / 10000
    buys = [o for o in state.orders if o.is_buy]
    sells = [o for o in state.orders if not o.is_buy]
    down_exit = state.range_lower * (1 - brk)
    up_exit = state.range_upper * (1 + brk)
    long_loss = sum(((o.price - down_exit) * o.size + o.price * o.size * maker for o in buys), ZERO)
    long_loss += sum((o.size for o in buys), ZERO) * down_exit * taker
    short_loss = sum(((up_exit - o.price) * o.size + o.price * o.size * maker for o in sells), ZERO)
    short_loss += sum((o.size for o in sells), ZERO) * up_exit * taker
    return max(long_loss, short_loss)


def _record_fill(s: GridState, is_buy: bool, price: Decimal, size: Decimal, fee: Decimal) -> None:
    signed = size if is_buy else -size
    s.fees += fee
    s.fills += 1
    if s.net_position == 0 or (s.net_position > 0) == is_buy:
        total = abs(s.net_position) + size
        s.avg_entry = (s.avg_entry * abs(s.net_position) + price * size) / total
        s.net_position += signed
        return
    closing = min(size, abs(s.net_position))
    direction = (price - s.avg_entry) if s.net_position > 0 else (s.avg_entry - price)
    s.realized_pnl += direction * closing
    s.net_position += signed
    if s.net_position == 0:
        s.avg_entry = ZERO
    elif (s.net_position > 0) == is_buy:  # flipped through zero
        s.avg_entry = price


def step(s: GridState, mid: Decimal, params: dict) -> list[dict]:
    """Advances the simulation to a new mid price. Returns fill/pause events."""
    p = {**DEFAULTS, **params}
    events: list[dict] = []
    if s.paused:
        s.mid = mid
        return events
    maker = Decimal(str(p["maker_fee_bps"])) / 10000
    remaining: list[GridOrder] = []
    new_orders: list[GridOrder] = []
    for o in sorted(s.orders, key=lambda x: x.price, reverse=True):
        crossed = (o.is_buy and mid <= o.price) or (not o.is_buy and mid >= o.price)
        if not crossed:
            remaining.append(o)
            continue
        fee = o.price * o.size * maker
        _record_fill(s, o.is_buy, o.price, o.size, fee)
        events.append({"type": "fill", "side": "BUY" if o.is_buy else "SELL", "price": str(o.price), "size": str(o.size),
                       "fee": str(fee)})
        replacement = GridOrder(o.price + s.spacing, o.size, False) if o.is_buy else GridOrder(o.price - s.spacing, o.size, True)
        new_orders.append(replacement)
    s.orders = remaining + new_orders
    s.mid = mid
    s.peak_equity = max(s.peak_equity, s.equity)

    brk = Decimal(str(p["range_break_pct"])) / 100
    reason = None
    if s.drawdown_pct >= Decimal(str(p["max_drawdown_pct"])):
        reason = "pause_drawdown"
    elif mid < s.range_lower * (1 - brk) or mid > s.range_upper * (1 + brk):
        reason = "pause_range_break"
    if reason:
        s.paused = reason
        events.append({"type": "pause", "reason": reason, "mid": str(mid), "equity": str(s.equity)})
        if p["flatten_on_pause"] and s.net_position != 0:
            taker = Decimal(str(p["taker_fee_bps"])) / 10000
            size = abs(s.net_position)
            _record_fill(s, s.net_position < 0, mid, size, mid * size * taker)
            events.append({"type": "flatten", "price": str(mid), "size": str(size)})
        s.orders = []
    s.history = (s.history + [{"t": len(s.history), **e} for e in events])[-200:]
    return events


def session_summary(status: str, data: dict) -> dict:
    """Operator-facing summary of a stored grid session (strategy_states row)."""
    out: dict = {"status": status, "reason": data.get("reason") or data.get("stopped_reason") or None,
                 "reserved": data.get("reserved"), "worst_case_loss": data.get("worst_case_loss"),
                 "started_at": data.get("started_at"), "updated": data.get("updated"), "last_mid": data.get("last_mid")}
    if "grid" in data:
        s = from_json(data["grid"])
        equity = Decimal(data["final_equity"]) if data.get("final_equity") else s.equity
        out.update(equity=str(equity), realized_pnl=str(s.realized_pnl), fees=str(s.fees), fills=s.fills,
                   net_position=str(s.net_position), unrealized=str(s.unrealized), drawdown_pct=str(s.drawdown_pct),
                   range_lower=str(s.range_lower), range_upper=str(s.range_upper), levels=len(s.levels),
                   paused=s.paused, pnl=str(equity - s.starting_equity))
    return out
