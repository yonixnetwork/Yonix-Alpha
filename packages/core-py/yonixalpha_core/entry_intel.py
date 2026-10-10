"""Early-entry intelligence for Pump.fun bonding-curve tokens: an
early-momentum feature engine, a momentum phase classifier and three
competing entry strategies, all evaluated in SHADOW.

Why this exists (docs/EARLY_ENTRY_ROOT_CAUSE_AUDIT.md): the existing path
promotes a token after a fixed observation window, re-evaluates it on a
fixed cadence and qualifies it with a "price up since the window start"
rule. A token can therefore be judged promising after its first move is
over. The functions here look at the move while it develops (real SOL
inflow, its acceleration, independent buyer growth, seller growth, price
displacement from the first observable price) and say, at every moment,
which of three strategies would want the token, which would WAIT and which
would refuse it, and why.

Strategies (each a separate candidate with its own record; none buys):
  EARLY_ACCELERATION        demand developing now: positive and rising real
                            inflow, rising trade activity, independent
                            buyer growth, price supported by inflow.
  SMART_WALLET_CONFIRMATION historically evaluated wallets entering. Never a
                            trigger on its own: it only confirms a candidate
                            EARLY_ACCELERATION or MOMENTUM_CONTINUATION
                            already produced.
  MOMENTUM_CONTINUATION     an older token whose demand is strengthening
                            again (healthy pullback and recovery, continued
                            net buying), not one that is merely up a lot.

Every input is a trade at or before the decision time `t` (on-chain block
time, second resolution). Prices are lamports per raw token unit
(decimals-free), as in the opportunity ledger. A value that cannot be
measured is None and the reason says why. Nothing here touches the safety
gate: a CANDIDATE is a candidate, never an approval (see
safety/gate.py for the order DATA QUALITY -> TOKEN SAFETY -> SELLABILITY ->
LIQUIDITY -> EXECUTION -> RISK -> STRATEGY -> ML).
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any

from yonixalpha_core.solana.flow import Trade, synchronized_buy_cluster
from yonixalpha_core.solana.launch_features import INITIAL_REAL_TOKENS, REAL_TOKEN_OFFSET, STANDARD_VSOL0

FEATURE_VERSION = "early-2026.10.1"
LAMPORTS = 1_000_000_000

EARLY_ACCELERATION = "EARLY_ACCELERATION"
SMART_WALLET_CONFIRMATION = "SMART_WALLET_CONFIRMATION"
MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
STRATEGIES = (EARLY_ACCELERATION, SMART_WALLET_CONFIRMATION, MOMENTUM_CONTINUATION)

# Baselines recorded through the same labeller, so every strategy is compared
# with the existing pipeline on identical terms (same stream, same costs).
CURRENT_PROMOTE = "CURRENT_PROMOTE"  # the observation funnel promoted the token
CURRENT_GATE_ENTRY = "CURRENT_GATE_ENTRY"  # the safety gate approved an entry (paper or live)
NAIVE_SAMPLE = "NAIVE_SAMPLE"  # a deterministic sample of launches entered at their 10th trade
BASELINES = (CURRENT_PROMOTE, CURRENT_GATE_ENTRY, NAIVE_SAMPLE)

# Migrated-token entry variants (PumpSwap), recorded from pool reserve samples.
MIGRATED_IMMEDIATE = "MIGRATED_IMMEDIATE"
MIGRATED_DELAYED_CONFIRMATION = "MIGRATED_DELAYED_CONFIRMATION"
MIGRATED_PULLBACK = "MIGRATED_PULLBACK"
MIGRATED_CONTINUATION = "MIGRATED_CONTINUATION"
MIGRATED_NO_TRADE = "MIGRATED_NO_TRADE"  # the baseline: a row with no entry, outcome = 0 by definition
MIGRATED_VARIANTS = (MIGRATED_IMMEDIATE, MIGRATED_DELAYED_CONFIRMATION, MIGRATED_PULLBACK, MIGRATED_CONTINUATION,
                     MIGRATED_NO_TRADE)
ALL_RECORDED = STRATEGIES + BASELINES + MIGRATED_VARIANTS

CANDIDATE, WAIT, NO_TRADE = "CANDIDATE", "WAIT", "NO_TRADE"

# Momentum phases.
EARLY_ACCEL_PHASE = "EARLY_ACCELERATION"
HEALTHY_CONTINUATION = "HEALTHY_CONTINUATION"
EXHAUSTED = "EXHAUSTED"
DISTRIBUTION = "DISTRIBUTION"
UNCLEAR = "UNCLEAR"
PHASES = (EARLY_ACCEL_PHASE, HEALTHY_CONTINUATION, EXHAUSTED, DISTRIBUTION, UNCLEAR)

LIMITED, MODERATE, STRONG = "LIMITED", "MODERATE", "STRONG"

SHORT_WINDOW = 10  # seconds: "now" vs the 10 s before
MID_WINDOW = 30
LONG_WINDOW = 60
TINY_BUY_SOL = 0.01
MEANINGFUL_BUY_SOL = 0.05
LARGE_BUY_SOL = 0.5
DUMP_WITHIN_SECONDS = 30
DUMP_SOLD_SHARE = 0.5
EARLY_MAX_PULLBACK_PCT = 8.0  # a token that already pulled back this much is past its first move


@dataclass
class EntryConfig:
    """Thresholds of the three strategies. Stored as the platform setting
    `entry_intelligence` (dashboard, audited); defaults are starting points
    for the shadow comparison, not validated values."""

    # EARLY_ACCELERATION
    ea_max_age_seconds: int = 180
    ea_min_trades: int = 6
    ea_min_meaningful_buyers: int = 3
    ea_min_net_inflow_sol: float = 0.5  # last 30 s
    ea_max_displacement_pct: float = 120.0  # price vs first observable price
    # MOMENTUM_CONTINUATION
    mc_min_age_seconds: int = 60
    mc_min_net_inflow_sol: float = 0.5  # last 60 s
    mc_min_new_buyers: int = 3  # last 60 s
    mc_max_pullback_pct: float = 20.0  # below the running peak
    mc_max_displacement_pct: float = 400.0
    # SMART_WALLET_CONFIRMATION
    sw_min_proven_wallets: int = 1
    sw_entry_within_seconds: int = 90
    # Shared
    max_round_trip_cost_pct: float = 8.0  # fees + impact + fixed costs at the reference size
    reference_size_sol: float = 0.05
    fee_bps: int = 125  # used only when the stream has not reported the token's fee yet
    fixed_cost_sol: float = 0.000225
    max_tiny_trade_share: float = 0.5
    max_window_top_buyer_share: float = 0.7
    max_sync_cluster: int = 4
    naive_sample_every: int = 50  # 1 launch in N is recorded as the NAIVE_SAMPLE baseline
    # Event-driven gate re-evaluation (gate_events): timing only, no rule changes.
    event_reevaluation: bool = True
    event_min_interval_seconds: int = 10

    @classmethod
    def from_dict(cls, raw: dict | None) -> "EntryConfig":
        out = cls()
        for f in fields(cls):
            if raw and f.name in raw and raw[f.name] is not None:
                v = raw[f.name]
                if isinstance(getattr(out, f.name), bool):
                    setattr(out, f.name, v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"))
                    continue
                try:
                    setattr(out, f.name, type(getattr(out, f.name))(v))
                except (TypeError, ValueError):
                    pass
        return out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_config(raw: dict) -> list[str]:
    """Errors for an operator edit; empty when valid."""
    errors: list[str] = []
    cfg = EntryConfig.from_dict(raw)
    known = {f.name for f in fields(EntryConfig)}
    for k in raw:
        if k not in known:
            errors.append(f"unknown setting {k}")
    if not 10 <= cfg.ea_max_age_seconds <= 1800:
        errors.append("ea_max_age_seconds must be 10-1800")
    if not 1 <= cfg.ea_min_trades <= 500 or not 1 <= cfg.ea_min_meaningful_buyers <= 200:
        errors.append("ea_min_trades 1-500 and ea_min_meaningful_buyers 1-200")
    if not 0 < cfg.ea_min_net_inflow_sol <= 100 or not 0 < cfg.mc_min_net_inflow_sol <= 100:
        errors.append("minimum inflows must be > 0 and <= 100 SOL")
    if not 10 <= cfg.ea_max_displacement_pct <= 5000 or not 10 <= cfg.mc_max_displacement_pct <= 10000:
        errors.append("displacement limits out of range")
    if not 1 <= cfg.mc_max_pullback_pct <= 90:
        errors.append("mc_max_pullback_pct must be 1-90")
    if not 0.5 <= cfg.max_round_trip_cost_pct <= 50:
        errors.append("max_round_trip_cost_pct must be 0.5-50")
    if not 0 < cfg.reference_size_sol <= 10:
        errors.append("reference_size_sol must be > 0 and <= 10")
    if not 2 <= cfg.naive_sample_every <= 10000:
        errors.append("naive_sample_every must be 2-10000")
    if not 3 <= cfg.event_min_interval_seconds <= 300:
        errors.append("event_min_interval_seconds must be 3-300")
    return errors


# --- features -------------------------------------------------------------------------------

def _sol(lamports: float) -> float:
    return lamports / LAMPORTS


def _p(t: Trade) -> float | None:
    return t.virtual_sol / t.virtual_token if t.virtual_token else None


def _between(trades: list[Trade], start: datetime, end: datetime) -> list[Trade]:
    return [x for x in trades if start < x.at <= end]


def _last_before(trades: list[Trade], at: datetime) -> Trade | None:
    best = None
    for x in trades:
        if x.at <= at:
            best = x
        else:
            break
    return best


def _window(trades: list[Trade], end: datetime, seconds: int, seen_buyers: set[str], seen_sellers: set[str]) -> dict[str, Any]:
    w = _between(trades, end - timedelta(seconds=seconds), end)
    buys = [x for x in w if x.is_buy]
    sells = [x for x in w if not x.is_buy]
    buy_sol = _sol(sum(x.sol_lamports for x in buys))
    sell_sol = _sol(sum(x.sol_lamports for x in sells))
    start = _last_before(trades, end - timedelta(seconds=seconds))
    p0 = _p(start) if start is not None else (_p(w[0]) if w else None)
    p1 = _p(w[-1]) if w else (p0 if start is not None else None)
    by_wallet: dict[str, int] = {}
    for b in buys:
        by_wallet[b.trader] = by_wallet.get(b.trader, 0) + b.sol_lamports
    total_buy = sum(by_wallet.values())
    return {
        "trades": len(w), "buys": len(buys), "sells": len(sells),
        "buy_sol": round(buy_sol, 6), "sell_sol": round(sell_sol, 6), "net_sol": round(buy_sol - sell_sol, 6),
        "new_buyers": len({x.trader for x in buys} - seen_buyers),
        "new_sellers": len({x.trader for x in sells} - seen_sellers),
        "price_ret_pct": round((p1 / p0 - 1) * 100, 3) if p0 and p1 else None,
        "top_buyer_share": round(max(by_wallet.values()) / total_buy, 4) if total_buy else None,
        "max_buy_sol": round(_sol(max((x.sol_lamports for x in buys), default=0)), 6),
    }


def _seen_before(trades: list[Trade], at: datetime) -> tuple[set[str], set[str]]:
    held = [x for x in trades if x.at <= at]
    return {x.trader for x in held if x.is_buy}, {x.trader for x in held if not x.is_buy}


def round_trip_cost_pct(trades: list[Trade], t: datetime, size_sol: float, fee_bps: int, fixed_cost_sol: float) -> float | None:
    """Fees on both legs + curve price impact of buying and immediately
    selling `size_sol` at the state at `t` + fixed network costs, in percent
    of `size_sol`. None without a curve state."""
    last = _last_before(trades, t)
    if last is None or not last.virtual_token or not last.virtual_sol:
        return None
    fee = fee_bps / 10_000
    size = size_sol * LAMPORTS
    net_in = size / (1 + fee)
    k = last.virtual_sol * last.virtual_token
    tokens = last.virtual_token - k / (last.virtual_sol + net_in)
    if tokens <= 0:
        return None
    vs1, vt1 = last.virtual_sol + net_in, last.virtual_token - tokens
    gross_out = vs1 - (vs1 * vt1) / (vt1 + tokens)
    net_out = gross_out * (1 - fee)
    return round((size - net_out + fixed_cost_sol * LAMPORTS) / size * 100, 4)


def early_features(trades: list[Trade], t: datetime, *, created_ts: int | None, creator: str | None = None,
                   complete_history: bool | None = None, fee_bps: int | None = None,
                   cfg: EntryConfig | None = None, stream_heartbeat: datetime | None = None) -> dict[str, Any]:
    """The early-momentum state of a bonding-curve token at `t`, from trades
    at or before `t` only."""
    cfg = cfg or EntryConfig()
    held = sorted((x for x in trades if x.at <= t), key=lambda x: x.at)
    created = datetime.fromtimestamp(created_ts, tz=t.tzinfo) if created_ts else None
    f: dict[str, Any] = {"feature_version": FEATURE_VERSION, "at": t.isoformat(), "unknown": {},
                         "age_seconds": round((t - created).total_seconds(), 1) if created else None,
                         "trades_total": len(held), "complete_history": complete_history}
    if created is None:
        f["unknown"]["age_seconds"] = "creation time unknown"
    if stream_heartbeat is not None:
        f["stream_age_seconds"] = round((t - stream_heartbeat).total_seconds(), 1)
    if not held:
        f["unknown"]["price"] = "no trade yet"
        return f
    buys = [x for x in held if x.is_buy]
    sells = [x for x in held if not x.is_buy]
    buy_sol = _sol(sum(x.sol_lamports for x in buys))
    sell_sol = _sol(sum(x.sol_lamports for x in sells))
    f.update({"buys_total": len(buys), "sells_total": len(sells),
              "unique_buyers": len({x.trader for x in buys}), "unique_sellers": len({x.trader for x in sells}),
              "buy_sol_total": round(buy_sol, 6), "sell_sol_total": round(sell_sol, 6),
              "net_inflow_sol_total": round(buy_sol - sell_sol, 6),
              "last_trade_age_seconds": round((t - held[-1].at).total_seconds(), 1)})

    # Windows: now vs the window before (rates and acceleration).
    sb, ss = _seen_before(held, t - timedelta(seconds=SHORT_WINDOW))
    cur = _window(held, t, SHORT_WINDOW, sb, ss)
    sb2, ss2 = _seen_before(held, t - timedelta(seconds=2 * SHORT_WINDOW))
    prev = _window(held, t - timedelta(seconds=SHORT_WINDOW), SHORT_WINDOW, sb2, ss2)
    mb, ms = _seen_before(held, t - timedelta(seconds=MID_WINDOW))
    mid = _window(held, t, MID_WINDOW, mb, ms)
    lb, ls = _seen_before(held, t - timedelta(seconds=LONG_WINDOW))
    lng = _window(held, t, LONG_WINDOW, lb, ls)
    lb2, ls2 = _seen_before(held, t - timedelta(seconds=2 * LONG_WINDOW))
    lng_prev = _window(held, t - timedelta(seconds=LONG_WINDOW), LONG_WINDOW, lb2, ls2)
    f["w10"], f["w10_prev"], f["w30"], f["w60"], f["w60_prev"] = cur, prev, mid, lng, lng_prev
    f["inflow_sol_per_s"] = round(cur["net_sol"] / SHORT_WINDOW, 6)
    f["inflow_acceleration_sol_per_s2"] = round((cur["net_sol"] - prev["net_sol"]) / SHORT_WINDOW / SHORT_WINDOW, 6)
    f["trade_rate_per_s"] = round(cur["trades"] / SHORT_WINDOW, 4)
    f["trade_acceleration"] = round(cur["trades"] / prev["trades"], 4) if prev["trades"] else None
    f["buyer_growth_10s"] = cur["new_buyers"]
    f["buyer_growth_prev_10s"] = prev["new_buyers"]
    f["seller_growth_10s"] = cur["new_sellers"]
    f["seller_growth_prev_10s"] = prev["new_sellers"]
    f["buy_sell_ratio_30s"] = round(mid["buy_sol"] / mid["sell_sol"], 4) if mid["sell_sol"] else None
    f["net_buy_pressure_30s"] = round((mid["buy_sol"] - mid["sell_sol"]) / (mid["buy_sol"] + mid["sell_sol"]), 4) \
        if (mid["buy_sol"] + mid["sell_sol"]) else None
    f["price_acceleration_pct"] = (round(cur["price_ret_pct"] - prev["price_ret_pct"], 3)
                                   if cur["price_ret_pct"] is not None and prev["price_ret_pct"] is not None else None)
    age = f["age_seconds"]
    f["buyers_per_minute"] = round(f["unique_buyers"] * 60 / age, 3) if age and age > 0 else None

    # Buy sizes and meaningful, independent buyers.
    sizes = [_sol(x.sol_lamports) for x in buys]
    f["median_buy_sol"] = round(statistics.median(sizes), 6) if sizes else None
    f["p90_buy_sol"] = round(sorted(sizes)[int(0.9 * (len(sizes) - 1))], 6) if sizes else None
    f["sol_per_buy"] = round(buy_sol / len(buys), 6) if buys else None
    per_wallet: dict[str, float] = {}
    trades_per_wallet: dict[str, int] = {}
    for x in held:
        trades_per_wallet[x.trader] = trades_per_wallet.get(x.trader, 0) + 1
        if x.is_buy:
            per_wallet[x.trader] = per_wallet.get(x.trader, 0.0) + _sol(x.sol_lamports)
    sync = synchronized_buy_cluster(held, t, LONG_WINDOW)
    f["sync_buy_cluster_60s"] = sync
    meaningful = {w for w, s in per_wallet.items() if s >= MEANINGFUL_BUY_SOL}
    f["meaningful_buyers"] = len(meaningful)
    independent = {w for w in meaningful if w != creator}
    f["meaningful_independent_buyers"] = len(independent)
    tiny_repeat = [x for x in held if _sol(x.sol_lamports) < TINY_BUY_SOL and trades_per_wallet.get(x.trader, 0) >= 3]
    f["tiny_trade_share"] = round(len(tiny_repeat) / len(held), 4)

    # Creator and large-buy-then-dump behaviour.
    f["creator_bought"] = bool(creator) and any(x.trader == creator and x.is_buy for x in held)
    f["creator_sold"] = bool(creator) and any(x.trader == creator and not x.is_buy for x in held)
    dumpers = 0
    for w in {x.trader for x in buys if _sol(x.sol_lamports) >= LARGE_BUY_SOL}:
        first = next(x for x in buys if x.trader == w and _sol(x.sol_lamports) >= LARGE_BUY_SOL)
        tok_in = sum(x.token_raw for x in buys if x.trader == w)
        tok_out = sum(x.token_raw for x in sells if x.trader == w and first.at <= x.at <= first.at + timedelta(seconds=DUMP_WITHIN_SECONDS))
        if tok_in and tok_out / tok_in >= DUMP_SOLD_SHARE:
            dumpers += 1
    f["large_buy_then_dump"] = dumpers

    # Price path: displacement from the first observable price, peak, drawdown.
    prices = [p for p in (_p(x) for x in held) if p]
    first_p, last_p = prices[0], prices[-1]
    peak_i = max(range(len(prices)), key=lambda i: prices[i])
    f["price_raw"] = last_p
    f["first_price_raw"] = first_p
    f["first_price_is_launch"] = bool(complete_history)
    f["displacement_pct"] = round((last_p / first_p - 1) * 100, 3)
    f["peak_price_raw"] = prices[peak_i]
    f["drawdown_from_peak_pct"] = round((1 - last_p / prices[peak_i]) * 100, 3)
    peak_trade = [x for x in held if _p(x)][peak_i]
    f["seconds_since_peak"] = round((t - peak_trade.at).total_seconds(), 1)
    run_high, worst = prices[0], 0.0
    for p in prices:
        run_high = max(run_high, p)
        worst = max(worst, 1 - p / run_high)
    f["max_pullback_so_far_pct"] = round(worst * 100, 3)

    # Curve progress and its rate (standard pump.fun curve).
    last = held[-1]
    f["curve_progress"] = round(max(0.0, min(1.0, 1 - (last.virtual_token - REAL_TOKEN_OFFSET) / INITIAL_REAL_TOKENS)), 6)
    f["sol_accumulated"] = round(_sol(last.virtual_sol - STANDARD_VSOL0), 6)
    back = _last_before(held, t - timedelta(seconds=LONG_WINDOW))
    if back is not None:
        prog_back = max(0.0, min(1.0, 1 - (back.virtual_token - REAL_TOKEN_OFFSET) / INITIAL_REAL_TOKENS))
        f["curve_progress_per_min"] = round(f["curve_progress"] - prog_back, 6)
    else:
        f["curve_progress_per_min"] = None
        f["unknown"]["curve_progress_per_min"] = "no trade 60 s ago"

    # Costs: does slippage leave anything to earn?
    fee = fee_bps if fee_bps else cfg.fee_bps
    f["fee_bps"] = fee
    f["fee_assumed"] = not bool(fee_bps)
    f["round_trip_cost_pct"] = round_trip_cost_pct(held, t, cfg.reference_size_sol, fee, cfg.fixed_cost_sol)
    return f


# --- phase ------------------------------------------------------------------------------------

def momentum_phase(f: dict[str, Any]) -> tuple[str, list[str]]:
    """EARLY_ACCELERATION / HEALTHY_CONTINUATION / EXHAUSTED / DISTRIBUTION /
    UNCLEAR, with the evidence."""
    if f.get("trades_total", 0) < 5 or "w10" not in f:
        return UNCLEAR, [f"{f.get('trades_total', 0)} trades: too few to classify"]
    cur, prev, w30, w60 = f["w10"], f["w10_prev"], f["w30"], f["w60"]
    dd = f.get("drawdown_from_peak_pct") or 0.0
    disp = f.get("displacement_pct") or 0.0
    age = f.get("age_seconds")
    ev: list[str] = []
    sellers_outrun = cur["new_sellers"] > cur["new_buyers"] and cur["sell_sol"] > cur["buy_sol"] * 1.2
    if (dd >= 25 and w30["net_sol"] < 0) or sellers_outrun or (f.get("large_buy_then_dump") and w30["net_sol"] < 0):
        ev.append(f"{dd:.0f}% below the peak, net flow 30 s {w30['net_sol']:+.3f} SOL"
                  + (", sellers outrunning buyers" if sellers_outrun else "")
                  + (", a large buyer sold within 30 s" if f.get("large_buy_then_dump") else ""))
        return DISTRIBUTION, ev
    # Demand now against its own recent average: a 60 s window still holds
    # the run-up, so "fading" compares the last 10 s with the 60 s mean.
    avg10 = w60["net_sol"] / (LONG_WINDOW / SHORT_WINDOW)
    buyers_fading = prev["new_buyers"] >= 3 and cur["new_buyers"] <= prev["new_buyers"] / 2
    inflow_fading = cur["net_sol"] < prev["net_sol"] and (avg10 <= 0 or cur["net_sol"] < 0.25 * avg10)
    fading = inflow_fading or buyers_fading
    if disp >= 150 and fading and (f.get("trade_acceleration") or 0) < 1:
        ev.append(f"up {disp:.0f}% from the first price; inflow {cur['net_sol']:+.3f} SOL in the last 10 s vs "
                  f"{avg10:+.3f} per 10 s over the minute; trade rate slowing")
        return EXHAUSTED, ev
    first_run = (f.get("max_pullback_so_far_pct") or 0.0) < EARLY_MAX_PULLBACK_PCT  # no pullback yet: still the first move
    if (age is not None and age <= 120 and first_run and cur["net_sol"] > 0 and cur["net_sol"] >= prev["net_sol"]
            and cur["new_buyers"] >= 2 and disp < 100):
        ev.append(f"{age:.0f}s old, inflow {prev['net_sol']:+.3f} -> {cur['net_sol']:+.3f} SOL per 10 s, "
                  f"{cur['new_buyers']} new buyers")
        return EARLY_ACCEL_PHASE, ev
    if (age is not None and age > 60 and dd < 20 and w60["net_sol"] > 0 and w60["new_buyers"] >= 3
            and w60["new_sellers"] <= w60["new_buyers"] and not fading and cur["net_sol"] > 0):
        ev.append(f"{dd:.0f}% below the peak, net inflow 60 s {w60['net_sol']:+.3f} SOL, "
                  f"{w60['new_buyers']} new buyers vs {w60['new_sellers']} new sellers")
        return HEALTHY_CONTINUATION, ev
    ev.append("no clear structure")
    return UNCLEAR, ev


# --- strategies -------------------------------------------------------------------------------

@dataclass
class StrategyDecision:
    strategy: str
    decision: str  # CANDIDATE / WAIT / NO_TRADE
    score: float | None
    evidence_level: str | None
    size_factor: float | None
    reasons: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _evidence(f: dict[str, Any]) -> tuple[str, float]:
    """How much the decision can rest on, and the size factor that follows
    (limited evidence -> a quarter of the planned size)."""
    n, b, age = f.get("trades_total", 0), f.get("meaningful_independent_buyers", 0), f.get("age_seconds") or 0
    if n >= 30 and b >= 10 and age >= 30:
        return STRONG, 1.0
    if n >= 15 and b >= 5:
        return MODERATE, 0.5
    return LIMITED, 0.25


def _quality_blocks(f: dict[str, Any], cfg: EntryConfig) -> tuple[list[str], list[str]]:
    """(NO_TRADE reasons, WAIT reasons) shared by the strategies."""
    no, wait = [], []
    if f.get("creator_sold"):
        no.append("creator wallet sold")
    if (f.get("sync_buy_cluster_60s") or 0) >= cfg.max_sync_cluster:
        no.append(f"{f['sync_buy_cluster_60s']} wallets bought identical amounts in the same second (scripted)")
    if f.get("large_buy_then_dump"):
        no.append("a large buyer sold most of its tokens within 30 s")
    cost = f.get("round_trip_cost_pct")
    if cost is None:
        wait.append("round-trip cost unknown (no curve state)")
    elif cost > cfg.max_round_trip_cost_pct:
        no.append(f"round-trip cost {cost:.1f}% at {cfg.reference_size_sol} SOL exceeds {cfg.max_round_trip_cost_pct}%: "
                  "slippage and fees remove the expected profit")
    if (f.get("tiny_trade_share") or 0) > cfg.max_tiny_trade_share:
        wait.append(f"{f['tiny_trade_share']:.0%} of trades are tiny repeats: activity may be inflated")
    stream_age = f.get("stream_age_seconds")
    if stream_age is not None and stream_age > 15:
        no.append(f"stream data {stream_age:.0f}s old: stale")
    return no, wait


def _score(parts: list[tuple[float, float]]) -> float:
    total = sum(w for _, w in parts)
    return round(sum(max(0.0, min(1.0, v)) * w for v, w in parts) / total, 4) if total else 0.0


def early_acceleration(f: dict[str, Any], phase: str, cfg: EntryConfig) -> StrategyDecision:
    s = StrategyDecision(EARLY_ACCELERATION, WAIT, None, None, None)
    if "w10" not in f:
        s.reasons.append("no trades yet")
        return s
    no, wait = _quality_blocks(f, cfg)
    age = f.get("age_seconds")
    cur, prev, w30 = f["w10"], f["w10_prev"], f["w30"]
    if age is None:
        wait.append("token age unknown")
    elif age > cfg.ea_max_age_seconds:
        no.append(f"{age:.0f}s old: past the early window ({cfg.ea_max_age_seconds}s)")
    if phase in (DISTRIBUTION, EXHAUSTED):
        no.append(f"phase {phase}")
    if (f.get("displacement_pct") or 0) > cfg.ea_max_displacement_pct:
        no.append(f"already {f['displacement_pct']:.0f}% above the first price (limit {cfg.ea_max_displacement_pct:.0f}%)")
    if f["trades_total"] < cfg.ea_min_trades:
        wait.append(f"{f['trades_total']} trades (need {cfg.ea_min_trades})")
    if f.get("meaningful_independent_buyers", 0) < cfg.ea_min_meaningful_buyers:
        wait.append(f"{f.get('meaningful_independent_buyers', 0)} meaningful independent buyers (need {cfg.ea_min_meaningful_buyers})")
    if w30["net_sol"] < cfg.ea_min_net_inflow_sol:
        wait.append(f"net inflow 30 s {w30['net_sol']:+.3f} SOL (need {cfg.ea_min_net_inflow_sol})")
    else:
        s.positives.append(f"net inflow 30 s {w30['net_sol']:+.3f} SOL")
    if cur["net_sol"] <= 0:
        wait.append(f"inflow stopped: {cur['net_sol']:+.3f} SOL in the last 10 s")
    elif cur["net_sol"] < prev["net_sol"]:
        wait.append(f"inflow decelerating: {prev['net_sol']:+.3f} -> {cur['net_sol']:+.3f} SOL per 10 s")
    else:
        s.positives.append(f"inflow accelerating {prev['net_sol']:+.3f} -> {cur['net_sol']:+.3f} SOL per 10 s")
    if cur["new_buyers"] < 1:
        wait.append("no new buyers in the last 10 s")
    else:
        s.positives.append(f"{cur['new_buyers']} new buyers in 10 s")
    if (cur.get("top_buyer_share") or 0) >= cfg.max_window_top_buyer_share and cur["new_buyers"] <= 2:
        wait.append(f"price move driven by one wallet ({cur['top_buyer_share']:.0%} of buy volume): weak organic demand")
    if cur["new_sellers"] > cur["new_buyers"]:
        wait.append(f"sellers growing faster than buyers ({cur['new_sellers']} vs {cur['new_buyers']})")
    level, factor = _evidence(f)
    s.evidence_level, s.size_factor = level, factor
    s.score = _score([(w30["net_sol"] / max(cfg.ea_min_net_inflow_sol * 4, 1e-9), 3),
                      ((cur["net_sol"] - prev["net_sol"]) / max(cfg.ea_min_net_inflow_sol, 1e-9), 2),
                      (f.get("meaningful_independent_buyers", 0) / max(cfg.ea_min_meaningful_buyers * 3, 1), 2),
                      (1 - (f.get("displacement_pct") or 0) / max(cfg.ea_max_displacement_pct, 1), 1)])
    if no:
        s.decision, s.reasons = NO_TRADE, no + wait
    elif wait:
        s.decision, s.reasons = WAIT, wait
    else:
        s.decision = CANDIDATE
        s.reasons = [f"evidence {level}: planned size x{factor}"]
    return s


def momentum_continuation(f: dict[str, Any], phase: str, cfg: EntryConfig) -> StrategyDecision:
    s = StrategyDecision(MOMENTUM_CONTINUATION, WAIT, None, None, None)
    if "w10" not in f:
        s.reasons.append("no trades yet")
        return s
    no, wait = _quality_blocks(f, cfg)
    age = f.get("age_seconds")
    cur, w60, w60p = f["w10"], f["w60"], f["w60_prev"]
    dd = f.get("drawdown_from_peak_pct") or 0.0
    if age is None or age < cfg.mc_min_age_seconds:
        wait.append(f"younger than {cfg.mc_min_age_seconds}s: continuation needs a history")
    if phase in (EXHAUSTED, DISTRIBUTION):
        no.append(f"phase {phase}")
    elif phase != HEALTHY_CONTINUATION:
        wait.append(f"phase {phase}: no healthy continuation structure")
    if (f.get("displacement_pct") or 0) > cfg.mc_max_displacement_pct:
        no.append(f"{f['displacement_pct']:.0f}% above the first price: likely exhausted (limit {cfg.mc_max_displacement_pct:.0f}%)")
    if w60["net_sol"] < cfg.mc_min_net_inflow_sol:
        wait.append(f"net inflow 60 s {w60['net_sol']:+.3f} SOL (need {cfg.mc_min_net_inflow_sol})")
    else:
        s.positives.append(f"net inflow 60 s {w60['net_sol']:+.3f} SOL")
    if w60["new_buyers"] < cfg.mc_min_new_buyers:
        wait.append(f"{w60['new_buyers']} new buyers in 60 s (need {cfg.mc_min_new_buyers})")
    if w60["new_sellers"] > w60["new_buyers"]:
        wait.append(f"seller growth {w60['new_sellers']} > buyer growth {w60['new_buyers']} in 60 s")
    if w60p["net_sol"] > 0 and w60["net_sol"] < w60p["net_sol"] * 0.5:
        wait.append(f"inflow halved vs the minute before ({w60p['net_sol']:+.3f} -> {w60['net_sol']:+.3f} SOL)")
    if dd > cfg.mc_max_pullback_pct:
        wait.append(f"pullback {dd:.0f}% deeper than {cfg.mc_max_pullback_pct:.0f}%")
    elif dd > 2 and (cur.get("price_ret_pct") or 0) <= 0:
        wait.append(f"in a {dd:.0f}% pullback that has not turned up yet")
    else:
        s.positives.append("at a new high" if dd <= 2 else f"recovering from a {dd:.0f}% pullback")
    level, factor = _evidence(f)
    s.evidence_level, s.size_factor = level, factor
    s.score = _score([(w60["net_sol"] / max(cfg.mc_min_net_inflow_sol * 4, 1e-9), 3),
                      (w60["new_buyers"] / max(cfg.mc_min_new_buyers * 3, 1), 2),
                      (1 - dd / max(cfg.mc_max_pullback_pct, 1), 1),
                      (1 - (f.get("displacement_pct") or 0) / max(cfg.mc_max_displacement_pct, 1), 1)])
    if no:
        s.decision, s.reasons = NO_TRADE, no + wait
    elif wait:
        s.decision, s.reasons = WAIT, wait
    else:
        s.decision, s.reasons = CANDIDATE, [f"evidence {level}: planned size x{factor}"]
    return s


def smart_wallet_confirmation(f: dict[str, Any], others: list[StrategyDecision], wallets: dict[str, Any] | None,
                              cfg: EntryConfig) -> StrategyDecision:
    """Confirms a candidate of another strategy with evaluated wallets. Never
    a trigger alone: without an EARLY_ACCELERATION or MOMENTUM_CONTINUATION
    candidate it WAITs whatever the wallets do."""
    s = StrategyDecision(SMART_WALLET_CONFIRMATION, WAIT, None, None, None)
    base = [o for o in others if o.decision == CANDIDATE]
    if not base:
        s.reasons.append("no EARLY_ACCELERATION or MOMENTUM_CONTINUATION candidate to confirm (never a trigger alone)")
        return s
    if not wallets or wallets.get("status") != "MEASURED":
        s.reasons.append("wallet history insufficient: " + ((wallets or {}).get("reason") or "not evaluated"))
        return s
    proven = wallets.get("proven_entries") or []
    exits = wallets.get("proven_exits") or []
    if exits:
        s.decision = NO_TRADE
        s.reasons.append(f"{len(exits)} proven wallet(s) already sold: " + ", ".join(e["wallet"][:6] + "…" for e in exits[:3]))
        return s
    recent = [p for p in proven if p.get("seconds_ago") is not None and p["seconds_ago"] <= cfg.sw_entry_within_seconds]
    if len(recent) < cfg.sw_min_proven_wallets:
        s.reasons.append(f"{len(recent)} proven wallet(s) entered in the last {cfg.sw_entry_within_seconds}s "
                         f"(need {cfg.sw_min_proven_wallets})")
        return s
    if wallets.get("coordinated"):
        s.decision = NO_TRADE
        s.reasons.append("the proven wallets trade as a coordinated group (shared dumps)")
        return s
    s.decision = CANDIDATE
    best = base[0]
    s.evidence_level, s.size_factor = best.evidence_level, best.size_factor
    s.score = round(min(1.0, (best.score or 0) * 0.7 + 0.3 * min(1.0, len(recent) / 3)), 4)
    s.positives.append(f"{len(recent)} proven wallet(s) entered: " + ", ".join(p["wallet"][:6] + "…" for p in recent[:3]))
    s.reasons.append(f"confirms {best.strategy}; selection bias possible: wallet records are this system's own observations")
    return s


def evaluate(f: dict[str, Any], cfg: EntryConfig, wallets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Phase and the three strategy decisions at one moment."""
    phase, phase_ev = momentum_phase(f)
    ea = early_acceleration(f, phase, cfg)
    mc = momentum_continuation(f, phase, cfg)
    sw = smart_wallet_confirmation(f, [ea, mc], wallets, cfg)
    return {"phase": phase, "phase_evidence": phase_ev,
            "strategies": {d.strategy: d.to_dict() for d in (ea, sw, mc)}}


