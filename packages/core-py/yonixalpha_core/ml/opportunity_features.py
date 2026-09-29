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

FEATURE_VERSION = "oppfeat-2026.09.2"

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
# Scanner intelligence (solana.wallet_graph, deployer_intel, solana.
# manufactured_pump). Wallet identities and cluster ids are deliberately NOT
# features: the models learn from relationship structure, never "wallet X
# bought" (no wallet-following overfit).
SCANNER = ("raw_unique_buyers", "effective_unique_buyers", "creator_related_buyers", "coordinated_buyers",
           "independent_buyers", "funding_cluster_size", "funding_fanout", "funding_time_concentration",
           "common_funder_count", "funding_known_share",
           "organic_demand_ratio", "organic_ratio_lower", "organic_ratio_upper", "creator_volume_ratio",
           "cluster_volume_ratio", "independent_volume_ratio", "net_sol_flow_30s", "net_sol_flow_60s",
           "organic_net_sol_flow_30s", "raw_volume_acceleration", "organic_volume_acceleration",
           "effective_buyer_acceleration",
           "smart_money_quality", "smart_money_independence", "smart_money_signal_strength",
           "deployer_launch_count", "deployer_resolved_launches", "deployer_bond_rate", "deployer_risk_score",
           "deployer_recent_success_rate", "deployer_creator_sell_rate", "deployer_median_peak_mc", "deployer_launches_24h",
           "manufactured_pump_score", "manufactured_pattern_duration", "log_price_r2", "buy_sell_ratio_stability",
           "positive_return_share", "observation_complete")
NUMERIC = NUMERIC + SCANNER
BINARY = ("is_gate", "is_momentum", "is_migrated") + tuple(f"flow_{s}" for s in FLOW_STATES) + tuple(f"pm_{s}" for s in POST_MIG)
MISSING_OF = NUMERIC
FEATURE_NAMES: tuple[str, ...] = NUMERIC + tuple(f"{n}__missing" for n in MISSING_OF) + BINARY

# Feature groups for ablation (services/ml/app/ablation.py). A group's
# __missing indicators travel with it.
GROUPS: dict[str, tuple[str, ...]] = {
    "price_volume_buyers": ("market_cap_sol", "liquidity_sol", "age_seconds", "buyers", "sellers", "buy_volume_sol",
                            "sell_volume_sol", "trades", "volatility", "price_age_seconds", "now_curve_progress",
                            "now_unique_buyers", "now_buy_sell_ratio", "now_net_flow_sol", "mom_return_1m", "mom_return_5m",
                            "mom_buyer_acceleration", "mom_trade_rate_acceleration", "mom_volume_acceleration"),
    "wallet_graph": ("raw_unique_buyers", "effective_unique_buyers", "creator_related_buyers", "coordinated_buyers",
                     "independent_buyers", "funding_cluster_size", "funding_fanout", "funding_time_concentration",
                     "common_funder_count", "funding_known_share", "recycled_wallets", "dump_cluster_level"),
    "deployer": ("deployer_launch_count", "deployer_resolved_launches", "deployer_bond_rate", "deployer_risk_score",
                 "deployer_recent_success_rate", "deployer_creator_sell_rate", "deployer_median_peak_mc",
                 "deployer_launches_24h", "creator_share", "creator_launches_24h"),
    "organic_demand": ("organic_demand_ratio", "organic_ratio_lower", "organic_ratio_upper", "creator_volume_ratio",
                       "cluster_volume_ratio", "independent_volume_ratio", "net_sol_flow_30s", "net_sol_flow_60s",
                       "organic_net_sol_flow_30s", "raw_volume_acceleration", "organic_volume_acceleration",
                       "effective_buyer_acceleration"),
    "manipulation": ("manipulation_level", "manipulation_families", "manufactured_pump_score", "manufactured_pattern_duration",
                     "log_price_r2", "buy_sell_ratio_stability", "positive_return_share"),
    "smart_money": ("smart_proven_wallets", "smart_proven_share", "smart_money_quality", "smart_money_independence",
                    "smart_money_signal_strength"),
}
# Where each scanner feature comes from, and which timestamp bounds it.
SOURCES: dict[str, dict[str, str]] = {
    "wallet_graph": {"source": "intel.relationships (pump stream trades + cached funding lookups)", "timestamp": "relationships.as_of"},
    "organic_demand": {"source": "intel.relationships.demand / flows / acceleration", "timestamp": "relationships.as_of"},
    "smart_money": {"source": "intel.wallets.smart_money + intel.relationships.smart_money", "timestamp": "wallets.as_of"},
    "deployer": {"source": "intel.deployer (deployer_launches resolved before the decision)",
                 "timestamp": "deployer.deployer_history_cutoff"},
    "manipulation": {"source": "intel.manipulation + intel.manufactured_pump", "timestamp": "manufactured_pump.as_of"},
}


def group_features(group: str, full: bool = True) -> tuple[str, ...]:
    """A group's features plus their __missing indicators."""
    names = GROUPS[group]
    return names + tuple(f"{n}__missing" for n in names if n in MISSING_OF) if full else names


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
    raw.update(_scanner(intel, decided_at))
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


