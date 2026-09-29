"""The ablation experiment credits a layer only when it improves out-of-sample
ranking, and the activity-matched comparison removes a difference that is
only due to busier tokens."""

import random
from datetime import datetime, timedelta, timezone

from app import ablation as ab
from app.shadow_ml import Sample

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def synth(n: int = 1600, seed: int = 3) -> list[Sample]:
    """fast_dump depends on the wallet-graph feature funding_cluster_size
    only; every other feature is noise."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        at = T0 + timedelta(minutes=i)
        cluster = rng.choice([0, 0, 0, 2, 3, 5])
        p = 0.1 + 0.12 * cluster
        dump = rng.random() < min(p, 0.9)
        x = {"market_cap_sol": rng.uniform(25, 90), "buyers": float(rng.randint(3, 60)), "volatility": rng.random(),
             "funding_cluster_size": float(cluster), "deployer_risk_score": rng.random(), "organic_demand_ratio": rng.random(),
             "manufactured_pump_score": rng.random(), "smart_money_quality": rng.random()}
        labels = {"fast_dump": dump, "rug_60m": dump and rng.random() < 0.5, "upside_50": (not dump) and rng.random() < 0.4,
                  "upside_100": (not dump) and rng.random() < 0.2, "executable_return_primary_pct": -20.0 if dump else 5.0,
                  "max_drawdown_pct": -40.0 if dump else -10.0, "available_at": (at + timedelta(hours=1)).isoformat()}
        out.append(Sample(at, at + timedelta(hours=1), x, labels,
                          {"stage": "OBSERVATION", "engine": "solana_fresh", "data_regime": "post_boost"}))
    return out


def test_only_the_informative_layer_is_credited():
    r = ab.evaluate(synth())
    assert r["status"] == "EVALUATED" and r["split"]["purged"] >= 0 and r["period"]["first"].startswith("2026-09-01")
    t = r["targets"]["P_FAST_DUMP"]
    v = t["verdicts"]
    assert v["wallet_graph (step B_plus_wallet_graph vs A_price_volume_buyers)"]["verdict"] == "ADDS_VALUE"
    assert v["wallet_graph (F_full vs without_wallet_graph)"]["verdict"] == "ADDS_VALUE"
    for layer in ("deployer (step C_plus_deployer vs B_plus_wallet_graph)", "smart_money (F_full vs without_smart_money)",
                  "manipulation (F_full vs without_manipulation_detector)"):
        assert v[layer]["verdict"] != "ADDS_VALUE", layer  # noise is never credited
    s = t["sets"]["B_plus_wallet_graph"]
    assert s["roc_auc"] > t["sets"]["A_price_volume_buyers"]["roc_auc"] + 0.1
    d = s["decision"]
    assert {"precision", "recall", "false_positives", "false_negatives", "expected_executable_return_pct",
            "mean_max_drawdown_pct", "missed_winners", "false_entries"} <= set(d)
    assert "engine=solana_fresh" in s["segments"] and r["feature_availability"]["holdout"]["wallet_graph"] == 1.0
    assert "not causal proof" in r["note"] and any("ADDS_VALUE" in line for line in ab.summary_lines(r))


def test_too_little_data_is_skipped_not_guessed():
    assert ab.evaluate(synth(50))["status"].startswith("skipped")


def test_matching_removes_a_difference_explained_by_activity():
    """Treated tokens are busier (more buyers), and busier tokens dump more;
    within the same buyer bucket the treatment makes no difference."""
    rng = random.Random(5)
    samples = []
    for i in range(3000):
        at = T0 + timedelta(minutes=i)
        busy = rng.random() < 0.5
        buyers = float(rng.randint(30, 45) if busy else rng.randint(1, 4))
        treated = rng.random() < (0.8 if busy else 0.1)
        dump = rng.random() < (0.7 if busy else 0.2)
        x = {"buyers": buyers, "funding_cluster_size": 3.0 if treated else 0.0, "market_cap_sol": 35.0, "age_seconds": 20.0,
             "liquidity_sol": 10.0, "recycled_wallets": 0.0}
        samples.append(Sample(at, at, x, {"fast_dump": dump, "rug_60m": False, "upside_50": False, "upside_100": False,
                                          "executable_return_primary_pct": -5.0},
                              {"stage": "OBSERVATION", "engine": "solana_fresh", "data_regime": "post_boost"}))
    m = ab.matched_comparison(samples, ab.TREATMENTS["coordinated_or_funding_cluster"])
    fd = m["outcomes"]["fast_dump"]
    assert fd["raw_difference"] > 0.3  # naive comparison: looks strongly predictive
    assert abs(fd["matched_difference"]) < 0.08 and fd["ci95"][0] < 0 < fd["ci95"][1]  # matched: no evidence
    assert m["status"] == "COMPARED" and "not a causal effect" in m["note"]
    missing = ab.matched_comparison([Sample(T0, T0, {}, {}, samples[0].segment)], ab.TREATMENTS["low_organic_demand"])
    assert missing["rows_with_evidence"] == 0 and missing["status"] == "NO_MATCHES"