def naive_sampled(mint: str, every: int) -> bool:
    """Deterministic 1-in-N launch sample for the NAIVE_SAMPLE baseline."""
    return int(hashlib.sha256(mint.encode()).hexdigest()[:8], 16) % max(every, 1) == 0


def compact_features(f: dict[str, Any]) -> dict[str, Any]:
    """The subset stored with a recorded signal (bounded size)."""
    keep = ("feature_version", "at", "age_seconds", "trades_total", "complete_history", "buys_total", "sells_total",
            "unique_buyers", "unique_sellers", "net_inflow_sol_total", "inflow_sol_per_s", "inflow_acceleration_sol_per_s2",
            "trade_rate_per_s", "trade_acceleration", "buyer_growth_10s", "buyer_growth_prev_10s", "seller_growth_10s",
            "seller_growth_prev_10s", "buy_sell_ratio_30s", "net_buy_pressure_30s", "price_acceleration_pct",
            "buyers_per_minute", "median_buy_sol", "p90_buy_sol", "sol_per_buy", "sync_buy_cluster_60s",
            "meaningful_buyers", "meaningful_independent_buyers", "tiny_trade_share", "creator_bought", "creator_sold",
            "large_buy_then_dump", "price_raw", "first_price_raw", "first_price_is_launch", "displacement_pct",
            "peak_price_raw", "drawdown_from_peak_pct", "seconds_since_peak", "max_pullback_so_far_pct", "curve_progress", "sol_accumulated",
            "curve_progress_per_min", "fee_bps", "fee_assumed", "round_trip_cost_pct", "last_trade_age_seconds",
            "stream_age_seconds", "unknown")
    out = {k: f.get(k) for k in keep if k in f}
    for w in ("w10", "w10_prev", "w30", "w60"):
        if w in f:
            out[w] = f[w]
    return out