def _asof_ok(block: dict, key: str, decided_at: datetime) -> bool:
    """A block stamped after the decision is look-ahead: never used."""
    v = block.get(key)
    if not v:
        return True
    try:
        return datetime.fromisoformat(v) <= decided_at
    except (TypeError, ValueError):
        return False


def _scanner(intel: dict[str, Any], decided_at: datetime) -> dict[str, float | None]:
    out: dict[str, float | None] = dict.fromkeys(SCANNER)
    rel = intel.get("relationships") or {}
    if rel.get("status") == "MEASURED" and _asof_ok(rel, "as_of", decided_at):
        b, d, c = rel.get("buyers") or {}, rel.get("demand") or {}, rel.get("creator") or {}
        cov = rel.get("coverage") or {}
        dep = [x for x in rel.get("clusters") or [] if x.get("classification") in ("COORDINATED", "FUNDING_RELATED")]
        top = max(dep, key=lambda x: x.get("size") or 0) if dep else None
        total = _f(d.get("total_volume_sol"))
        cluster_vol = (_f(d.get("funding_cluster_volume_sol")) or 0) + (_f(d.get("coordinated_volume_sol")) or 0)
        f30, f60 = (rel.get("flows") or {}).get("30s") or {}, (rel.get("flows") or {}).get("60s") or {}
        acc = rel.get("acceleration") or {}
        out.update({
            "raw_unique_buyers": _f(b.get("raw_unique_buyers")), "effective_unique_buyers": _f(b.get("effective_unique_buyers")),
            "creator_related_buyers": _f(b.get("creator_related_buyers")), "coordinated_buyers": _f(b.get("coordinated_buyers")),
            "independent_buyers": _f(b.get("independent_buyers")),
            "funding_cluster_size": _f(rel.get("largest_dependent_cluster")),
            "funding_fanout": _f(max((v or 0 for v in (top or {}).get("funding_fanout", {}).values()), default=0)) if top else 0.0,
            "funding_time_concentration": _f((top or {}).get("funding_window_seconds")),
            "common_funder_count": _f(len(dep)), "funding_known_share": _f(cov.get("funding_known_share")),
            "organic_demand_ratio": _f(d.get("organic_demand_ratio")),
            "organic_ratio_lower": _f(d.get("organic_demand_ratio_lower")),
            "organic_ratio_upper": _f(d.get("organic_demand_ratio_upper")),
            "creator_volume_ratio": _f(c.get("creator_related_volume_ratio")),
            "cluster_volume_ratio": (cluster_vol / total) if total else None,
            "independent_volume_ratio": _f(d.get("organic_demand_ratio_lower")),
            # compact storage drops zero flows: absent inside a measured block is 0
            "net_sol_flow_30s": _f(f30.get("net_sol_flow", 0)), "net_sol_flow_60s": _f(f60.get("net_sol_flow", 0)),
            "organic_net_sol_flow_30s": _f(f30.get("organic_net_sol_flow", 0)),
            "raw_volume_acceleration": _f(acc.get("raw_volume_acceleration")),
            "organic_volume_acceleration": _f(acc.get("organic_volume_acceleration")),
            "effective_buyer_acceleration": _f(acc.get("effective_buyer_acceleration")),
        })
        sm = rel.get("smart_money") or {}
        if sm.get("status") == "MEASURED":
            out.update({"smart_money_quality": _f(sm.get("smart_money_quality")),
                        "smart_money_independence": _f(sm.get("smart_money_independence")),
                        "smart_money_signal_strength": _f(sm.get("smart_money_signal_strength"))})
    dep = intel.get("deployer") or {}
    if dep.get("status") in ("MEASURED", "INSUFFICIENT_HISTORY", "NO_HISTORY") and _asof_ok(dep, "deployer_history_cutoff", decided_at):
        out.update({"deployer_launch_count": _f(dep.get("deployer_launch_count")),
                    "deployer_resolved_launches": _f(dep.get("resolved_launches")),
                    "deployer_launches_24h": _f(dep.get("launches_last_24h"))})
        if dep.get("resolved_launches"):
            out.update({"deployer_bond_rate": _f(dep.get("deployer_bond_rate_shrunk")),
                        "deployer_risk_score": _f(dep.get("deployer_risk_score")),
                        "deployer_recent_success_rate": _f(dep.get("deployer_recent_success_rate")),
                        "deployer_creator_sell_rate": _f(dep.get("deployer_creator_sell_rate")),
                        "deployer_median_peak_mc": _f((dep.get("deployer_peak_mc_sol") or {}).get("median"))})
    mp = intel.get("manufactured_pump") or {}
    if mp.get("risk") not in (None, "UNKNOWN") and _asof_ok(mp, "as_of", decided_at):
        m = mp.get("metrics") or {}
        out.update({"manufactured_pump_score": _f(mp.get("score")),
                    "manufactured_pattern_duration": _f(mp.get("pattern_duration_seconds")),
                    "log_price_r2": _f(m.get("log_price_r2")), "buy_sell_ratio_stability": _f(m.get("buy_sell_ratio_variation")),
                    "positive_return_share": _f(m.get("positive_return_share"))})
    cs = (intel.get("observation") or {}).get("coverage_status")
    out["observation_complete"] = None if cs is None else (1.0 if cs == "COMPLETE" else 0.0)
    return out
