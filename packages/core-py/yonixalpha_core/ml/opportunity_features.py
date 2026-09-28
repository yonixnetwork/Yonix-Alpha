"""Decision-time features of an opportunity ledger row, for the multi-target
SHADOW models (services/ml/app/shadow_ml.py).

Only the snapshot recorded AT the decision is read (the gate / observation
state and the causal launch intelligence, whose `as_of` must not be after
the decision; an intel record stamped later is dropped as look-ahead).
Nothing after the decision (horizons, peak, labels, trade result) is a
feature.

Missing is not zero: every value that can be unknown has a `<name>__missing`
indicator, and the value itself is None here (the trainer imputes it with
the training median, never with 0 by default).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

FEATURE_VERSION = "oppfeat-2026.09.1"

RISK = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}
LEVEL = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
FLOW_STATES = ("EXPANSION", "HEALTHY_CONSOLIDATION", "DISTRIBUTION", "DETERIORATION", "QUIET", "MIXED")
POST_MIG = ("DUMPING", "STABILIZING", "RECOVERING", "CONTINUING", "WEAK")

NUMERIC = ("market_cap_sol", "liquidity_sol", "age_seconds", "buyers", "sellers", "buy_volume_sol", "sell_volume_sol",
           "trades", "volatility", "top10_share", "creator_share", "creator_launches_24h", "signal_strength",
           "price_age_seconds", "risk", "now_curve_progress", "now_unique_buyers", "now_top3_buy_share",
           "now_buy_sell_ratio", "now_net_flow_sol", "breadth_score", "mom_return_1m", "mom_return_5m",
           "mom_buyer_acceleration", "mom_trade_rate_acceleration", "mom_volume_acceleration", "manipulation_level",
           "manipulation_families", "mayhem", "reach5_trades", "smart_proven_wallets", "smart_proven_share",
           "dump_cluster_level", "recycled_wallets")
BINARY = ("is_gate", "is_momentum", "is_migrated") + tuple(f"flow_{s}" for s in FLOW_STATES) + tuple(f"pm_{s}" for s in POST_MIG)
MISSING_OF = NUMERIC
FEATURE_NAMES: tuple[str, ...] = NUMERIC + tuple(f"{n}__missing" for n in MISSING_OF) + BINARY


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def causal_intel(snapshot: dict[str, Any], decided_at: datetime) -> dict[str, Any] | None:
    intel = snapshot.get("intel")
    if not isinstance(intel, dict) or "error" in intel:
        return None
    as_of = intel.get("as_of")
    try:
        if as_of and datetime.fromisoformat(as_of) > decided_at:
            return None  # stamped after the decision: look-ahead, never used
    except ValueError:
        return None
    return intel


def features(row_snapshot: dict[str, Any], decided_at: datetime, engine: str, stage: str) -> dict[str, float | None]:
    s = row_snapshot or {}
    intel = causal_intel(s, decided_at) or {}
    now = intel.get("now") or {}
    mom = intel.get("momentum") or {}
    man = intel.get("manipulation") or {}
    wallets = intel.get("wallets") or {}
    smart = wallets.get("smart_money") or {}
    reach = ((intel.get("trades_to_reach") or {}).get("checkpoints") or {}).get("5") or {}
    mayhem = (intel.get("regime") or {}).get("mayhem")
    dc = (wallets.get("dump_cluster") or {}).get("level")
    raw: dict[str, float | None] = {
        "market_cap_sol": _f(s.get("market_cap_sol")), "liquidity_sol": _f(s.get("liquidity_sol")),
        "age_seconds": _f(s.get("age_seconds")), "buyers": _f(s.get("buyers")), "sellers": _f(s.get("sellers")),
        "buy_volume_sol": _f(s.get("buy_volume_sol")), "sell_volume_sol": _f(s.get("sell_volume_sol")),
        "trades": _f(s.get("trades")), "volatility": _f(s.get("volatility")), "top10_share": _f(s.get("top10_share")),
        "creator_share": _f(s.get("creator_share")), "creator_launches_24h": _f(s.get("creator_launches_24h")),
        "signal_strength": _f(s.get("signal_strength")), "price_age_seconds": _f(s.get("price_age_seconds")),
        "risk": _f(RISK.get(s.get("overall_risk") or "")),
        "now_curve_progress": _f(now.get("curve_progress")), "now_unique_buyers": _f(now.get("unique_buyers")),
        "now_top3_buy_share": _f(now.get("top3_buy_share")), "now_buy_sell_ratio": _f(now.get("buy_sell_ratio")),
        "now_net_flow_sol": _f(now.get("net_flow_sol")),
        "breadth_score": _f((intel.get("buyer_breadth") or {}).get("score")),
        "mom_return_1m": _f(mom.get("return_1m")), "mom_return_5m": _f(mom.get("return_5m")),
        "mom_buyer_acceleration": _f(mom.get("buyer_acceleration")),
        "mom_trade_rate_acceleration": _f(mom.get("trade_rate_acceleration")),
        "mom_volume_acceleration": _f(mom.get("volume_acceleration")),
        "manipulation_level": _f(LEVEL.get(man.get("level") or "")),
        "manipulation_families": _f(man.get("count")) if man.get("level") not in (None, "UNKNOWN") else None,
        "mayhem": _f(mayhem),
        "reach5_trades": _f(reach.get("trades")),
        "smart_proven_wallets": _f(smart.get("proven_wallets")) if smart.get("status") == "MEASURED" else None,
        "smart_proven_share": _f(smart.get("proven_share_of_buy_volume")) if smart.get("status") == "MEASURED" else None,
        "dump_cluster_level": _f(LEVEL.get(dc or "")),
        "recycled_wallets": _f(len(wallets["recycled_wallets"])) if isinstance(wallets.get("recycled_wallets"), list)
        and wallets.get("dump_cluster", {}).get("level") != "UNKNOWN" else None,
    }
    out: dict[str, float | None] = dict(raw)
    for n in MISSING_OF:
        out[f"{n}__missing"] = 1.0 if raw[n] is None else 0.0
    flow = (intel.get("flow_state") or {}).get("state")
    pm = (intel.get("post_migration") or {}).get("state")
    out["is_gate"] = 1.0 if stage == "GATE" else 0.0
    out["is_momentum"] = 1.0 if engine == "solana_momentum" else 0.0
    out["is_migrated"] = 1.0 if s.get("migrated") or intel.get("stage") == "MIGRATED" else 0.0
    for st in FLOW_STATES:
        out[f"flow_{st}"] = 1.0 if flow == st else 0.0
    for st in POST_MIG:
        out[f"pm_{st}"] = 1.0 if pm == st else 0.0
    return out
