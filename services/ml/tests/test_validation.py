"""Frozen validation sets (master §38): windows frozen out of every trainer,
models scored only on the windows they never saw, one pooled report per
(newest window, model version)."""

import os
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app import gate_ml
from app.dataset import FEATURE_NAMES, load_labeled_dataset
from app.evm_ml import run_evm_cycle
from app.validation import MIN_N, never_saw, run_validation, scores, verdict
from tests.test_evm_ml import T, _row
from tests.test_gate_ml import BASE, seed_learnable
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import MLFeatureSnapshot, MlValidationReport, MlValidationSet, ModelVersion
from yonixalpha_core.ml import frozen

DAY = timedelta(days=1)


def _sf():
    return make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))


def test_verdict_needs_enough_unseen_samples_a_real_auc_and_calibration():
    rng = random.Random(1)
    good = [rng.random() for _ in range(400)]  # calibrated: each outcome drawn with its own probability
    y = [1 if rng.random() < p else 0 for p in good]
    status, reason = verdict(scores(y, good))
    assert status == "PASS" and "never saw" in reason
    assert verdict(scores(y[:MIN_N - 2], good[:MIN_N - 2]))[0] == "INSUFFICIENT_DATA"
    assert verdict(scores([0] * 150 + [1] * 5, [0.1] * 155))[0] == "INSUFFICIENT_DATA"  # 5 positives
    noise = [rng.random() for _ in y]
    status, reason = verdict(scores(y, noise))
    assert status == "FAIL" and "AUC" in reason
    y = [i % 2 for i in range(200)]
    m = scores(y, [0.95 if t else 0.6 for t in y])  # perfect ranking, miscalibrated
    status, reason = verdict(m)
    assert status == "FAIL" and "calibration" in reason and m["auc"] == 1.0
    assert m["false_positives"] == 100 and m["precision"] == 0.5 and m["recall"] == 1.0


def test_a_model_is_judged_only_on_windows_it_never_saw():
    s = MlValidationSet(id=7, family="evm_entry", window_start=T + DAY, window_end=T + 2 * DAY, frozen_at=T, samples=1)
    excluded = ModelVersion(name="x", version=1, metrics={"frozen_excluded": [7], "dataset_end": (T + 9 * DAY).isoformat()})
    before = ModelVersion(name="x", version=2, metrics={"dataset_end": (T + DAY / 2).isoformat()})
    trained_on_it = ModelVersion(name="x", version=3, metrics={"frozen_excluded": [], "dataset_end": (T + 9 * DAY).isoformat()})
    legacy = ModelVersion(name="x", version=4, metrics={}, trained_at=T)
    assert never_saw(excluded, s) and never_saw(before, s) and never_saw(legacy, s)
    assert not never_saw(trained_on_it, s)


@pytest.mark.asyncio
async def test_frozen_windows_are_never_trained_on_and_scored_only_by_models_that_never_saw_them(db_session):
    rng = random.Random(7)
    db_session.add_all([_row(i, rng) for i in range(1000)])  # one every 10 minutes from T: ~7 days
    await db_session.commit()
    sf = _sf()

    first = await run_validation(sf, now=T + 4 * DAY)  # freezes T+1d (the day LAG back) in every family
    assert first["evaluated"] == 0
    evm_set = next(f for f in first["frozen"] if f["family"] == "evm_entry")
    assert evm_set == {"family": "evm_entry", "window": str((T + DAY).date()), "samples": 144}
    assert (await run_validation(sf, now=T + 5 * DAY))["frozen"] == []  # weekly, not hourly

    out = await run_evm_cycle(sf, now=T + 30 * DAY)
    assert out["evm_models"]["samples"] == 1000 - 144  # the frozen day is left out
    m = (await db_session.execute(select(ModelVersion).where(ModelVersion.name == "shadow_evm_p_upside_50"))).scalar_one()
    sid = (await db_session.execute(select(MlValidationSet.id).where(MlValidationSet.family == "evm_entry"))).scalar_one()
    assert m.metrics["frozen_excluded"] == [sid]

    val = await run_validation(sf, now=T + 30 * DAY)
    assert val["evaluated"] >= 2
    reps = {r.model_name: r for r in (await db_session.execute(select(MlValidationReport))).scalars()}
    up = reps["shadow_evm_p_upside_50"]
    newest = (await db_session.execute(select(MlValidationSet).where(MlValidationSet.family == "evm_entry")
                                       .order_by(MlValidationSet.window_start.desc()))).scalars().first()
    assert up.set_id == newest.id and newest.samples == 0  # T+27d frozen, empty: pooled with T+1d
    windows = {w["window"]: w for w in up.metrics["sets"]}
    assert windows[str((T + DAY).date())]["n"] == 144 and windows[str(newest.window_start.date())]["n"] == 0
    assert up.metrics["n"] == 144 and up.metrics["auc"] > 0.7 and up.status in ("PASS", "FAIL")
    assert up.metrics["by_category"]["FRESH"]["n"] == 144
    d = up.metrics["decisions"]
    assert d["scored"] == 144 and 0 <= d["ml_buy"]["n"] <= 144 and "rules_final_buy" in d
    assert d["ml_buy"]["executable"]["n"] == 0 and "NOT AVAILABLE" in d["ml_buy"]["executable"]["note"]
    again = await run_validation(sf, now=T + 30 * DAY + timedelta(hours=1))
    assert again["evaluated"] == 0 and again["frozen"] == []  # one report per (newest window, version)


