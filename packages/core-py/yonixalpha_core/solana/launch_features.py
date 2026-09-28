"""Causal launch features for Pump.fun tokens: time-series snapshots,
bonding-curve progress and trade efficiency, buyer breadth, contextual flow
state, momentum acceleration, and the regime flags that decide whether
curve math applies at all (Mayhem Mode, instant bonds, the BOOST window).

Every function takes `trades` (the stream's decoded TradeEvents, any
order) and a time `t`, and uses only trades at or before `t`. Nothing
observed later can leak into a value recorded for `t`. A value that cannot
be measured is None, with the reason in the result's "unknown" map; never
0 and never a default.

These are features for evaluation and learning. None of them is, by itself,
a reason to buy: see docs/INTELLIGENCE_AUDIT_2026.md for what the research
does and does not support.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from yonixalpha_core.solana.flow import Trade

FEATURE_VERSION = "launch-2026.09.1"
LAMPORTS = 1_000_000_000

# Standard pump.fun curve (verified against the decoded stream and the chain:
# the constant product holds exactly for non-Mayhem tokens).
STANDARD_VSOL0 = 30_000_000_000  # lamports
STANDARD_VTOK0 = 1_073_000_000_000_000  # raw (6 decimals)
STANDARD_K = STANDARD_VSOL0 * STANDARD_VTOK0
REAL_TOKEN_OFFSET = 279_900_000_000_000  # vtok at which the curve completes (real token reserves = 0)
INITIAL_REAL_TOKENS = STANDARD_VTOK0 - REAL_TOKEN_OFFSET  # 793.1M tokens sold along the curve
K_TOLERANCE = 1e-6  # relative; integer rounding stays far below this

SNAPSHOT_OFFSETS = (0, 5, 10, 20, 30, 60)  # seconds after creation
SOL_CHECKPOINTS = (5, 15, 30, 50, 70)  # SOL accumulated on the curve (vSOL 35 / 45 / 60 / 80 / 100)
MEANINGFUL_BUY_SOL = 0.05
INSTANT_BOND_SECONDS = 5  # create → migrate faster than this: a bundle nobody else could buy
BOOST_START = datetime(2026, 7, 21, tzinfo=timezone.utc)  # pump.fun BOOST: post-migration buybacks
BOOST_WINDOW_SECONDS = 300


def _sol(lamports: float) -> float:
    return lamports / LAMPORTS


def _upto(trades: Iterable[Trade], t: datetime) -> list[Trade]:
    return sorted((x for x in trades if x.at <= t), key=lambda x: x.at)


def _between(trades: list[Trade], start: datetime, end: datetime) -> list[Trade]:
    return [x for x in trades if start < x.at <= end]


def _price(t: Trade, decimals: int = 6) -> float:
    return (t.virtual_sol / LAMPORTS) / (t.virtual_token / 10 ** decimals) if t.virtual_token else 0.0


def _pre_reserves(t: Trade) -> tuple[int, int]:
    """Curve reserves right before this trade (events report them after)."""
    if t.is_buy:
        return t.virtual_sol - t.sol_lamports, t.virtual_token + t.token_raw
    return t.virtual_sol + t.sol_lamports, t.virtual_token - t.token_raw


# --- regime ---------------------------------------------------------------------------

def curve_math(trades: list[Trade], mayhem: bool | None) -> dict[str, Any]:
    """Whether constant-product curve math applies to this token. Mayhem
    tokens break the invariant (measured, docs/INTELLIGENCE_AUDIT_2026.md
    R1), so their progress, SOL accumulated and simulated fills are not
    computed. Validity is the invariant holding across this token's own
    trades (a protocol-wide change of the opening reserves would not make
    every token "invalid"); the match with today's standard k is reported
    separately. mayhem None (flag unknown) is unsafe, not False."""
    if mayhem:
        return {"valid": False, "reason": "Mayhem Mode token: different curve mechanism", "max_k_deviation": None,
                "standard_k": None}
    if not trades:
        return {"valid": None, "reason": "no trades yet", "max_k_deviation": None, "standard_k": None}
    ks = [t.virtual_sol * t.virtual_token for t in trades]
    std_dev = max(abs(k - STANDARD_K) / STANDARD_K for k in ks)
    standard = std_dev <= K_TOLERANCE
    if len(ks) >= 2:
        mean = sum(ks) / len(ks)
        dev = (max(ks) - min(ks)) / mean if mean else float("inf")
        holds = dev <= K_TOLERANCE
    else:
        dev, holds = std_dev, standard  # one trade: only the standard k can be compared
    base = {"max_k_deviation": dev, "standard_k": standard}
    if not holds:
        return {**base, "valid": False, "reason": f"constant product not held across trades (deviation {dev:.2e})"}
    if mayhem is None:
        return {**base, "valid": None, "reason": "Mayhem flag unknown"}
    return {**base, "valid": True, "reason": "constant product holds" + ("" if standard else " (non-standard opening reserves)")}


def instant_bond(created_ts: int | None, migrated_ts: int | None, threshold: int = INSTANT_BOND_SECONDS) -> bool | None:
    if created_ts is None or migrated_ts is None:
        return None
    return migrated_ts - created_ts < threshold


def data_regime(t: datetime) -> str:
    return "post_boost" if t >= BOOST_START else "pre_boost"


def boost_window(migrated_at: datetime | None, t: datetime, window: int = BOOST_WINDOW_SECONDS) -> bool | None:
    """True while post-migration flow is partly protocol buybacks (BOOST)."""
    if migrated_at is None:
        return None
    return migrated_at >= BOOST_START and timedelta(0) <= t - migrated_at < timedelta(seconds=window)


# --- coverage / data quality ----------------------------------------------------------

def coverage(trades: list[Trade], created_ts: int | None, stream_started_ts: int | None, max_kept: int = 400) -> dict[str, Any]:
    """Whether the trades held are the token's complete history from its
    creation (needed for "trades to reach N SOL")."""
    first = min((x.at for x in trades), default=None)
    complete = None
    why = None
    if created_ts is None:
        why = "creation time unknown"
    elif stream_started_ts is not None and stream_started_ts > created_ts:
        complete, why = False, "stream started after the token was created"
    elif len(trades) >= max_kept:
        complete, why = False, f"stream keeps the last {max_kept} trades; earlier ones dropped"
    elif first is not None:
        v0, _ = _pre_reserves(min(trades, key=lambda x: x.at))
        complete = abs(v0 - STANDARD_VSOL0) <= 1_000  # first trade starts from the opening reserve
        why = None if complete else "first trade held is not the first on the curve"
    return {"complete": complete, "reason": why, "trades_held": len(trades),
            "first_trade_at": first.isoformat() if first else None}


# --- snapshots ------------------------------------------------------------------------

def _buyer_stats(buys: list[Trade]) -> dict[str, Any]:
    sizes = [_sol(b.sol_lamports) for b in buys]
    per_wallet: dict[str, float] = defaultdict(float)
    for b in buys:
        per_wallet[b.trader] += _sol(b.sol_lamports)
    total = sum(per_wallet.values())
    top3 = sum(sorted(per_wallet.values(), reverse=True)[:3])
    return {"avg_buy_sol": round(statistics.fmean(sizes), 6) if sizes else None,
            "median_buy_sol": round(statistics.median(sizes), 6) if sizes else None,
            "largest_buy_sol": round(max(sizes), 6) if sizes else None,
            "top3_buy_share": round(top3 / total, 4) if total else None}


def snapshot(trades: list[Trade], created_at: datetime, t: datetime, *, decimals: int = 6, supply_raw: int | None = None,
             curve_valid: bool | None = None, interval_seconds: float = 5.0, prev: dict | None = None) -> dict[str, Any]:
    """The token's state at `t` from trades up to `t` only."""
    held = _upto(trades, t)
    unknown: dict[str, str] = {}
    out: dict[str, Any] = {"t": t.isoformat(), "age_seconds": round((t - created_at).total_seconds(), 3),
                           "trades": len(held), "unknown": unknown}
    if not held:
        unknown["price"] = "no trade yet"
        return out
    last = held[-1]
    first_price = _price(held[0], decimals)
    price = _price(last, decimals)
    prices = [_price(x, decimals) for x in held]
    buys = [x for x in held if x.is_buy]
    sells = [x for x in held if not x.is_buy]
    window = _between(held, t - timedelta(seconds=interval_seconds), t)
    earlier = {x.trader for x in held if x.at <= t - timedelta(seconds=interval_seconds) and x.is_buy}
    out.update({
        "price": price, "high": max(prices), "low": min(prices),
        "return_since_first": round(price / first_price - 1, 6) if first_price else None,
        "market_cap_sol": round(price * supply_raw / 10 ** decimals, 4) if supply_raw else None,
        "buy_count": len(buys), "sell_count": len(sells),
        "buy_volume_sol": round(sum(_sol(x.sol_lamports) for x in buys), 6),
        "sell_volume_sol": round(sum(_sol(x.sol_lamports) for x in sells), 6),
        "unique_buyers": len({x.trader for x in buys}), "unique_sellers": len({x.trader for x in sells}),
        "new_buyers_interval": len({x.trader for x in window if x.is_buy} - earlier),
        "trades_interval": len(window),
        "trade_rate_per_min": round(len(window) * 60 / interval_seconds, 3),
        **_buyer_stats(buys),
    })
    out["net_flow_sol"] = round(out["buy_volume_sol"] - out["sell_volume_sol"], 6)
    out["buy_sell_ratio"] = round(out["buy_volume_sol"] / out["sell_volume_sol"], 4) if out["sell_volume_sol"] else None
    if supply_raw is None:
        unknown["market_cap_sol"] = "supply unknown"
    if curve_valid:
        acc = _sol(last.virtual_sol - STANDARD_VSOL0)
        out["sol_accumulated"] = round(acc, 6)
        out["curve_progress"] = round(max(0.0, min(1.0, 1 - (last.virtual_token - REAL_TOKEN_OFFSET) / INITIAL_REAL_TOKENS)), 6)
        out["distance_to_graduation_sol"] = round(max(0.0, _sol(STANDARD_K // REAL_TOKEN_OFFSET - last.virtual_sol)), 6)
    else:
        for k in ("sol_accumulated", "curve_progress", "distance_to_graduation_sol"):
            unknown[k] = "curve math does not apply or is unverified"
    if prev and prev.get("price") and prev.get("age_seconds") is not None:
        dt = out["age_seconds"] - prev["age_seconds"]
        if dt > 0:
            out["price_velocity"] = round((price / prev["price"] - 1) / dt, 8)
            if prev.get("price_velocity") is not None:
                out["price_acceleration"] = round((out["price_velocity"] - prev["price_velocity"]) / dt, 10)
            if curve_valid and prev.get("sol_accumulated") is not None:
                out["curve_velocity_sol_s"] = round((out["sol_accumulated"] - prev["sol_accumulated"]) / dt, 6)
                if prev.get("curve_velocity_sol_s") is not None:
                    out["curve_acceleration"] = round((out["curve_velocity_sol_s"] - prev["curve_velocity_sol_s"]) / dt, 8)
            out["buyer_growth"] = out["unique_buyers"] - (prev.get("unique_buyers") or 0)
            out["seller_growth"] = out["unique_sellers"] - (prev.get("unique_sellers") or 0)
            if prev.get("trade_rate_per_min") is not None:
                out["trade_rate_acceleration"] = round(out["trade_rate_per_min"] - prev["trade_rate_per_min"], 3)
    return out


def snapshot_series(trades: list[Trade], created_at: datetime, now: datetime, offsets: Iterable[int] = SNAPSHOT_OFFSETS,
                    **kw) -> list[dict[str, Any]]:
    """Snapshots at creation + each offset that is not in the future."""
    out: list[dict[str, Any]] = []
    prev = None
    last_off = 0
    for off in sorted(offsets):
        t = created_at + timedelta(seconds=off)
        if t > now:
            break
        s = snapshot(trades, created_at, t, interval_seconds=max(1.0, off - last_off) if off else 1.0, prev=prev, **kw)
        s["offset_seconds"] = off
        out.append(s)
        prev, last_off = s, off
    return out


# --- trade efficiency -----------------------------------------------------------------

def trades_to_reach(trades: list[Trade], t: datetime, *, complete_history: bool | None, curve_valid: bool | None,
                    checkpoints: Iterable[float] = SOL_CHECKPOINTS, meaningful_sol: float = MEANINGFUL_BUY_SOL) -> dict[str, Any]:
    """For each SOL-accumulated checkpoint: trades (and seconds since the first
    trade) it took to reach it, plus buyer breadth at that point. Reaching a
    level in fewer, larger trades vs many tiny ones is one feature among
    many, not a signal by itself."""
    held = _upto(trades, t)
    out: dict[str, Any] = {"checkpoints": {}, "unknown": None}
    if not curve_valid:
        out["unknown"] = "curve math does not apply or is unverified"
        return out
    if not complete_history:
        out["unknown"] = "trade history from creation not held"
        return out
    if not held:
        return out
    start = held[0].at
    for cp in checkpoints:
        target = STANDARD_VSOL0 + int(cp * LAMPORTS)
        idx = next((i for i, x in enumerate(held) if x.virtual_sol >= target), None)
        if idx is None:
            out["checkpoints"][str(cp)] = None
            continue
        upto = held[:idx + 1]
        buys = [x for x in upto if x.is_buy]
        meaningful = [b for b in buys if _sol(b.sol_lamports) >= meaningful_sol]
        out["checkpoints"][str(cp)] = {
            "trades": idx + 1, "seconds": round((held[idx].at - start).total_seconds(), 3),
            "unique_buyers": len({b.trader for b in buys}),
            "meaningful_buy_ratio": round(len(meaningful) / len(buys), 4) if buys else None,
            **{k: v for k, v in _buyer_stats(buys).items() if k in ("avg_buy_sol", "median_buy_sol", "top3_buy_share")},
        }
    return out


# --- buyer breadth --------------------------------------------------------------------

def buyer_breadth(trades: list[Trade], t: datetime, window_seconds: int = 60, target_buyers: int = 20,
                  meaningful_sol: float = MEANINGFUL_BUY_SOL, recycled_wallets: set[str] | None = None) -> dict[str, Any]:
    """How broad the buying is, in [0, 1], with every component shown. A
    heuristic input for learning: many transactions from a few (or
    recycled) wallets score low. recycled_wallets: wallets known (from
    earlier launches) to appear as early buyers again and again; None when
    wallet history is unavailable."""
    buys = [x for x in _between(_upto(trades, t), t - timedelta(seconds=window_seconds), t) if x.is_buy]
    if len(buys) < 3:
        return {"score": None, "reason": f"{len(buys)} buys in {window_seconds}s", "components": {}}
    per_wallet: dict[str, int] = defaultdict(int)
    for b in buys:
        per_wallet[b.trader] += 1
    stats = _buyer_stats(buys)
    comps = {
        "breadth": min(1.0, len(per_wallet) / target_buyers),
        "diversity": 1 - (stats["top3_buy_share"] or 0),
        "non_repeat": 1 - sum(n for n in per_wallet.values() if n >= 3) / len(buys),
        "meaningful": sum(1 for b in buys if _sol(b.sol_lamports) >= meaningful_sol) / len(buys),
    }
    if recycled_wallets is not None:
        comps["non_recycled"] = 1 - len(set(per_wallet) & recycled_wallets) / len(per_wallet)
    return {"score": round(statistics.fmean(comps.values()), 4), "unique_buyers": len(per_wallet), "buys": len(buys),
            "components": {k: round(v, 4) for k, v in comps.items()}}


# --- contextual flow ------------------------------------------------------------------

EXPANSION, HEALTHY_CONSOLIDATION, DISTRIBUTION, DETERIORATION = "EXPANSION", "HEALTHY_CONSOLIDATION", "DISTRIBUTION", "DETERIORATION"
QUIET, MIXED, UNKNOWN = "QUIET", "MIXED", "UNKNOWN"


def flow_state(trades: list[Trade], t: datetime, window_seconds: int = 60, min_trades: int = 6, decimals: int = 6,
               price_tolerance: float = 0.03) -> dict[str, Any]:
    """Context, not a volume rule: falling volume with buyers still arriving,
    sellers not accelerating and price holding is consolidation; falling
    volume with buyers gone, sellers accelerating and new lows is
    deterioration."""
    held = _upto(trades, t)
    cur = _between(held, t - timedelta(seconds=window_seconds), t)
    prev = _between(held, t - timedelta(seconds=2 * window_seconds), t - timedelta(seconds=window_seconds))
    if len(cur) + len(prev) < min_trades:
        return {"state": UNKNOWN if not held else QUIET, "evidence": [f"{len(cur) + len(prev)} trades in two windows"]}
    seen_before = {x.trader for x in held if x.at <= t - timedelta(seconds=window_seconds) and x.is_buy}
    vol = lambda w: sum(x.sol_lamports for x in w)  # noqa: E731
    new_buyers = len({x.trader for x in cur if x.is_buy} - seen_before)
    sellers_cur, sellers_prev = len({x.trader for x in cur if not x.is_buy}), len({x.trader for x in prev if not x.is_buy})
    sell_share = sum(x.sol_lamports for x in cur if not x.is_buy) / vol(cur) if vol(cur) else 0.0
    # Structure is judged against where the previous window ended, not its
    # low: a window that ran up from launch has a low far below any later price.
    low_cur = min((_price(x, decimals) for x in cur), default=None)
    close_cur, close_prev = (_price(cur[-1], decimals) if cur else None), (_price(prev[-1], decimals) if prev else None)
    volume_down = vol(prev) > 0 and vol(cur) < vol(prev)
    price_holding = low_cur is not None and close_prev is not None and low_cur >= close_prev * (1 - price_tolerance)
    new_lows = (close_cur is not None and close_prev is not None and low_cur is not None
                and close_cur < close_prev * (1 - price_tolerance) and low_cur < close_prev * (1 - price_tolerance))
    sellers_accel = sellers_cur > max(sellers_prev * 1.25, sellers_prev + 1)
    ev = [f"volume {_sol(vol(prev)):.3f} → {_sol(vol(cur)):.3f} SOL", f"{new_buyers} new buyers",
          f"sellers {sellers_prev} → {sellers_cur}", f"sell share {sell_share:.0%}",
          "price holding" if price_holding else ("new lows" if new_lows else "price range unchanged")]
    if not cur:
        state = QUIET
    elif new_lows and sellers_accel and new_buyers == 0:
        state = DETERIORATION
    elif close_cur and close_prev and close_cur > close_prev and not volume_down and new_buyers > 0 and sell_share < 0.5:
        state = EXPANSION
    elif volume_down and new_buyers > 0 and not sellers_accel and price_holding:
        state = HEALTHY_CONSOLIDATION
    elif price_holding and sell_share > 0.55 and sellers_accel:
        state = DISTRIBUTION
    else:
        state = MIXED
    return {"state": state, "evidence": ev, "new_buyers": new_buyers, "sellers": sellers_cur, "sell_share": round(sell_share, 4)}


# --- momentum -------------------------------------------------------------------------

def momentum(trades: list[Trade], t: datetime, decimals: int = 6) -> dict[str, Any]:
    """1 m / 5 m returns and acceleration of volume, buyers, sellers, trades
    and new wallets: last minute vs the minute before."""
    held = _upto(trades, t)

    def price_at(ago: int) -> float | None:
        before = [x for x in held if x.at <= t - timedelta(seconds=ago)]
        return _price(before[-1], decimals) if before else None

    now_p = _price(held[-1], decimals) if held else None
    cur = _between(held, t - timedelta(seconds=60), t)
    prev = _between(held, t - timedelta(seconds=120), t - timedelta(seconds=60))
    seen_cur = {x.trader for x in held if x.at <= t - timedelta(seconds=60)}
    seen_prev = {x.trader for x in held if x.at <= t - timedelta(seconds=120)}

    def ratio(a: float, b: float) -> float | None:
        return round(a / b, 4) if b else None

    p1, p5 = price_at(60), price_at(300)
    return {
        "return_1m": round(now_p / p1 - 1, 6) if now_p and p1 else None,
        "return_5m": round(now_p / p5 - 1, 6) if now_p and p5 else None,
        "volume_acceleration": ratio(sum(x.sol_lamports for x in cur), sum(x.sol_lamports for x in prev)),
        "buyer_acceleration": ratio(len({x.trader for x in cur if x.is_buy}), len({x.trader for x in prev if x.is_buy})),
        "seller_acceleration": ratio(len({x.trader for x in cur if not x.is_buy}), len({x.trader for x in prev if not x.is_buy})),
        "trade_rate_acceleration": ratio(len(cur), len(prev)),
        "new_wallet_acceleration": ratio(len({x.trader for x in cur} - seen_cur), len({x.trader for x in prev} - seen_prev)),
        "trades_1m": len(cur), "trades_prev_1m": len(prev),
    }


# --- post-migration state -------------------------------------------------------------

DUMPING, STABILIZING, RECOVERING, CONTINUING, WEAK = "DUMPING", "STABILIZING", "RECOVERING", "CONTINUING", "WEAK"


def post_migration_state(pool_trades: list[Trade], migrated_at: datetime | None, t: datetime, *, decimals: int = 6,
                         window_seconds: int = 60, min_trades: int = 6) -> dict[str, Any]:
    """DUMPING / STABILIZING / RECOVERING / CONTINUING / WEAK / UNKNOWN
    from PumpSwap trades since migration. RECOVERING describes the path;
    it is never a buy signal by itself (confirmation is the gate's job)."""
    held = [x for x in _upto(pool_trades, t) if migrated_at is None or x.at >= migrated_at]
    boost = boost_window(migrated_at, t)
    base = {"boost_window": boost, "trades": len(held)}
    if len(held) < min_trades:
        return {**base, "state": UNKNOWN if not held else WEAK, "evidence": [f"{len(held)} pool trades since migration"]}
    prices = [_price(x, decimals) for x in held]
    first, last, low = prices[0], prices[-1], min(prices)
    low_at = held[prices.index(low)].at
    cur = _between(held, t - timedelta(seconds=window_seconds), t)
    prev = _between(held, t - timedelta(seconds=2 * window_seconds), t - timedelta(seconds=window_seconds))
    sell = lambda w: sum(x.sol_lamports for x in w if not x.is_buy)  # noqa: E731
    buy = lambda w: sum(x.sol_lamports for x in w if x.is_buy)  # noqa: E731
    new_low_recent = low_at > t - timedelta(seconds=window_seconds)
    ev = [f"price vs migration {last / first - 1:+.1%}", f"off the post-migration low {last / low - 1:+.1%}",
          f"sell/buy last {window_seconds}s {_sol(sell(cur)):.2f}/{_sol(buy(cur)):.2f} SOL"]
    if boost:
        ev.append("BOOST window: part of the buying is protocol buybacks")
    if len(cur) < 2:
        state = WEAK
    elif sell(cur) > buy(cur) and new_low_recent:
        state = DUMPING
    elif last >= first and buy(cur) >= sell(cur):
        state = CONTINUING
    elif not new_low_recent and last > low * 1.10 and buy(cur) > sell(cur):
        state = RECOVERING
    elif not new_low_recent and sell(cur) <= sell(prev):
        state = STABILIZING
    else:
        state = WEAK
    return {**base, "state": state, "evidence": ev, "return_since_migration": round(last / first - 1, 6),
            "off_low": round(last / low - 1, 6)}
