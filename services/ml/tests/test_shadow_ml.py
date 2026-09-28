"""Multi-target shadow models: time split with purge, metrics, look-ahead
guard, scoring onto the ledger, and isolation from the decision path."""

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core.db.models import ModelVersion, OpportunityOutcome
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.opportunity_features import features

from app import shadow_ml

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _row(i: int, rng: random.Random) -> OpportunityOutcome:
    decided = T0 + timedelta(minutes=10 * i)
    breadth = rng.random()
    up = rng.random() < 0.15 + 0.6 * breadth  # upside depends on buyer breadth at the decision
    snap = {"buyers": int(5 + 40 * breadth), "market_cap_sol": str(30 + rng.random() * 20), "overall_risk": "MODERATE",
            "intel": {"as_of": decided.isoformat(), "stage": "FRESH", "buyer_breadth": {"score": breadth},
                      "flow_state": {"state": "EXPANSION" if breadth > 0.5 else "QUIET"},
                      "manipulation": {"level": "NONE", "count": 0}, "regime": {"mayhem": False}}}
    labels = {"available_at": (decided + timedelta(minutes=60)).isoformat(), "upside_50": up, "upside_100": up and rng.random() < 0.5,
              "upside_200": False, "migrate_60m": rng.random() < 0.1, "fast_dump": not up and rng.random() < 0.3,
              "recovery": rng.random() < 0.2, "rug_60m": rng.random() < 0.05,
              "executable_return_primary_pct": (20 if up else -15) + rng.gauss(0, 5), "max_drawdown_pct": -rng.random() * 60}
    return OpportunityOutcome(key=f"k{i}", mint=f"M{i}", engine="solana_fresh", stage="GATE" if i % 3 else "OBSERVATION",
                              decision="REJECT", traded=False, reasons=[], decided_at=decided, snapshot=snap, horizons={},
                              status="COMPLETE", labels=labels, regime={"data_regime": "post_boost"})


async def _ledger(db, n=300):
    rng = random.Random(7)
    db.add_all([_row(i, rng) for i in range(n)])
    await db.commit()


def test_look_ahead_intel_is_dropped_and_missing_is_not_zero():
    t = T0
    ok = features({"intel": {"as_of": t.isoformat(), "buyer_breadth": {"score": 0.7}}}, t, "solana_fresh", "GATE")
    late = features({"intel": {"as_of": (t + timedelta(seconds=1)).isoformat(), "buyer_breadth": {"score": 0.7}}}, t,
                    "solana_fresh", "GATE")
    assert ok["breadth_score"] == 0.7 and ok["breadth_score__missing"] == 0.0
    assert late["breadth_score"] is None and late["breadth_score__missing"] == 1.0
    assert ok["liquidity_sol"] is None and ok["liquidity_sol__missing"] == 1.0


def test_time_split_purges_rows_whose_labels_were_not_known_yet():
    rng = random.Random(1)
    samples = [shadow_ml.sample(_row(i, rng)) for i in range(100)]
    train, hold, info = shadow_ml.time_split(samples)
    assert max(s.decided_at for s in train) < min(s.decided_at for s in hold)
    assert all(s.available_at <= hold[0].decided_at for s in train) and info["purged"] == 5  # labels known 60 min after: the 5 rows before the cut


async def test_shadow_models_train_score_and_stay_out_of_the_decision_path(db_session):
    await _ledger(db_session)
    out = await shadow_ml.train_all(db_session)
    await db_session.commit()
    assert out["P_UPSIDE_50"]["status"] == "registered"
    h = out["P_UPSIDE_50"]["holdout"]
    assert h["roc_auc"] > 0.6 and "pr_auc" in h and h["calibration"] and "precision_at_10" in h
    assert "stage=GATE" in h["segments"] and "data_regime=post_boost" in h["segments"]
    assert out["E_EXECUTABLE_RETURN"]["holdout"]["beats_baseline"] is True
    assert "P_MANIPULATION" in out["not_trained"] and out["P_UPSIDE_200"]["status"] == "skipped"
    models = (await db_session.execute(select(ModelVersion))).scalars().all()
    assert models and all(m.status == "shadow" and m.name.startswith("shadow_") for m in models)
    assert await registry.get_active_model_row(db_session, "shadow_p_upside_50") is None  # never active
    again = await shadow_ml.train_all(db_session)
    assert again["P_UPSIDE_50"]["status"] == "skipped_no_new_samples"
    # Scoring writes review-only probabilities onto recent rows.
    now = T0 + timedelta(minutes=10 * 299 + 5)
    scored = await shadow_ml.score_recent(db_session, now=now)
    await db_session.commit()
    assert scored > 0
    row = (await db_session.execute(select(OpportunityOutcome).where(OpportunityOutcome.key == "k299"))).scalar_one()
    s = row.ml_shadow
    assert 0 <= s["scores"]["P_UPSIDE_50"]["value"] <= 1 and "never used for decisions" in s["note"]


async def test_too_few_rows_is_reported_not_trained(db_session):
    await _ledger(db_session, n=50)
    out = await shadow_ml.train_all(db_session)
    assert out["status"].startswith("skipped") and out["samples"] == 50


async def test_cycle_summary_says_why_a_target_was_skipped(db_session):
    rng = random.Random(3)
    rows = [_row(i, rng) for i in range(220)]
    for r in rows:  # only ~40 minutes of history: labels of most training rows are not known yet
        r.decided_at = T0 + timedelta(seconds=11 * rows.index(r))
        r.labels = {**r.labels, "available_at": (r.decided_at + timedelta(minutes=60)).isoformat()}
    db_session.add_all(rows)
    await db_session.commit()
    from sqlalchemy.ext.asyncio import async_sessionmaker
    out = await shadow_ml.run_shadow_cycle(async_sessionmaker(db_session.bind, expire_on_commit=False))
    t = out["training"]
    assert t["P_UPSIDE_50"].startswith("skipped: ") and "positives in training" in t["P_UPSIDE_50"]
    assert t["split"]["purged"] > 0 and t["samples"] == 220
