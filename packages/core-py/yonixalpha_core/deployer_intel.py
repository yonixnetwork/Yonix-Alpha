"""Deployer (creator) intelligence, time-aware.

What did this creator's earlier launches do? Launch count and recent
activity, bond (migration) rate and time to migration, peak market-cap
distribution, WIN / LOSS rates, how often the creator sold early, volume
and buyers — computed as of the decision time T from launches that were
created before T AND whose outcome had resolved at or before T. A launch
that resolves after T, or is created after T, never informs a decision at
T (no look-ahead; deployer_history_cutoff is stored with every result).

Rates are also given shrunk toward the base rate of all launches resolved
before T (Beta prior, strength PRIOR_STRENGTH), so one or two launches do
not make a deployer "good" or "bad"; below MIN_RESOLVED resolved launches
the status is INSUFFICIENT_HISTORY. Deployer history is one feature in
the decision and ML systems, never a BUY rule.

Source: launches this system observed (deployer_launches, filled by the
opportunity ledger in paper-trading). Coverage is bounded by what the
stream saw since it started; the on-chain creator launch count
(solana.creator_history) is a separate, live-only figure.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import DeployerLaunch

FEATURE_VERSION = "dep-1"
EARLY_SELL_SECONDS = 300
WINDOW_SECONDS = 3600
PRIOR_STRENGTH = 5.0
MIN_RESOLVED = 3
RECENT_N = 5
BASE_BUCKET_SECONDS = 600
LAMPORTS = Decimal(1_000_000_000)
_base_cache: dict[int, dict[str, float | int]] = {}


def _created(meta: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(meta["created_at"]), tz=timezone.utc) if meta.get("created_at") else None
    except (ValueError, TypeError):
        return None


async def note_launch(session: AsyncSession, mint: str, meta: dict, now: datetime) -> bool:
    """Records a launch the first time the ledger tracks it."""
    creator, created = meta.get("creator"), _created(meta)
    if not creator or created is None:
        return False
    res = await session.execute(insert(DeployerLaunch).values(
        mint=mint, creator=creator, launch_created_at=created, first_seen_at=now).on_conflict_do_nothing(index_elements=["mint"]))
    return bool(res.rowcount)


def launch_facts(trades: list, creator: str, created: datetime, now: datetime) -> dict[str, Any]:
    """Volume, buyers and creator behaviour over the launch's first hour
    (trades up to `now`). Creator facts are None when the held history
    does not start at the launch."""
    end = min(now, created + timedelta(seconds=WINDOW_SECONDS))
    xs = sorted((t for t in trades if created <= t.at <= end), key=lambda t: t.at)
    covered = bool(xs) and (xs[0].at - created).total_seconds() <= 10
    bought = sum(t.token_raw for t in xs if t.trader == creator and t.is_buy)
    sold = sum(t.token_raw for t in xs if t.trader == creator and not t.is_buy)
    early = any(t.trader == creator and not t.is_buy and (t.at - created).total_seconds() <= EARLY_SELL_SECONDS for t in xs)
    return {"volume_sol_60m": (Decimal(sum(t.sol_lamports for t in xs)) / LAMPORTS).quantize(Decimal("0.0001")) if xs else None,
            "unique_buyers_60m": len({t.trader for t in xs if t.is_buy}) if xs else None,
            "creator_sold_early": early if covered else None,
            "creator_sell_share": (Decimal(min(sold, bought)) / Decimal(bought)).quantize(Decimal("0.0001"))
            if covered and bought else None}


async def resolve(session: AsyncSession, mint: str, meta: dict, trades: list, *, outcome: str | None,
                  migrated_at: datetime | None, peak_mc_sol: Decimal | None, now: datetime) -> bool:
    """Writes the launch's outcome once (resolved_at = now)."""
    dl = await session.get(DeployerLaunch, mint)
    if dl is None:
        if not await note_launch(session, mint, meta, now):
            return False
        dl = await session.get(DeployerLaunch, mint)
    if dl is None or dl.resolved_at is not None:
        return False
    facts = launch_facts(trades, dl.creator, dl.launch_created_at, now)
    dl.volume_sol_60m, dl.unique_buyers_60m = facts["volume_sol_60m"], facts["unique_buyers_60m"]
    dl.creator_sold_early, dl.creator_sell_share = facts["creator_sold_early"], facts["creator_sell_share"]
    dl.migrated = migrated_at is not None
    dl.time_to_migration_seconds = int((migrated_at - dl.launch_created_at).total_seconds()) if migrated_at else None
    dl.peak_mc_sol, dl.outcome = peak_mc_sol, outcome
    dl.tracked_seconds = int((now - dl.launch_created_at).total_seconds())
    dl.resolved_at = now
    return True