# Numeric features used by the entry-timing model (services/ml): order is the
# model's input order; a missing value is imputed with the training median.
MODEL_FEATURES = ("age_seconds", "trades_total", "unique_buyers", "unique_sellers", "net_inflow_sol_total",
                  "inflow_sol_per_s", "inflow_acceleration_sol_per_s2", "trade_rate_per_s", "buyer_growth_10s",
                  "seller_growth_10s", "net_buy_pressure_30s", "buyers_per_minute", "median_buy_sol", "sol_per_buy",
                  "meaningful_independent_buyers", "tiny_trade_share", "displacement_pct", "drawdown_from_peak_pct",
                  "curve_progress", "curve_progress_per_min", "round_trip_cost_pct", "score")


def model_vector(features: dict[str, Any], score: float | None) -> list[float | None]:
    vals: list[float | None] = []
    for k in MODEL_FEATURES:
        v = score if k == "score" else features.get(k)
        try:
            vals.append(None if v is None or isinstance(v, bool) else float(v))
        except (TypeError, ValueError):
            vals.append(None)
    return vals


def logistic_predict(coef: dict[str, Any], vector: list[float | None]) -> float | None:
    """Pure-Python inference of an exported logistic model:
    {"features", "medians", "means", "scales", "weights", "intercept"}."""
    import math

    if not coef or list(coef.get("features") or []) != list(MODEL_FEATURES):
        return None
    z = float(coef["intercept"])
    for v, med, mu, sd, w in zip(vector, coef["medians"], coef["means"], coef["scales"], coef["weights"]):
        x = med if v is None else v
        z += w * ((x - mu) / sd if sd else 0.0)
    return round(1 / (1 + math.exp(-max(-50.0, min(50.0, z)))), 6)