@pytest.mark.asyncio
async def test_candidate_and_gate_training_leave_frozen_windows_out(db_session):
    at = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    for i, label in enumerate((1, 0, 1)):
        db_session.add(MLFeatureSnapshot(symbol="T", features={n: 1.0 for n in FEATURE_NAMES}, label=label,
                                         created_at=at + timedelta(days=i)))
    db_session.add(MlValidationSet(family="solana_candidate", window_start=datetime(2026, 9, 4, tzinfo=timezone.utc),
                                   window_end=datetime(2026, 9, 5, tzinfo=timezone.utc), frozen_at=at, samples=1))
    await db_session.commit()
    ds = await load_labeled_dataset(db_session)
    assert ds.labels == [1, 1] and len(ds.frozen_excluded) == 1 and ds.dataset_end == at + 2 * DAY

    # gate models: a frozen window covering every sample leaves nothing to train on
    await db_session.execute(MLFeatureSnapshot.__table__.delete())
    await seed_learnable(db_session, 120)
    await gate_ml.run_quality_check(db_session, "gate_futures")
    db_session.add(MlValidationSet(family="solana_candidate", window_start=BASE, window_end=BASE + DAY, frozen_at=BASE,
                                   samples=120))
    await db_session.commit()
    r = await gate_ml.train_challenger(db_session, "gate_futures")
    assert r["status"] == "skipped_insufficient_samples" and r["samples"] == 0


@pytest.mark.asyncio
async def test_freeze_due_records_empty_windows_honestly(db_session):
    out = await frozen.freeze_due(db_session, T + 10 * DAY)
    await db_session.commit()
    assert {f["family"] for f in out} == set(frozen.FAMILIES) and all(f["samples"] == 0 for f in out)
    note = (await db_session.execute(select(MlValidationSet.note))).scalars().first()
    assert note == "no labelled samples in this window"
    assert frozen.family_of("shadow_evm_p_upside_50") == "evm_entry"
    assert frozen.family_of("shadow_p_upside_50") == "solana_opportunity"
    assert frozen.family_of("gate_solana_fresh") == "solana_candidate" and frozen.family_of("gate_futures") is None


@pytest.mark.asyncio
async def test_an_observation_only_model_is_not_scored(db_session):
    from yonixalpha_core.db.models import EvmMlSample, PlatformSetting
    from yonixalpha_core.ml import governance

    rng = random.Random(5)
    db_session.add_all([_row(i, rng) for i in range(300)])
    db_session.add(PlatformSetting(key=governance.KEY, value={"models": {"shadow_evm_p_fast_dump": {
        "stage": "OBSERVATION_ONLY", "percent": 0}}}))
    await db_session.commit()
    out = await run_evm_cycle(_sf(), now=T + 30 * DAY)
    assert out["scored"] == 300
    row = (await db_session.execute(select(EvmMlSample).limit(1))).scalar_one()
    assert "P_UPSIDE_50" in row.ml_shadow["scores"] and "P_FAST_DUMP" not in row.ml_shadow["scores"]
