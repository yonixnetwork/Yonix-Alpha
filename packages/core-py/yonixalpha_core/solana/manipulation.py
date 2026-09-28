"""MANIPULATION_SCORE: independent families of evidence that a token's
activity is manufactured rather than organic.

Each family is one kind of evidence (wash trading: repeated in-and-out by
the same wallet, not a single flip; synchronized buying,
synchronized selling, bot-regular trade sizes, dust volume, a straight-line
price, creator-linked funding, a known dump cohort, a copycat name, a
single-second collapse). A family either fires, with its evidence, or not.
The level counts families, never single indicators:

  NONE (0), LOW (1), MEDIUM (2), HIGH (>= high_families, default 3)
  UNKNOWN: too few trades to judge

"Same second" is the stream's timestamp resolution, a proxy for "same
block". The research behind the families (arXiv 2609.10246, launchwatch)
is summarized in docs/INTELLIGENCE_AUDIT_2026.md §R10. Nothing here
accuses a wallet; every family is a pattern, reported with its numbers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from yonixalpha_core.solana.flow import Trade, synchronized_buy_cluster

LAMPORTS = 1_000_000_000


@dataclass(frozen=True)
class ManipulationConfig:
    window_seconds: int = 120
    min_trades: int = 10
    round_trip_share: float = 0.40  # volume from wallets that both bought and sold in the window
    sync_buy_wallets: int = 4  # distinct wallets, same second, near-identical size
    sync_sell_wallets: int = 3  # distinct wallets selling in the same second
    regular_size_cv: float = 0.05  # coefficient of variation of buy sizes at or below: bot-regular
    regular_min_buys: int = 12
    dust_sol: float = 0.001
    dust_share: float = 0.40  # share of buys below dust_sol
    linear_r2: float = 0.97  # price vs time over the window, rising
    linear_min_trades: int = 15
    collapse_pct: float = 0.30  # price fall within one second, with 2+ sellers
    high_families: int = 3


def _price(t: Trade) -> float:
    return (t.virtual_sol / LAMPORTS) / (t.virtual_token / 1e6) if t.virtual_token else 0.0


def _r2_rising(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in points)
    if sxy <= 0:
        return 0.0  # falling or flat: not a manufactured rise
    return (sxy * sxy) / (sxx * syy)


def repeated_round_trip_share(window: list[Trade]) -> float | None:
    """Share of window volume from wallets that went in AND out at least
    twice (>= 2 buys and >= 2 sells). One buy then one sell is a flip, which
    nearly every pump.fun sniper does within minutes (measured in production
    2026-09-28: a single in-and-out fired on ~85% of launches); wash trading
    is a wallet trading back and forth to manufacture volume."""
    total = sum(t.sol_lamports for t in window)
    if total == 0:
        return None
    buys: dict[str, int] = defaultdict(int)
    sells: dict[str, int] = defaultdict(int)
    for t in window:
        (buys if t.is_buy else sells)[t.trader] += 1
    washers = {w for w in buys if buys[w] >= 2 and sells.get(w, 0) >= 2}
    return sum(t.sol_lamports for t in window if t.trader in washers) / total


def score(trades: list[Trade], t: datetime, cfg: ManipulationConfig | None = None, *,
          funding: dict | None = None, dump_cluster: dict | None = None, duplicate_of: str | None = None) -> dict[str, Any]:
    """Manipulation families at `t` (trades up to `t` only).
    funding: solana.funding.funding_links() result; dump_cluster: the wallet
    intelligence dump_cluster result; duplicate_of: an earlier mint that
    used the same name."""
    cfg = cfg or ManipulationConfig()
    window = sorted((x for x in trades if t - timedelta(seconds=cfg.window_seconds) < x.at <= t), key=lambda x: x.at)
    families: dict[str, str] = {}
    unknown: list[str] = []

    if len(window) < cfg.min_trades:
        unknown.append(f"{len(window)} trades in {cfg.window_seconds}s (flow families need {cfg.min_trades})")
    else:
        rts = repeated_round_trip_share(window)
        if rts is not None and rts >= cfg.round_trip_share:
            families["wash_trading"] = (f"{rts:.0%} of volume from wallets that bought and sold at least twice each "
                                        f"in {cfg.window_seconds}s")
        sync = synchronized_buy_cluster(window, t, cfg.window_seconds)
        if sync >= cfg.sync_buy_wallets:
            families["synchronized_buys"] = f"{sync} wallets bought near-identical amounts in the same second"
        sells_by_sec: dict[int, set[str]] = defaultdict(set)
        for x in window:
            if not x.is_buy:
                sells_by_sec[int(x.at.timestamp())].add(x.trader)
        most = max((len(v) for v in sells_by_sec.values()), default=0)
        if most >= cfg.sync_sell_wallets:
            families["synchronized_sells"] = f"{most} wallets sold in the same second"
        buys = [x.sol_lamports / LAMPORTS for x in window if x.is_buy]
        if len(buys) >= cfg.regular_min_buys:
            mean = statistics.fmean(buys)
            cv = statistics.pstdev(buys) / mean if mean else None
            if cv is not None and cv <= cfg.regular_size_cv:
                families["regular_trade_sizes"] = f"{len(buys)} buys with size variation {cv:.1%} (bot-regular)"
            dust = sum(1 for b in buys if b < cfg.dust_sol) / len(buys)
            if dust >= cfg.dust_share:
                families["dust_volume"] = f"{dust:.0%} of buys below {cfg.dust_sol} SOL"
        if len(window) >= cfg.linear_min_trades:
            t0 = window[0].at
            r2 = _r2_rising([((x.at - t0).total_seconds(), _price(x)) for x in window])
            if r2 is not None and r2 >= cfg.linear_r2:
                families["straight_line_price"] = f"price rose along a straight line (R² {r2:.3f}) over {len(window)} trades"
        by_sec: dict[int, list[Trade]] = defaultdict(list)
        for x in window:
            by_sec[int(x.at.timestamp())].append(x)
        for sec, xs in by_sec.items():
            sellers = {x.trader for x in xs if not x.is_buy}
            before = [x for x in window if x.at.timestamp() < sec]
            if len(sellers) >= 2 and before:
                drop = 1 - _price(xs[-1]) / _price(before[-1]) if _price(before[-1]) else 0
                if drop >= cfg.collapse_pct:
                    families["single_second_collapse"] = f"price fell {drop:.0%} within one second, {len(sellers)} sellers"
                    break

    if funding and int(funding.get("checked") or 0) > 0:
        linked = int(funding.get("creator_linked") or 0)
        group = int(funding.get("largest_group") or 0)
        if linked > 0 or group >= 3:
            families["creator_linked_funding"] = f"{linked} early buyers funded by the creator; largest shared-funder group {group}"
    else:
        unknown.append("funding links not checked")
    if dump_cluster and dump_cluster.get("level") in ("MEDIUM", "HIGH"):
        families["dump_cohort"] = f"dump cluster {dump_cluster['level']}: {'; '.join(dump_cluster.get('evidence') or [])[:160]}"
    elif not dump_cluster or dump_cluster.get("level") == "UNKNOWN":
        unknown.append("wallet cohort history unavailable")
    if duplicate_of:
        families["copycat_name"] = f"reuses the name of the earlier launch {duplicate_of}"

    n = len(families)
    if n == 0 and len(window) < cfg.min_trades:
        level = "UNKNOWN"
    else:
        level = "HIGH" if n >= cfg.high_families else "MEDIUM" if n == 2 else "LOW" if n == 1 else "NONE"
    return {"level": level, "families": families, "count": n, "unknown": unknown, "trades_in_window": len(window),
            "as_of": t.isoformat()}
