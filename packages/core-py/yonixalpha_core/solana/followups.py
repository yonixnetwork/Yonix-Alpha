"""Later snapshots of every observed fresh token (traded or not):

  T0 / T+half / T+window   already in token_observations.report (checkpoints)
  T+5m, T+10m, T+30m, T+60m after launch   this job, into token_observations.followups
  migration                when the curve completed / a PumpSwap pool appeared
  final                    at T+60m: price change vs the decision, migrated or not

Prices are decimals-free (lamports per raw token unit), the same unit as the
observation checkpoints, so the change against the decision needs no token
metadata. Before migration the source is the curve from the trade stream
(price at its last trade, with that trade's time); after migration the
canonical PumpSwap pool's reserves over RPC. A snapshot that cannot be
priced records why ("unavailable"), never a guess.

Observation data only: nothing here changes a decision or trains a model;
the existing controlled ML process decides what it uses.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import TokenObservation
from yonixalpha_core.solana import pump_stream, pumpswap

OFFSETS = (("T+5m", 300), ("T+10m", 600), ("T+30m", 1800), ("T+60m", 3600))
LATE_AFTER_SECONDS = 600  # a snapshot taken this long after its mark is flagged late
POOL_LOOKUPS_PER_RUN = 5  # RPC budget per tick for migrated tokens
LAMPORTS = Decimal(1_000_000_000)


def decision_price_raw(report: dict | None) -> Decimal | None:
    """The price at the observation decision: its last checkpoint."""
    for cp in reversed((report or {}).get("checkpoints") or []):
        if cp.get("price_raw"):
            return Decimal(str(cp["price_raw"]))
    return None


async def snapshot(redis, rpc, mint: str, now: datetime, pool_budget: list[int]) -> dict[str, Any] | None:
    """Current price/liquidity of the token, or None when the pool lookup
    budget for this run is spent (retried next run)."""
    curve = await pump_stream.load_curve(redis, mint)
    migrated_ts = await redis.zscore(pump_stream.MIGRATED, mint)
    if curve is not None and not curve.pool and not curve.complete and curve.vtok:
        return {"price_raw": str(Decimal(curve.vsol) / Decimal(curve.vtok)),
                "liquidity_sol": str(Decimal(curve.rsol) / LAMPORTS) if curve.rsol is not None else None,
                "source": "pump_stream curve (last trade)", "price_at": curve.updated_at.isoformat() if curve.updated_at else None,
                "migrated": False}
    if curve is None and migrated_ts is None:
        return {"unavailable": "no stream data for this token (never traded since launch, or expired)", "migrated": False}
    if rpc is None:
        return {"unavailable": "token migrated; no RPC configured for the pool", "migrated": True}
    if pool_budget[0] <= 0:
        return None
    pool_budget[0] -= 1
    try:
        state = await pumpswap.fetch_pool(rpc, mint, now, None, 6)
    except pumpswap.PoolUnavailable as exc:
        return {"unavailable": str(exc)[:160], "migrated": True}
    except Exception as exc:  # noqa: BLE001 - a failed source is recorded, never guessed around
        return {"unavailable": f"pool lookup failed: {type(exc).__name__}", "migrated": True}
    return {"price_raw": str(Decimal(state.quote_reserve_lamports) / Decimal(state.base_reserve_raw)),
            "liquidity_sol": str(state.liquidity_sol), "source": "PumpSwap pool reserves (RPC)", "price_at": now.isoformat(),
            "migrated": True, "pool": state.pool}


async def track_observation_followups(session: AsyncSession, redis, rpc, now: datetime, limit: int = 300) -> int:
    """Fills the due snapshots of recently observed tokens. Returns how many
    tokens were updated."""
    rows = (await session.execute(
        select(TokenObservation)
        .where(TokenObservation.launched_at.is_not(None),
               TokenObservation.launched_at >= now - timedelta(seconds=OFFSETS[-1][1] + 2 * LATE_AFTER_SECONDS),
               TokenObservation.launched_at <= now - timedelta(seconds=OFFSETS[0][1]),
               or_(TokenObservation.followups.is_(None), ~TokenObservation.followups.has_key("final")))
        .order_by(TokenObservation.launched_at).limit(limit)
    )).scalars().all()
    budget = [POOL_LOOKUPS_PER_RUN]
    updated = 0
    for row in rows:
        launched = row.launched_at if row.launched_at.tzinfo else row.launched_at.replace(tzinfo=timezone.utc)
        f = dict(row.followups or {})
        due = [(label, sec) for label, sec in OFFSETS if label not in f and now >= launched + timedelta(seconds=sec)]
        if not due:
            continue
        snap = await snapshot(redis, rpc, row.mint, now, budget)
        if snap is None:
            continue
        base = decision_price_raw(row.report)
        for label, sec in due:
            rec = {**snap, "at": now.isoformat(), "after_launch_seconds": int((now - launched).total_seconds()),
                   "late": (now - launched).total_seconds() - sec > LATE_AFTER_SECONDS}
            if base and base > 0 and snap.get("price_raw"):
                rec["change_vs_decision_pct"] = str(((Decimal(snap["price_raw"]) / base - 1) * 100).quantize(Decimal("0.01")))
            f[label] = rec
        if snap.get("migrated") and "migration" not in f:
            ts = await redis.zscore(pump_stream.MIGRATED, row.mint)
            f["migration"] = {"detected_at": now.isoformat(), "pool": snap.get("pool"),
                              "migrated_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None}
        if "T+60m" in f:
            last = f["T+60m"]
            f["final"] = {"outcome_at_decision": row.outcome, "migrated": "migration" in f,
                          "change_vs_decision_pct_60m": last.get("change_vs_decision_pct"),
                          "liquidity_sol_60m": last.get("liquidity_sol"), "priced": "price_raw" in last,
                          "recorded_at": now.isoformat()}
        row.followups = f
        updated += 1
    await session.commit()
    return updated
