"""One intelligence record per decision: the launch features, regime flags
and manipulation score, computed from data available at the decision time
and stored with the assessment (inputs_snapshot["intel"]) and the
opportunity ledger.

Every section says where its data came from and when (`as_of`), and what
could not be measured (`unknown`). The gate acts only on the regime and
manipulation sections, through settings; the rest is evidence and ML input.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from yonixalpha_core.solana import launch_features as lf
from yonixalpha_core.solana import manipulation, manufactured_pump, wallet_graph
from yonixalpha_core.solana.flow import Trade

SNAPSHOT_GRID = (0, 5, 10, 20, 30, 60, 120, 300, 600)
STREAM_STALE_SECONDS = 60


def coverage_status(trades: list[Trade], now: datetime, cov: dict[str, Any] | None, *, created_ts: int | None,
                    stream_heartbeat: datetime | None, source: str = "pump_stream") -> dict[str, Any]:
    """Whether this token was actually observed continuously, for ML to tell
    an observed zero from a missing value.
    COMPLETE  full history from creation held, stream alive
    PARTIAL   history does not start at creation (stream started later, or
              older trades trimmed)
    STALE     the stream has not delivered anything recently
    UNAVAILABLE  no trades held
    (GAPPED needs recorded stream outages, which the stream does not keep
    yet: an outage inside the history is not detectable here.)"""
    held = sorted((t for t in trades if t.at <= now), key=lambda t: t.at)
    hb_age = round((now - stream_heartbeat).total_seconds(), 1) if stream_heartbeat else None
    gaps = [(b.at - a.at).total_seconds() for a, b in zip(held, held[1:])]
    if not held:
        status = "UNAVAILABLE"
    elif hb_age is None or hb_age > STREAM_STALE_SECONDS:
        status = "STALE"
    elif cov and cov.get("complete") is True:
        status = "COMPLETE"
    else:
        status = "PARTIAL"
    return {"coverage_status": status, "discovery_source": source, "data_source": "pump.fun program log stream",
            "first_seen_at": (datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat() if created_ts
                              else held[0].at.isoformat() if held else None),
            "first_trade_at": held[0].at.isoformat() if held else None,
            "last_seen_at": held[-1].at.isoformat() if held else None,
            "observation_count": len(held), "largest_trade_gap_seconds": round(max(gaps), 1) if gaps else None,
            "stream_heartbeat_age_seconds": hb_age,
            "reason": (cov or {}).get("reason") if status == "PARTIAL" else None,
            "note": "a trade gap is the token's own quiet time, not a data gap; GAPPED is not detectable yet"}


async def add_relationships(rec: dict[str, Any], redis, trades: list[Trade], now: datetime, s: Any, *,
                            creator: str | None, mint: str | None, wallets: dict | None, keep_wallets: int = 10) -> None:
    """rec["relationships"]: wallet_graph analysis from cached funding facts
    (Redis only, no RPC) and the wallet-intel cohorts / proven wallets.
    A failure is recorded in place of it."""
    try:
        held = [t for t in trades if t.at <= now]
        ctx = await wallet_graph.gather(redis, list(dict.fromkeys(t.trader for t in held)), creator)
        w = wallets or {}
        rec["relationships"] = wallet_graph.compact(wallet_graph.analyse(
            held, now, ctx, creator=creator, mint=mint, smart=w.get("smart_money"),
            cohorts=(w.get("dump_cluster") or {}).get("cohorts"), min_attribution=float(s.organic_min_attribution_share)),
            wallets=keep_wallets)
    except Exception as exc:  # noqa: BLE001 - evidence, never guessed
        rec["relationships"] = {"status": "UNAVAILABLE", "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                                "graph_version": wallet_graph.GRAPH_VERSION, "as_of": now.isoformat()}


def _mayhem(meta: dict | None, curve: Any | None, curve_from_chain: bool) -> bool | None:
    if curve is not None and curve_from_chain:
        return bool(getattr(curve, "is_mayhem_mode", False))
    if meta and meta.get("is_mayhem_mode") not in (None, ""):
        return str(meta["is_mayhem_mode"]) in ("1", "true", "True")
    return None


def manipulation_config(s: Any) -> manipulation.ManipulationConfig:
    return manipulation.ManipulationConfig(
        window_seconds=int(s.manipulation_window_seconds), round_trip_share=float(s.manipulation_round_trip_share),
        sync_buy_wallets=max(2, int(s.max_sync_buy_cluster) - 1), sync_sell_wallets=int(s.manipulation_sync_sell_wallets),
        regular_size_cv=float(s.manipulation_regular_size_cv), dust_share=float(s.manipulation_dust_share),
        linear_r2=float(s.manipulation_linear_r2), collapse_pct=float(s.manipulation_collapse_pct),
        high_families=int(s.manipulation_high_families),
        not_counted=frozenset(f for f, on in (
            ("synchronized_sells", s.manipulation_count_synchronized_sells),
            ("synchronized_buys", s.manipulation_count_synchronized_buys),
            ("single_second_collapse", s.manipulation_count_single_second_collapse),
            ("dust_volume", s.manipulation_count_dust_volume)) if not on))


def curve_intel(trades: list[Trade], now: datetime, s: Any, *, meta: dict | None, curve: Any | None, curve_from_chain: bool,
                decimals: int | None, supply_raw: int | None, stream_started_ts: int | None, funding: dict | None,
                duplicate_of: str | None, dump_cluster: dict | None = None, recycled_wallets: set[str] | None = None,
                engine: str = "solana_fresh", stream_heartbeat: datetime | None = None) -> dict[str, Any]:
    """Bonding-curve token (fresh or momentum)."""
    created_ts = int(meta["created_at"]) if meta and meta.get("created_at") else None
    mayhem = _mayhem(meta, curve, curve_from_chain)
    held = [t for t in trades if t.at <= now]
    cm = lf.curve_math(held, mayhem)
    cov = lf.coverage(held, created_ts, stream_started_ts)
    dec = decimals if decimals is not None else 6
    # Progress / SOL accumulated / checkpoints use today's standard opening
    # reserves: only computed when the curve holds k AND it is the standard k.
    std = bool(cm["valid"] and cm.get("standard_k"))
    out: dict[str, Any] = {
        "feature_version": lf.FEATURE_VERSION, "as_of": now.isoformat(), "source": "pump_stream trades + curve",
        "stage": "MOMENTUM" if engine == "solana_momentum" else "FRESH",
        "regime": {"mayhem": mayhem, "curve_math": cm, "data_regime": lf.data_regime(now),
                   "metadata_host": _host(meta.get("uri") if meta else None)},
        "coverage": cov,
    }
    if created_ts is not None:
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        offsets = [o for o in SNAPSHOT_GRID if o <= int(s.intel_snapshot_seconds)]
        out["snapshots"] = lf.snapshot_series(held, created, now, offsets, decimals=dec, supply_raw=supply_raw,
                                              curve_valid=std)
        out["now"] = lf.snapshot(held, created, now, decimals=dec, supply_raw=supply_raw, curve_valid=std,
                                 interval_seconds=60, prev=None)
    else:
        out["snapshots"], out["now"] = [], {"unknown": {"all": "creation time unknown"}}
    out["trades_to_reach"] = lf.trades_to_reach(held, now, complete_history=cov["complete"], curve_valid=std,
                                                meaningful_sol=float(s.intel_meaningful_buy_sol))
    out["buyer_breadth"] = lf.buyer_breadth(held, now, target_buyers=int(s.intel_buyer_breadth_target),
                                            meaningful_sol=float(s.intel_meaningful_buy_sol), recycled_wallets=recycled_wallets)
    out["flow_state"] = lf.flow_state(held, now, decimals=dec)
    out["momentum"] = lf.momentum(held, now, dec)
    out["manipulation"] = manipulation.score(held, now, manipulation_config(s), funding=funding, dump_cluster=dump_cluster,
                                             duplicate_of=duplicate_of)
    out["manufactured_pump"] = manufactured_pump.detect(held, now, manufactured_pump.config(s))
    out["observation"] = coverage_status(held, now, cov, created_ts=created_ts, stream_heartbeat=stream_heartbeat)
    return out


def pool_intel(pool_trades: list[Trade], curve_trades: list[Trade], now: datetime, s: Any, *, meta: dict | None,
               migrated_at: datetime | None, decimals: int | None, funding: dict | None, duplicate_of: str | None,
               dump_cluster: dict | None = None) -> dict[str, Any]:
    """Migrated token (PumpSwap)."""
    created_ts = int(meta["created_at"]) if meta and meta.get("created_at") else None
    migrated_ts = int(migrated_at.timestamp()) if migrated_at else None
    held = [t for t in pool_trades if t.at <= now]
    dec = decimals if decimals is not None else 6
    ib = lf.instant_bond(created_ts, migrated_ts, int(s.instant_bond_seconds))
    boost = lf.boost_window(migrated_at, now, int(s.boost_window_seconds))
    return {
        "feature_version": lf.FEATURE_VERSION, "as_of": now.isoformat(), "source": "rpc:pumpswap trades + migration event",
        "stage": "MIGRATED",
        "regime": {"mayhem": _mayhem(meta, None, False), "instant_bond": ib, "boost_window": boost,
                   "data_regime": lf.data_regime(now), "migrated_at": migrated_at.isoformat() if migrated_at else None,
                   "seconds_since_migration": round((now - migrated_at).total_seconds(), 1) if migrated_at else None,
                   "metadata_host": _host(meta.get("uri") if meta else None)},
        "post_migration": lf.post_migration_state(held, migrated_at, now, decimals=dec),
        "flow_state": lf.flow_state(held, now, decimals=dec),
        "momentum": lf.momentum(held, now, dec),
        "curve_history": {"trades_before_migration": len(curve_trades)},
        "manipulation": manipulation.score(held, now, manipulation_config(s), funding=funding, dump_cluster=dump_cluster,
                                           duplicate_of=duplicate_of),
        "manufactured_pump": manufactured_pump.detect(held, now, manufactured_pump.config(s)),
    }


def _host(uri: str | None) -> str | None:
    """Metadata host (e.g. ipfs.io vs a bot deployer's own endpoint): a
    creation-pathway proxy recorded as a feature only (research R7)."""
    if not uri or "://" not in uri:
        return None
    return uri.split("://", 1)[1].split("/", 1)[0].lower()[:80] or None
