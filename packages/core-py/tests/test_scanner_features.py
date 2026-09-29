"""Scanner-intelligence ML features: read only from the decision-time
record, dropped when stamped after the decision, missing kept missing, and
no wallet identity ever becomes a feature."""
from datetime import datetime, timedelta, timezone

from yonixalpha_core.ml import opportunity_features as of

T = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def snap(as_of=T, dep_cutoff=T):
    rel = {"status": "MEASURED", "as_of": as_of.isoformat(),
           "buyers": {"raw_unique_buyers": 20, "effective_unique_buyers": 12, "creator_related_buyers": 2,
                      "coordinated_buyers": 7, "independent_buyers": 4},
           "demand": {"organic_demand_ratio": None, "organic_demand_ratio_lower": 0.2, "organic_demand_ratio_upper": 0.7,
                      "total_volume_sol": 10.0, "funding_cluster_volume_sol": 2.0, "coordinated_volume_sol": 1.0},
           "creator": {"creator_related_volume_ratio": 0.1}, "coverage": {"funding_known_share": 0.4},
           "clusters": [{"classification": "COORDINATED", "size": 7, "funding_fanout": {"F": 30}, "funding_window_seconds": 50,
                         "cluster_id": "fund-abc", "members": ["W1", "W2"]}],
           "largest_dependent_cluster": 7, "flows": {"30s": {"net_sol_flow": 1.5}},
           "acceleration": {"raw_volume_acceleration": 2.0, "organic_volume_acceleration": None},
           "smart_money": {"status": "MEASURED", "smart_money_quality": 0.3, "smart_money_independence": 0.5,
                           "smart_money_signal_strength": 1}}
    dep = {"status": "MEASURED", "deployer_history_cutoff": dep_cutoff.isoformat(), "deployer_launch_count": 9,
           "resolved_launches": 6, "deployer_bond_rate_shrunk": 0.1, "deployer_risk_score": 0.7,
           "deployer_peak_mc_sol": {"median": 45.0}, "launches_last_24h": 3}
    mp = {"risk": "HIGH", "as_of": T.isoformat(), "score": 1.0, "pattern_duration_seconds": 120,
          "metrics": {"log_price_r2": 0.97, "buy_sell_ratio_variation": 0.05, "positive_return_share": 0.9}}
    return {"intel": {"as_of": T.isoformat(), "relationships": rel, "deployer": dep, "manufactured_pump": mp,
                      "observation": {"coverage_status": "PARTIAL"}}}


def test_scanner_features_are_extracted_with_missing_kept_missing():
    x = of.features(snap(), T, "solana_fresh", "OBSERVATION")
    assert x["effective_unique_buyers"] == 12 and x["funding_cluster_size"] == 7 and x["funding_fanout"] == 30
    assert x["cluster_volume_ratio"] == 0.3 and x["net_sol_flow_30s"] == 1.5 and x["net_sol_flow_60s"] == 0.0
    assert x["organic_demand_ratio"] is None and x["organic_demand_ratio__missing"] == 1.0  # PARTIAL: not assumed
    assert x["organic_volume_acceleration__missing"] == 1.0 and x["organic_ratio_upper"] == 0.7
    assert x["deployer_risk_score"] == 0.7 and x["deployer_median_peak_mc"] == 45.0 and x["manufactured_pump_score"] == 1.0
    assert x["observation_complete"] == 0.0 and x["smart_money_signal_strength"] == 1


def test_blocks_stamped_after_the_decision_are_dropped():
    later = T + timedelta(seconds=1)
    x = of.features(snap(as_of=later, dep_cutoff=later), T, "solana_fresh", "OBSERVATION")
    assert x["effective_unique_buyers"] is None and x["deployer_risk_score"] is None
    assert x["manufactured_pump_score"] == 1.0  # its own stamp is at the decision


def test_no_wallet_identity_is_a_feature_and_every_group_is_known():
    assert not [n for n in of.FEATURE_NAMES if "cluster_id" in n or "wallet_address" in n]
    assert set(of.SOURCES) <= set(of.GROUPS) and all(n in of.FEATURE_NAMES for g in of.GROUPS.values() for n in g)
    assert of.FEATURE_VERSION == "oppfeat-2026.09.2"
