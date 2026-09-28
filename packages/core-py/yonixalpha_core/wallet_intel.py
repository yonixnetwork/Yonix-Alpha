"""Wallet intelligence: who bought a launch early, and what happened to the
launches those wallets bought before.

Three evidence sources, all causal (only what was known before the
decision they inform), all features rather than rules:

  smart_money   Beta-Binomial reputation of each early buyer: how often
                launches it bought early went on to rise (WIN) after this
                system decided on them. Shrunk toward the base rate, judged
                on its LOWER bound, so a wallet with 2 lucky launches is not
                "proven". Never a BUY trigger; there is no whitelist.
  recycled      wallets that were an early buyer of many other launches in
                the last 24 h (sniper / bundle wallets that buy everything).
  dump_cluster  cohorts of wallets that repeatedly sold early TOGETHER in
                launches that then failed (union-find over shared dumps).
                UNKNOWN when none of the buyers has resolved history; a
                pattern with evidence, never an accusation.

Recording (paper-trading loop, opportunities.track): the first N buyers of
each launch the system decided on are written to launch_buyers when its
opportunity row is first tracked; their sells in the first minutes are
updated while the early window is open; the launch outcome is written when
the row completes (T+30m), and only then do the Redis counters change. So a
decision at time t reads counters built only from outcomes resolved before
t. Coverage is bounded by what this system observed (not all of pump.fun).

Redis (read at decision time by the assembler, no database round-trip):
  yx:wi:rep:{wallet}    hash n, wins, losses, dumps
  yx:wi:base            hash n, wins (the prior's base rate)
  yx:wi:mates:{wallet}  zset mate -> launches where both dumped
  yx:wi:seen:{wallet}   zset mint -> first buy time (recycled detection)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import LaunchBuyer
from yonixalpha_core.solana import launch_features as lf
from yonixalpha_core.solana.flow import Trade

LAMPORTS = Decimal(1_000_000_000)
REP = "yx:wi:rep:"
BASE = "yx:wi:base"
MATES = "yx:wi:mates:"
SEEN = "yx:wi:seen:"
SEEN_RETENTION = 2 * 86400
MIN_BASE_OBSERVATIONS = 100  # below this the base rate itself is unknown
Z = 1.645  # one-sided 95% lower bound

WIN, FLAT, LOSS = "WIN", "FLAT", "LOSS"


@dataclass(frozen=True)
class WalletConfig:
    early_buyers: int = 20
    early_sell_seconds: int = 120
    sold_early_share: float = 0.5
    win_peak_pct: float = 50.0
    loss_drawdown_pct: float = 50.0
    prior_strength: float = 10.0
    min_launches: int = 5
    history_days: int = 30
    recycled_launches: int = 5
    cohort_min_shared: int = 3
    cluster_medium_wallets: int = 2
    cluster_high_wallets: int = 4
    serial_dumper_launches: int = 3
    serial_dumper_rate: float = 0.6


def config(s: Any) -> WalletConfig:
    """From SafetySettings (wallet_* / dump_cluster_* fields)."""
    return WalletConfig(
        early_buyers=int(s.wallet_early_buyers), early_sell_seconds=int(s.wallet_early_sell_seconds),
        sold_early_share=float(s.wallet_sold_early_share), win_peak_pct=float(s.wallet_win_peak_pct),
        loss_drawdown_pct=float(s.wallet_loss_drawdown_pct), prior_strength=float(s.wallet_prior_strength),
        min_launches=int(s.wallet_min_launches), history_days=int(s.wallet_history_days),
        recycled_launches=int(s.wallet_recycled_launches), cohort_min_shared=int(s.dump_cluster_min_shared),
        cluster_medium_wallets=int(s.dump_cluster_medium_wallets), cluster_high_wallets=int(s.dump_cluster_high_wallets))


# --- pure functions -------------------------------------------------------------------

def early_buyers(trades: list[Trade], n: int) -> list[dict[str, Any]]:
    """First `n` distinct buyers in trade order, with their first-buy size.
    The caller checks the history starts at the launch (coverage)."""
    out: dict[str, dict[str, Any]] = {}
    for t in sorted(trades, key=lambda x: x.at):
        if not t.is_buy:
            continue
        if t.trader not in out:
            if len(out) >= n:
                continue
            out[t.trader] = {"wallet": t.trader, "rank": len(out) + 1, "first_buy_at": t.at, "sol_lamports": 0, "tokens": 0}
        b = out[t.trader]
        if t.at <= b["first_buy_at"] + timedelta(seconds=5):  # the entry itself (split fills in the same moment)
            b["sol_lamports"] += t.sol_lamports
            b["tokens"] += t.token_raw
    return list(out.values())


def early_sold_share(trades: list[Trade], wallet: str, start: datetime, seconds: int, tokens_in: int) -> tuple[float | None, bool]:
    """Share of the wallet's entry tokens sold within `seconds` of its first
    buy, and whether the held history covers that whole window (it does not
    once the stream has trimmed trades from inside it)."""
    if tokens_in <= 0:
        return None, False
    end = start + timedelta(seconds=seconds)
    held = sorted(trades, key=lambda x: x.at)
    covered = bool(held) and held[0].at <= start
    sold = sum(t.token_raw for t in held if t.trader == wallet and not t.is_buy and start <= t.at <= end)
    return min(1.0, sold / tokens_in), covered


def launch_outcome(peak_pct: Decimal | float | None, drawdown_pct: Decimal | float | None, cfg: WalletConfig) -> str | None:
    """WIN: rose at least win_peak_pct after the decision within 30 min.
    LOSS: did not, and fell at least loss_drawdown_pct. FLAT: neither."""
    if peak_pct is None and drawdown_pct is None:
        return None
    if peak_pct is not None and float(peak_pct) >= cfg.win_peak_pct:
        return WIN
    if drawdown_pct is not None and float(drawdown_pct) <= -cfg.loss_drawdown_pct:
        return LOSS
    return FLAT


def beta_reputation(n: int, wins: int, base_rate: float, prior_strength: float) -> dict[str, float]:
    """Posterior Beta(wins + m·p, n − wins + m·(1 − p)); mean and a one-sided
    95% lower bound (normal approximation of the Beta)."""
    a = wins + prior_strength * base_rate
    b = (n - wins) + prior_strength * (1 - base_rate)
    mean = a / (a + b)
    var = a * b / ((a + b) ** 2 * (a + b + 1))
    return {"mean": round(mean, 4), "lower": round(max(0.0, mean - Z * math.sqrt(var)), 4), "n": n, "wins": wins}


def cohorts(wallets: list[str], edges: dict[tuple[str, str], int], min_shared: int) -> list[set[str]]:
    """Connected groups (size >= 2) among `wallets`, linking two wallets that
    dumped together in at least `min_shared` earlier launches."""
    parent = {w: w for w in wallets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), shared in edges.items():
        if shared >= min_shared and a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for w in wallets:
        groups.setdefault(find(w), set()).add(w)
    return sorted((g for g in groups.values() if len(g) >= 2), key=len, reverse=True)


def dump_cluster(stats: dict[str, dict[str, int]], groups: list[set[str]], cfg: WalletConfig) -> dict[str, Any]:
    """UNKNOWN (no buyer has resolved history) / LOW / MEDIUM / HIGH, with
    the evidence. `stats`: wallet -> {n, dumps} for the current early buyers."""
    with_history = [w for w, s in stats.items() if s.get("n", 0) > 0]
    if not with_history:
        return {"level": "UNKNOWN", "evidence": [], "wallets_with_history": 0,
                "reason": "none of the early buyers has a resolved launch in this system's history"}
    serial = sorted(w for w in with_history if stats[w].get("dumps", 0) >= cfg.serial_dumper_launches
                    and stats[w]["dumps"] / stats[w]["n"] >= cfg.serial_dumper_rate)
    largest = len(groups[0]) if groups else 0
    evidence = [f"cohort of {len(g)} wallets that sold early together in >= {cfg.cohort_min_shared} earlier failed launches"
                for g in groups[:3]]
    if serial:
        evidence.append(f"{len(serial)} early buyers sold early in >= {cfg.serial_dumper_rate:.0%} of their resolved launches")
    if largest >= cfg.cluster_high_wallets:
        level = "HIGH"
    elif largest >= cfg.cluster_medium_wallets or len(serial) >= 3:
        level = "MEDIUM"
    else:
        level = "LOW"
    return {"level": level, "evidence": evidence, "largest_cohort": largest, "cohorts": [sorted(g) for g in groups[:3]],
            "serial_dumpers": serial[:10], "wallets_with_history": len(with_history)}


# --- decision time (assembler) ------------------------------------------------------------

async def assess(redis, trades: list[Trade], now: datetime, cfg: WalletConfig, *, complete_history: bool,
                 created_at: datetime | None, mint: str | None = None) -> dict[str, Any]:
    """Wallet features for a launch at `now` from Redis counters (outcomes
    resolved before now). Returns smart_money, recycled_wallets (list),
    dump_cluster. Every part says what it could not see."""
    as_of = now.isoformat()
    if not complete_history:
        unknown = {"level": "UNKNOWN", "reason": "trade history from creation not held", "as_of": as_of}
        return {"smart_money": {"status": "UNKNOWN", "reason": unknown["reason"], "as_of": as_of},
                "recycled_wallets": [], "dump_cluster": unknown, "as_of": as_of}
    held = [t for t in trades if t.at <= now]
    buyers = early_buyers(held, cfg.early_buyers)
    wallets = [b["wallet"] for b in buyers]
    base_raw = await redis.hgetall(BASE)
    pipe = redis.pipeline()
    for w in wallets:
        pipe.hgetall(REP + w)
    for w in wallets:
        pipe.zrangebyscore(MATES + w, cfg.cohort_min_shared, "+inf", withscores=True)
    for w in wallets:
        pipe.zrangebyscore(SEEN + w, now.timestamp() - 86400, now.timestamp())
    res = await pipe.execute() if wallets else []
    k = len(wallets)
    reps, mates, seen = res[:k], res[k:2 * k], res[2 * k:]

    def _d(h: dict) -> dict[str, int]:
        return {(key.decode() if isinstance(key, bytes) else key): int(v) for key, v in (h or {}).items()}

    base = _d(base_raw)
    stats = {w: _d(r) for w, r in zip(wallets, reps)}
    # --- recycled
    recycled = []
    for w, mints in zip(wallets, seen):
        others = [m for m in mints if (m.decode() if isinstance(m, bytes) else m) != mint]
        if len(others) >= cfg.recycled_launches:
            recycled.append(w)
    # --- smart money
    buy_sol = sum(t.sol_lamports for t in held if t.is_buy) or 0
    if base.get("n", 0) < MIN_BASE_OBSERVATIONS:
        smart = {"status": "UNKNOWN", "reason": f"{base.get('n', 0)} resolved buyer-launches so far "
                                                  f"(base rate needs {MIN_BASE_OBSERVATIONS})"}
    else:
        p = base["wins"] / base["n"]
        proven, capital, arrivals = [], 0, []
        with_history = 0
        for b in buyers:
            s = stats[b["wallet"]]
            if s.get("n", 0) == 0:
                continue
            with_history += 1
            rep = beta_reputation(s["n"], s.get("wins", 0), p, cfg.prior_strength)
            if s["n"] >= cfg.min_launches and rep["lower"] > p:
                proven.append({"wallet": b["wallet"], **rep})
                capital += b["sol_lamports"]
                if created_at is not None:
                    arrivals.append((b["first_buy_at"] - created_at).total_seconds())
        smart = {"status": "MEASURED" if with_history else "UNKNOWN", "base_rate": round(p, 4),
                 "evaluated_wallets": len(buyers), "wallets_with_history": with_history,
                 "proven_wallets": len(proven), "proven_capital_sol": str((Decimal(capital) / LAMPORTS).quantize(Decimal("0.0001"))),
                 "proven_share_of_buy_volume": round(capital / buy_sol, 4) if buy_sol else None,
                 "first_proven_arrival_seconds": min(arrivals) if arrivals else None,
                 "proven": proven[:10]}
        if not with_history:
            smart["reason"] = "none of the early buyers has a resolved launch in this system's history"
    smart.update({"as_of": as_of, "note": "feature only: never a BUY trigger, never a whitelist"})
    edges: dict[tuple[str, str], int] = {}
    for w, ms in zip(wallets, mates):
        for m, score in ms:
            m = m.decode() if isinstance(m, bytes) else m
            edges[tuple(sorted((w, m)))] = max(edges.get(tuple(sorted((w, m))), 0), int(score))
    cluster = dump_cluster(stats, cohorts(wallets, edges, cfg.cohort_min_shared), cfg)
    cluster["as_of"] = as_of
    return {"smart_money": smart, "recycled_wallets": recycled, "dump_cluster": cluster, "as_of": as_of,
            "early_buyers_evaluated": len(buyers)}


# --- recording and resolution (paper-trading loop) -------------------------------------------

async def record_launch(session: AsyncSession, redis, mint: str, trades: list[Trade], created_ts: int | None,
                        stream_started_ts: int | None, now: datetime, cfg: WalletConfig) -> int:
    """Writes the first buyers of `mint` (only when the held history starts
    at the launch). Returns how many rows were written."""
    held = sorted((t for t in trades if t.at <= now), key=lambda x: x.at)
    if not lf.coverage(held, created_ts, stream_started_ts)["complete"]:
        return 0
    buyers = early_buyers(held, cfg.early_buyers)
    if not buyers:
        return 0
    created = datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None
    rows = []
    for b in buyers:
        share, covered = early_sold_share(held, b["wallet"], b["first_buy_at"], cfg.early_sell_seconds, b["tokens"])
        closed = now >= b["first_buy_at"] + timedelta(seconds=cfg.early_sell_seconds)
        rows.append({"mint": mint, "wallet": b["wallet"], "rank": b["rank"], "launch_created_at": created,
                     "first_buy_at": b["first_buy_at"], "sol_in": Decimal(b["sol_lamports"]) / LAMPORTS,
                     "tokens_in": Decimal(b["tokens"]), "sold_share_early": None if share is None else Decimal(str(round(share, 4))),
                     "sold_early": _sold_flag(share, covered, closed, cfg), "early_window_closed": closed, "recorded_at": now})
    await session.execute(insert(LaunchBuyer).values(rows).on_conflict_do_nothing(
        index_elements=["mint", "wallet"]))
    pipe = redis.pipeline()
    for b in buyers:
        pipe.zadd(SEEN + b["wallet"], {mint: b["first_buy_at"].timestamp()})
        pipe.zremrangebyscore(SEEN + b["wallet"], "-inf", now.timestamp() - SEEN_RETENTION)
        pipe.expire(SEEN + b["wallet"], SEEN_RETENTION)
    await pipe.execute()
    return len(rows)


def _sold_flag(share: float | None, covered: bool, closed: bool, cfg: WalletConfig) -> bool | None:
    if share is not None and share >= cfg.sold_early_share:
        return True  # sells only accumulate: already enough
    if not closed:
        return None
    return False if covered and share is not None else None  # part of the window was trimmed: unknown


async def update_early(session: AsyncSession, mint: str, trades: list[Trade], now: datetime, cfg: WalletConfig) -> int:
    """Refreshes the early-sell share of buyers whose window was open."""
    rows = (await session.execute(select(LaunchBuyer).where(LaunchBuyer.mint == mint,
                                                             LaunchBuyer.early_window_closed.is_(False)))).scalars().all()
    for r in rows:
        share, covered = early_sold_share(trades, r.wallet, r.first_buy_at, cfg.early_sell_seconds, int(r.tokens_in))
        prev = float(r.sold_share_early) if r.sold_share_early is not None else None
        best = max(x for x in (share, prev) if x is not None) if share is not None or prev is not None else None
        closed = now >= r.first_buy_at + timedelta(seconds=cfg.early_sell_seconds)
        r.sold_share_early = None if best is None else Decimal(str(round(best, 4)))
        r.sold_early = _sold_flag(best, covered, closed, cfg)
        r.early_window_closed = closed
    return len(rows)


async def resolve(session: AsyncSession, redis, mint: str, peak_pct, drawdown_pct, migrated: bool | None,
                  now: datetime, cfg: WalletConfig) -> dict[str, Any] | None:
    """Writes the launch outcome to its unresolved buyers and only then adds
    it to the Redis counters (so it informs decisions after `now` only)."""
    outcome = launch_outcome(peak_pct, drawdown_pct, cfg)
    if outcome is None:
        return None
    rows = (await session.execute(select(LaunchBuyer).where(LaunchBuyer.mint == mint,
                                                             LaunchBuyer.outcome_resolved_at.is_(None)))).scalars().all()
    if not rows:
        return None
    await session.execute(update(LaunchBuyer).where(LaunchBuyer.mint == mint, LaunchBuyer.outcome_resolved_at.is_(None))
                          .values(outcome=outcome, outcome_peak_pct=peak_pct, outcome_drawdown_pct=drawdown_pct,
                                  outcome_migrated=migrated, outcome_resolved_at=now))
    ttl = cfg.history_days * 86400
    dumpers = [r.wallet for r in rows if r.sold_early and outcome != WIN]
    pipe = redis.pipeline()
    for r in rows:
        pipe.hincrby(REP + r.wallet, "n", 1)
        pipe.hincrby(REP + r.wallet, {WIN: "wins", LOSS: "losses", FLAT: "flats"}[outcome], 1)
        if r.wallet in dumpers:
            pipe.hincrby(REP + r.wallet, "dumps", 1)
        pipe.expire(REP + r.wallet, ttl)
    pipe.hincrby(BASE, "n", len(rows))
    if outcome == WIN:
        pipe.hincrby(BASE, "wins", len(rows))
    for a in dumpers:
        for b in dumpers:
            if a != b:
                pipe.zincrby(MATES + a, 1, b)
        pipe.expire(MATES + a, ttl)
    await pipe.execute()
    return {"outcome": outcome, "buyers": len(rows), "dumpers": len(dumpers)}


async def reputation_asof(session: AsyncSession, wallets: list[str], t: datetime, history_days: int = 30) -> dict[str, dict[str, int]]:
    """The same counters rebuilt from the database for a past time `t`
    (outcomes resolved before t only): for datasets and audits."""
    if not wallets:
        return {}
    rows = (await session.execute(select(LaunchBuyer.wallet, LaunchBuyer.outcome, LaunchBuyer.sold_early).where(
        LaunchBuyer.wallet.in_(wallets), LaunchBuyer.outcome_resolved_at.is_not(None),
        LaunchBuyer.outcome_resolved_at < t, LaunchBuyer.outcome_resolved_at >= t - timedelta(days=history_days)))).all()
    out: dict[str, dict[str, int]] = {}
    for wallet, outcome, sold in rows:
        s = out.setdefault(wallet, {"n": 0, "wins": 0, "dumps": 0})
        s["n"] += 1
        s["wins"] += outcome == WIN
        s["dumps"] += bool(sold) and outcome != WIN
    return out
