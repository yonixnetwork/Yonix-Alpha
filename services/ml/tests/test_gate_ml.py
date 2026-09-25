import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import gate_ml
from yonixalpha_core.db.models import AuditLog, DataQualityEvent, MLFeatureSnapshot, ModelVersion, Notification, SystemEvent
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.gate_features import FEATURE_VERSION

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def features(strength: float, at: datetime, **over) -> dict:
    f = {"volatility": 0.01, "liquidity_quote": 5e6, "spread_bps": 1.0, "signal_strength": strength, "side": "LONG",
         "decision_at": at.isoformat()}
    f.update(over)
    return f


def sample(i: int, label: int, strength: float, **over) -> MLFeatureSnapshot:
    at = BASE + timedelta(minutes=i)
    outcome = over.pop("outcome", {"profitable": bool(label), "exit_reason": "take_profit_1" if label else "stop_loss",
                                   "return_pct": "0.02" if label else "-0.01"})
    return MLFeatureSnapshot(symbol="ETHUSDT", engine="binance_futures", features=features(strength, at, **over),
                             feature_version=FEATURE_VERSION, label=label, outcome=outcome, created_at=at)


async def seed_learnable(session, n: int, start: int = 0, seed: int = 1) -> None:
    rng = random.Random(seed)
    for i in range(start, start + n):
        label = i % 2
        session.add(sample(i, label, (0.7 if label else 0.3) + rng.uniform(-0.15, 0.15)))
    await session.commit()


async def test_quality_check_quarantines_bad_samples_with_reasons(db_session):
    db_session.add(sample(0, 1, 0.8))
    db_session.add(sample(1, 1, 0.8, outcome={"profitable": False, "exit_reason": "stop_loss"}))  # label disagrees
    db_session.add(sample(2, 0, 0.2, outcome={}))  # never closed
    db_session.add(sample(3, 1, 0.8, spread_bps=None))  # missing feature, not defaulted
    db_session.add(sample(4, 0, 0.2, volatility=-1))  # impossible
    leaked = sample(5, 1, 0.8)
    leaked.features = {**leaked.features, "decision_at": (BASE + timedelta(hours=3)).isoformat()}
    db_session.add(leaked)
    await db_session.commit()

    counts = await gate_ml.run_quality_check(db_session, "gate_futures")
    await db_session.commit()

    assert counts == {"ok": 1, "quarantined": 5}
    issues = {e.issue for e in (await db_session.execute(select(DataQualityEvent))).scalars()}
    assert {"label_outcome_mismatch", "incomplete_trade", "missing_fields", "impossible_value:volatility",
            "future_leakage"} <= issues
    statuses = [r.quality_status for r in (await db_session.execute(select(MLFeatureSnapshot))).scalars()]
    assert None not in statuses


async def test_insufficient_samples_trains_nothing(db_session):
    await seed_learnable(db_session, gate_ml.MIN_SAMPLES - 1)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    r = await gate_ml.train_challenger(db_session, "gate_futures")
    assert r["status"] == "skipped_insufficient_samples"
    assert (await db_session.execute(select(ModelVersion))).first() is None


async def test_challenger_is_registered_but_never_promoted_automatically(db_session):
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    r = await gate_ml.train_challenger(db_session, "gate_futures")
    await db_session.commit()
    assert r["status"] == "challenger_registered" and r["promotable"] is True

    mv = (await db_session.execute(select(ModelVersion))).scalar_one()
    assert mv.status == "challenger"
    assert await registry.get_active_model_row(db_session, "gate_futures") is None
    m = mv.metrics
    for key in ("auc", "auc_lower_bound", "brier", "precision", "recall", "false_positive_rate", "false_negative_rate",
                "stability_auc_gap", "paper_return_favoured_avg"):
        assert key in m["challenger"]
    assert m["split"] == "temporal" and "reference" in m

    # Same data again: nothing new to learn from, no new version.
    assert (await gate_ml.train_challenger(db_session, "gate_futures"))["status"] == "skipped_no_new_samples"

    await registry.promote_challenger(db_session, mv, None, "looked fine")
    await db_session.commit()
    assert (await registry.get_active_model_row(db_session, "gate_futures")).id == mv.id
    audit = (await db_session.execute(select(AuditLog))).scalar_one()
    assert audit.event_type == "ml.promoted" and audit.detail["version"] == mv.version