async def _base_rates(session: AsyncSession, t: datetime) -> dict[str, float | int]:
    """Base LOSS / WIN / migration rates of all launches resolved by the
    start of t's 10-minute bucket (cached per bucket; the cutoff is never
    after t)."""
    bucket = int(t.timestamp()) // BASE_BUCKET_SECONDS
    if bucket in _base_cache:
        return _base_cache[bucket]
    cutoff = datetime.fromtimestamp(bucket * BASE_BUCKET_SECONDS, tz=timezone.utc)
    d = DeployerLaunch
    n, losses, wins, mig = (await session.execute(select(
        func.count(), func.count().filter(d.outcome == "LOSS"), func.count().filter(d.outcome == "WIN"),
        func.count().filter(d.migrated.is_(True))).where(d.resolved_at <= cutoff, d.outcome.is_not(None)))).one()
    base = {"n": int(n), "loss": (losses / n) if n else 0.5, "win": (wins / n) if n else 0.5,
            "migrated": (mig / n) if n else 0.05, "cutoff": cutoff.isoformat()}
    if len(_base_cache) > 64:
        _base_cache.clear()
    _base_cache[bucket] = base
    return base


def _shrunk(k: int, n: int, p: float) -> float:
    return round((k + PRIOR_STRENGTH * p) / (n + PRIOR_STRENGTH), 4)


def _q(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 4)


async def features_asof(session: AsyncSession, creator: str | None, t: datetime, *, exclude_mint: str | None = None) -> dict[str, Any]:
    """Deployer features at decision time `t` (see module docstring)."""
    computed = datetime.now(timezone.utc).isoformat()
    base_out = {"feature_version": FEATURE_VERSION, "deployer": creator, "deployer_history_cutoff": t.isoformat(),
                "deployer_feature_timestamp": computed,
                "source": "launches this system observed (deployer_launches); outcomes resolved at or before the cutoff"}
    if not creator:
        return {**base_out, "status": "UNKNOWN", "reason": "creator wallet unknown"}
    t0 = time.monotonic()
    d = DeployerLaunch
    q = select(d).where(d.creator == creator, d.launch_created_at < t)
    if exclude_mint:
        q = q.where(d.mint != exclude_mint)
    prior = (await session.execute(q.order_by(d.launch_created_at))).scalars().all()
    resolved = [r for r in prior if r.resolved_at is not None and r.resolved_at <= t and r.outcome is not None]
    last = prior[-1].launch_created_at if prior else None
    out: dict[str, Any] = {
        **base_out,
        "deployer_launch_count": len(prior),
        "launches_last_1h": sum(1 for r in prior if r.launch_created_at >= t - timedelta(hours=1)),
        "launches_last_24h": sum(1 for r in prior if r.launch_created_at >= t - timedelta(days=1)),
        "launches_last_7d": sum(1 for r in prior if r.launch_created_at >= t - timedelta(days=7)),
        "last_launch_seconds_ago": round((t - last).total_seconds(), 1) if last else None,
        "resolved_launches": len(resolved),
    }
    if not prior:
        return {**out, "status": "NO_HISTORY", "reason": "no earlier launch by this creator seen by this system",
                "query_ms": int((time.monotonic() - t0) * 1000)}
    if not resolved:
        return {**out, "status": "INSUFFICIENT_HISTORY", "reason": "earlier launches have not resolved yet",
                "query_ms": int((time.monotonic() - t0) * 1000)}
    base = await _base_rates(session, t)
    n = len(resolved)
    wins = sum(1 for r in resolved if r.outcome == "WIN")
    losses = sum(1 for r in resolved if r.outcome == "LOSS")
    migrated = sum(1 for r in resolved if r.migrated)
    sells = [r.creator_sold_early for r in resolved if r.creator_sold_early is not None]
    peaks = [float(r.peak_mc_sol) for r in resolved if r.peak_mc_sol is not None]
    ttm = [r.time_to_migration_seconds for r in resolved if r.time_to_migration_seconds is not None]
    recent = sorted(resolved, key=lambda r: r.launch_created_at)[-RECENT_N:]
    out.update({
        "status": "MEASURED" if n >= MIN_RESOLVED else "INSUFFICIENT_HISTORY",
        "deployer_bond_rate": round(migrated / n, 4), "deployer_bond_rate_shrunk": _shrunk(migrated, n, base["migrated"]),
        "deployer_win_rate": round(wins / n, 4), "deployer_loss_rate": round(losses / n, 4),
        "deployer_recent_success_rate": round(sum(1 for r in recent if r.outcome == "WIN") / len(recent), 4),
        "deployer_creator_sell_rate": round(sum(sells) / len(sells), 4) if sells else None,
        "deployer_median_time_to_migration_seconds": statistics.median(ttm) if ttm else None,
        "deployer_peak_mc_sol": {"p25": _q(peaks, 0.25), "median": _q(peaks, 0.5), "p75": _q(peaks, 0.75), "n": len(peaks)},
        "deployer_median_volume_sol_60m": _q([float(r.volume_sol_60m) for r in resolved if r.volume_sol_60m is not None], 0.5),
        "deployer_median_buyers_60m": _q([float(r.unique_buyers_60m) for r in resolved if r.unique_buyers_60m is not None], 0.5),
        "deployer_outcomes": {"WIN": wins, "FLAT": n - wins - losses, "LOSS": losses},
        # Loss rate shrunk toward the base rate: the transparent risk score.
        "deployer_risk_score": _shrunk(losses, n, base["loss"]),
        "base_rates": base, "prior_strength": PRIOR_STRENGTH, "min_resolved": MIN_RESOLVED,
        "query_ms": int((time.monotonic() - t0) * 1000),
    })
    if n < MIN_RESOLVED:
        out["reason"] = f"{n} resolved earlier launch(es); {MIN_RESOLVED} needed before the history is judged"
    return out
