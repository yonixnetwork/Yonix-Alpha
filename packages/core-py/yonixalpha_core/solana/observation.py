"""Fresh pump.fun token observation: what the discovery funnel does with a
token between its launch and a decision.

A new token is FRESH_OBSERVING for `fresh_observation_seconds`. Nothing is
decided from a single moment: every trade of the token is in the stream
store with its on-chain timestamp, so the funnel rebuilds the whole window
each run and compares three checkpoints, T0 / T+half / T+window, plus the
two half-windows between them. That comparison says whether activity is
INCREASING, STABLE or DETERIORATING (or there is NO_ACTIVITY).

Outcomes:
- OBSERVING            window not over yet;
- PROMOTE              enough activity, not deteriorating: handed to the
                       safety gate (which still runs every check);
- CONTINUE_MONITORING  interesting but not qualified yet, and monitoring is
                       enabled, still active and within the time limit;
- REJECT               clear deterioration (price collapse from the peak,
                       or the creator selling into sell pressure);
- NO_TRADE             expired: inactive, too little activity, monitoring
                       disabled, or past the maximum monitoring time;
- MIGRATION_DETECTED   the curve completed / migrated while observed: the
                       migration engine takes over with pool rules.

Liquidity is never a precondition here: before migration there is no DEX
pool, and the bonding curve itself is the market. Its reserve and progress
are recorded as context; executability is judged by the gate.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from yonixalpha_core.solana.flow import Trade

LAMPORTS = Decimal(1_000_000_000)
# Pump.fun's standard launch: 793.1M sellable tokens (6 decimals) on the
# curve. Mayhem-mode launches report their own figure in the CreateEvent.
DEFAULT_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000

OBSERVING = "OBSERVING"
PROMOTE = "PROMOTE"
CONTINUE_MONITORING = "CONTINUE_MONITORING"
REJECT = "REJECT"
NO_TRADE = "NO_TRADE"
MIGRATION_DETECTED = "MIGRATION_DETECTED"
FINAL_OUTCOMES = {PROMOTE, REJECT, NO_TRADE, MIGRATION_DETECTED}

INCREASING, STABLE, DETERIORATING, NO_ACTIVITY = "INCREASING", "STABLE", "DETERIORATING", "NO_ACTIVITY"


@dataclass
class Interval:
    start: str
    end: str
    trades: int = 0
    buys: int = 0
    sells: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    new_buyers: int = 0  # first buy of that wallet in this token
    buy_volume_sol: str = "0"
    sell_volume_sol: str = "0"
    price_change_pct: str | None = None


@dataclass
class Checkpoint:
    label: str
    at: str
    trades: int
    unique_buyers: int
    unique_sellers: int
    volume_sol: str
    price_raw: str | None  # lamports per raw token unit (decimals-free: only ratios are used)


@dataclass
class ObservationReport:
    mint: str
    evaluated_at: str
    created_at: str | None
    age_seconds: float | None
    window_seconds: int
    outcome: str
    trend: str
    reasons: list[str] = field(default_factory=list)
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)
    halves: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sol(lamports: int) -> Decimal:
    return Decimal(lamports) / LAMPORTS


def _raw_price(t: Trade) -> Decimal | None:
    return Decimal(t.virtual_sol) / Decimal(t.virtual_token) if t.virtual_token else None


def _interval(trades: list[Trade], start: datetime, end: datetime, seen_before: set[str]) -> Interval:
    w = [t for t in trades if start < t.at <= end]
    buyers = {t.trader for t in w if t.is_buy}
    iv = Interval(start=start.isoformat(), end=end.isoformat(), trades=len(w), buys=sum(1 for t in w if t.is_buy),
                  sells=sum(1 for t in w if not t.is_buy), unique_buyers=len(buyers),
                  unique_sellers=len({t.trader for t in w if not t.is_buy}), new_buyers=len(buyers - seen_before),
                  buy_volume_sol=str(_sol(sum(t.sol_lamports for t in w if t.is_buy))),
                  sell_volume_sol=str(_sol(sum(t.sol_lamports for t in w if not t.is_buy))))
    before = [t for t in trades if t.at <= start]
    p0 = _raw_price(before[-1]) if before else (_raw_price(w[0]) if w else None)
    p1 = _raw_price(w[-1]) if w else None
    if p0 and p1:
        iv.price_change_pct = str(round((p1 / p0 - 1) * 100, 2))
    return iv


def _checkpoint(trades: list[Trade], label: str, at: datetime) -> Checkpoint:
    upto = [t for t in trades if t.at <= at]
    return Checkpoint(label=label, at=at.isoformat(), trades=len(upto),
                      unique_buyers=len({t.trader for t in upto if t.is_buy}),
                      unique_sellers=len({t.trader for t in upto if not t.is_buy}),
                      volume_sol=str(_sol(sum(t.sol_lamports for t in upto))),
                      price_raw=str(_raw_price(upto[-1])) if upto else None)


def curve_context(curve: Any | None, initial_real_token_reserves: int | None) -> dict[str, Any]:
    """Bonding-curve state as recorded context (never a precondition)."""
    if curve is None:
        return {"liquidity_state": "UNKNOWN (no curve state from the stream yet)"}
    ctx: dict[str, Any] = {"curve_complete": bool(curve.complete), "pool": curve.pool}
    if curve.rsol is not None:
        ctx["curve_real_sol"] = str(_sol(curve.rsol))
    if curve.rtok is not None:
        initial = initial_real_token_reserves or DEFAULT_INITIAL_REAL_TOKEN_RESERVES
        ctx["curve_progress"] = str(round(max(Decimal(0), 1 - Decimal(curve.rtok) / Decimal(initial)), 4))
    if curve.pool:
        ctx["liquidity_state"] = "MIGRATED (PumpSwap pool)"
    elif curve.complete:
        ctx["liquidity_state"] = "CURVE COMPLETE — migration pending"
    else:
        ctx["liquidity_state"] = "NO DEX POOL YET — bonding curve market"
    return ctx


def evaluate(mint: str, trades: list[Trade], created_ts: int | None, now: datetime, s: Any, creator: str | None = None,
             curve: Any | None = None, initial_real_token_reserves: int | None = None,
             monitoring_since: datetime | None = None) -> ObservationReport:
    """`s` carries the fresh_* settings (SafetySettings). `monitoring_since`
    is when the first observation window ended (None: first window)."""
    window = int(s.fresh_observation_seconds)
    created = datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None
    age = (now - created).total_seconds() if created else None
    trades = sorted(trades, key=lambda t: t.at)
    ctx = curve_context(curve, initial_real_token_reserves)
    report = ObservationReport(mint=mint, evaluated_at=now.isoformat(), created_at=created.isoformat() if created else None,
                               age_seconds=round(age, 1) if age is not None else None, window_seconds=window,
                               outcome=OBSERVING, trend=NO_ACTIVITY, metrics=dict(ctx))

    if curve is not None and (curve.pool or curve.complete):
        report.outcome = MIGRATION_DETECTED
        report.reasons.append("MIGRATION_DETECTED: " + ("PumpSwap pool " + curve.pool if curve.pool else "bonding curve complete")
                              + " — handed to the migration engine (MIGRATED_ANALYSIS, pool rules)")
        return report

    first_window_end = created + timedelta(seconds=window) if created else now
    if age is not None and age < window:
        report.reasons.append(f"FRESH_OBSERVING: {age:.0f}s of {window}s observation window")
        return report

    # The window compared: the launch window when this is the first
    # evaluation and it runs on time (within one window of its end), the
    # latest window otherwise (continued monitoring, or a token first seen
    # late, e.g. after a restart). T0 / T+half / T+window checkpoints are
    # cumulative since launch; the two halves are what happened between.
    on_time = monitoring_since is None and (now - first_window_end).total_seconds() <= window
    end = first_window_end if on_time else now
    start = end - timedelta(seconds=window)
    mid = start + timedelta(seconds=window / 2)
    seen_before = {t.trader for t in trades if t.is_buy and t.at <= start}
    a = _interval(trades, start, mid, seen_before)
    seen_mid = seen_before | {t.trader for t in trades if t.is_buy and start < t.at <= mid}
    b = _interval(trades, mid, end, seen_mid)
    report.halves = [asdict(a), asdict(b)]
    report.checkpoints = [asdict(_checkpoint(trades, lbl, at)) for lbl, at in
                          (("T0", start), (f"T+{window // 2}s", mid), (f"T+{window}s", end))]

    total = [t for t in trades if t.at <= now]
    buyers_total = len({t.trader for t in total if t.is_buy})
    sellers_total = len({t.trader for t in total if not t.is_buy})
    vol_total = _sol(sum(t.sol_lamports for t in total))
    buy_total, sell_total = _sol(sum(t.sol_lamports for t in total if t.is_buy)), _sol(sum(t.sol_lamports for t in total if not t.is_buy))
    prices = [p for p in (_raw_price(t) for t in total) if p]
    peak, last = (max(prices), prices[-1]) if prices else (None, None)
    drawdown = (1 - last / peak) if peak else None
    last_trade_age = (now - total[-1].at).total_seconds() if total else None
    creator_sold = bool(creator) and any(t.trader == creator and not t.is_buy for t in total)
    b_buy, b_sell = Decimal(b.buy_volume_sol), Decimal(b.sell_volume_sol)
    sell_pressure = b_sell / b_buy if b_buy > 0 else (Decimal("Infinity") if b_sell > 0 else Decimal(0))

    def growth(x: float, y: float) -> str | None:
        return None if x == 0 else str(round((y - x) / x * 100, 1))

    report.metrics.update({
        "trades_total": len(total), "unique_buyers_total": buyers_total, "unique_sellers_total": sellers_total,
        "volume_total_sol": str(vol_total), "buy_sell_volume_ratio": str(round(buy_total / sell_total, 3)) if sell_total else None,
        "tx_growth_pct": growth(a.trades, b.trades), "volume_growth_pct": growth(float(Decimal(a.buy_volume_sol) + Decimal(a.sell_volume_sol)),
                                                                                 float(b_buy + b_sell)),
        "buyer_growth": b.new_buyers - a.new_buyers, "seller_growth": b.unique_sellers - a.unique_sellers,
        "sell_pressure_latest_half": None if sell_pressure == Decimal("Infinity") else str(round(sell_pressure, 3)),
        "price_drawdown_from_peak_pct": str(round(drawdown * 100, 2)) if drawdown is not None else None,
        "last_trade_age_seconds": round(last_trade_age, 1) if last_trade_age is not None else None,
        "creator_sold": creator_sold,
    })

    # Trend: second half against the first.
    neg: list[str] = []
    if b_sell > 0 and sell_pressure >= s.fresh_max_sell_pressure:
        neg.append(f"sells accelerating: sell volume {b_sell:.4f} SOL vs buys {b_buy:.4f} SOL in the latest half-window")
    if a.trades >= 4 and b.trades <= a.trades / 2:
        neg.append(f"activity falling: {b.trades} trades vs {a.trades} in the previous half-window")
    if a.new_buyers >= 3 and b.new_buyers == 0:
        neg.append("buyer growth stopped: no new buyers in the latest half-window")
    if drawdown is not None and drawdown >= s.fresh_max_price_drawdown_pct:
        neg.append(f"price reversal: {drawdown:.0%} below the observed peak")
    if creator_sold:
        neg.append("creator wallet sold")
    pos: list[str] = []
    if b.trades > a.trades or (a.trades == 0 and b.trades > 0):
        pos.append(f"transactions increasing: {a.trades} → {b.trades}")
    if b.new_buyers >= a.new_buyers and b.new_buyers > 0:
        pos.append(f"new buyers {a.new_buyers} → {b.new_buyers}")
    if b_buy > Decimal(a.buy_volume_sol):
        pos.append(f"buy volume {a.buy_volume_sol} → {b.buy_volume_sol} SOL")
    if b.price_change_pct is not None and Decimal(b.price_change_pct) > 0:
        pos.append(f"price +{b.price_change_pct}% in the latest half-window")
    report.positive, report.negative = pos, neg
    if a.trades + b.trades == 0:
        report.trend = NO_ACTIVITY
    elif neg and not (len(pos) >= 3 and len(neg) == 1 and "creator" not in neg[0] and "reversal" not in neg[0]):
        report.trend = DETERIORATING
    elif b.trades >= max(1, a.trades) * 1.2 or (a.trades == 0 and b.trades > 0):
        report.trend = INCREASING
    else:
        report.trend = STABLE

    # Outcome.
    if drawdown is not None and drawdown >= s.fresh_max_price_drawdown_pct:
        report.outcome = REJECT
        report.reasons.append(f"REJECT: price {drawdown:.0%} below its observed peak (limit {s.fresh_max_price_drawdown_pct:.0%})")
        return report
    if creator_sold and b_sell > 0 and sell_pressure >= s.fresh_max_sell_pressure:
        report.outcome = REJECT
        report.reasons.append("REJECT: creator wallet sold while sellers dominate volume")
        return report

    short: list[str] = []
    if len(total) < s.fresh_promote_min_trades:
        short.append(f"{len(total)} trades since launch (need {s.fresh_promote_min_trades})")
    if buyers_total < s.fresh_promote_min_unique_buyers:
        short.append(f"{buyers_total} unique buyers (need {s.fresh_promote_min_unique_buyers})")
    if vol_total < s.fresh_promote_min_volume_quote:
        short.append(f"{vol_total:.4f} SOL volume since launch (need {s.fresh_promote_min_volume_quote})")
    if report.trend in (DETERIORATING, NO_ACTIVITY):
        short.append(f"activity {report.trend.lower()}")
    if not short:
        report.outcome = PROMOTE
        report.reasons.append(f"PROMOTE: {len(total)} trades, {buyers_total} buyers, {vol_total:.4f} SOL volume, "
                              f"trend {report.trend} — handed to the safety gate")
        return report

    expire: list[str] = []
    if not s.fresh_continue_monitoring:
        expire.append("continued monitoring is disabled")
    if age is not None and age >= s.fresh_max_monitoring_seconds:
        expire.append(f"maximum monitoring time {s.fresh_max_monitoring_seconds}s reached")
    if last_trade_age is None or last_trade_age >= s.fresh_inactivity_timeout_seconds:
        expire.append(f"inactive: no trade for {s.fresh_inactivity_timeout_seconds}s+" if last_trade_age is not None
                      else "no trades since launch")
    elif b.trades + a.trades < s.fresh_min_trades_to_continue:
        expire.append(f"only {a.trades + b.trades} trades in the latest {window}s (need {s.fresh_min_trades_to_continue} to keep watching)")
    if expire:
        report.outcome = NO_TRADE
        report.reasons.append("NO_TRADE: " + "; ".join(short + expire))
        return report
    report.outcome = CONTINUE_MONITORING
    report.reasons.append("CONTINUE_MONITORING: not qualified yet — " + "; ".join(short))
    return report