async def test_new_challenger_is_compared_against_champion_on_same_holdout(db_session):
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    await gate_ml.train_challenger(db_session, "gate_futures")
    first = (await db_session.execute(select(ModelVersion))).scalar_one()
    await registry.promote_challenger(db_session, first)
    await db_session.commit()

    await seed_learnable(db_session, 40, start=120, seed=2)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    r = await gate_ml.train_challenger(db_session, "gate_futures")
    await db_session.commit()
    second = (await db_session.execute(select(ModelVersion).where(ModelVersion.version == r["version"]))).scalar_one()
    assert second.status == "challenger"
    assert second.metrics["champion"]["version"] == first.version
    assert second.metrics["champion"]["holdout_size"] == second.metrics["challenger"]["holdout_size"]


async def test_unpromotable_challenger_cannot_be_promoted(db_session):
    # Labels independent of every feature: nothing to learn.
    rng = random.Random(7)
    for i in range(120):
        db_session.add(sample(i, rng.randint(0, 1), rng.uniform(0, 1)))
    await db_session.commit()
    await gate_ml.run_quality_check(db_session, "gate_futures")
    r = await gate_ml.train_challenger(db_session, "gate_futures")
    await db_session.commit()
    if r["status"] != "challenger_registered":
        pytest.skip(f"training skipped: {r}")
    mv = (await db_session.execute(select(ModelVersion))).scalar_one()
    if mv.metrics["promotable"]:
        pytest.skip("random data happened to clear the bar")
    with pytest.raises(ValueError):
        await registry.promote_challenger(db_session, mv)


async def test_drift_detected_flags_model_and_notifies(db_session, monkeypatch):
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    await gate_ml.train_challenger(db_session, "gate_futures")
    mv = (await db_session.execute(select(ModelVersion))).scalar_one()
    await registry.promote_challenger(db_session, mv)
    await db_session.commit()

    now = BASE + timedelta(days=30)
    # Recent market: volatility an order of magnitude above anything trained on.
    for i in range(30):
        at = now - timedelta(hours=1, minutes=i)
        db_session.add(MLFeatureSnapshot(symbol="ETHUSDT", engine="binance_futures", feature_version=FEATURE_VERSION,
                                         features=features(0.5, at, volatility=0.2 + i * 0.01), created_at=at))
    await db_session.commit()

    class FakeRedis:
        def __init__(self):
            self.kv = {}

        async def set(self, k, v, nx=False, ex=None):
            if nx and k in self.kv:
                return False
            self.kv[k] = v
            return True

        async def delete(self, k):
            self.kv.pop(k, None)

        async def publish(self, *a):
            return 0

        async def hincrby(self, *a):
            return 1

    redis = FakeRedis()
    report = await gate_ml.check_drift(db_session, redis, None, "gate_futures", now)
    await db_session.commit()
    assert report["status"] == "MODEL_DRIFT_DETECTED"
    assert report["feature_psi"]["volatility"] > gate_ml.PSI_DRIFT
    assert redis.kv.get(gate_ml.drift_flag_key("gate_futures")) == "1"
    assert (await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "MODEL_DRIFT_DETECTED"))).first()
    assert (await db_session.execute(select(Notification).where(Notification.kind == "ml_drift"))).first()

    # Second check the same day: still flagged, but no duplicate notification.
    await gate_ml.check_drift(db_session, redis, None, "gate_futures", now)
    await db_session.commit()
    assert len((await db_session.execute(select(Notification))).all()) == 1


async def test_no_drift_on_same_distribution_clears_flag(db_session):
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    await gate_ml.train_challenger(db_session, "gate_futures")
    mv = (await db_session.execute(select(ModelVersion))).scalar_one()
    await registry.promote_challenger(db_session, mv)
    await db_session.commit()
    now = BASE + timedelta(days=1)
    rng = random.Random(3)
    for i in range(60):
        at = now - timedelta(minutes=i + 1)
        label = i % 2
        db_session.add(MLFeatureSnapshot(symbol="ETHUSDT", engine="binance_futures", feature_version=FEATURE_VERSION,
                                         features=features((0.7 if label else 0.3) + rng.uniform(-0.15, 0.15), at),
                                         created_at=at))
    await db_session.commit()
    report = await gate_ml.check_drift(db_session, None, None, "gate_futures", now)
    assert report["status"] == "ok", report


async def test_retire_champion_is_audited(db_session):
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    await gate_ml.train_challenger(db_session, "gate_futures")
    mv = (await db_session.execute(select(ModelVersion))).scalar_one()
    await registry.promote_challenger(db_session, mv)
    await registry.retire_champion(db_session, "gate_futures", None, "drift")
    await db_session.commit()
    assert await registry.get_active_model_row(db_session, "gate_futures") is None
    types = [a.event_type for a in (await db_session.execute(select(AuditLog))).scalars()]
    assert types == ["ml.promoted", "ml.champion_retired"]
