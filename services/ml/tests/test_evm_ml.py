"""EVM shadow models (M12a): time-split training, registration as shadow,
scoring with in-sample samples kept out of the comparison."""

import os
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.evm_ml import IN_SAMPLE, run_evm_cycle
from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import EvmMlSample, ModelVersion
from yonixalpha_core.ml import evm_samples as es

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _row(i: int, rng: random.Random) -> EvmMlSample:
    buyers = rng.uniform(0, 40)
    snap = {"minutes": 5, "at": "2026-09-01T00:05:00+00:00", "taken_at": "2026-09-01T00:05:10+00:00",
            "trades": buyers * 2, "buyers": buyers, "sellers": rng.uniform(0, 10), "buy_volume": buyers / 10,
            "sell_volume": rng.uniform(0, 1), "price": "0.000001"}
    x = es.features(snap, None, rng.choice(["bsc", "robinhood"]), "FRESH", "fourmeme")
    up = buyers > 20 or rng.random() < 0.1
    dump = buyers < 8 and rng.random() < 0.6
    lab = {"upside_50": up, "upside_100": up and rng.random() < 0.5, "fast_dump": dump, "migrate_60m": up and rng.random() < 0.3,
           "return_60m_pct": (60.0 if up else -30.0) + rng.uniform(-10, 10), "max_drawdown_pct": -rng.uniform(0, 80)}
    return EvmMlSample(chain="bsc", token=f"0x{i:040x}", category="FRESH", launchpad="fourmeme",
                       decided_at=T + timedelta(minutes=10 * i), features=x, labels=lab, feature_version=es.FEATURE_VERSION,
                       label_version=es.LABEL_VERSION, verdicts=es.verdicts("EXPIRED", [], None), observation_state="EXPIRED",
                       traded=False, created_at=T)


@pytest.mark.asyncio
async def test_evm_cycle_trains_shadow_models_and_marks_in_sample_scores(db_session):
    rng = random.Random(7)
    db_session.add_all([_row(i, rng) for i in range(300)])
    await db_session.commit()
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    out = await run_evm_cycle(sf, now=T + timedelta(days=30))
    assert out["evm_models"]["samples"] == 300 and out["evm_models"]["P_UPSIDE_50"].startswith("registered")
    assert out["wallet_models"]["status"].startswith("skipped: 0 labelled samples")
    names = {m.name: m for m in (await db_session.execute(select(ModelVersion))).scalars()}
    m = names["shadow_evm_p_upside_50"]
    assert m.status == "shadow" and m.metrics["role"].startswith("SHADOW") and m.metrics["holdout"]["roc_auc"] > 0.7
    assert "category=FRESH" in m.metrics["holdout"]["segments"]
    assert out["scored"] == 300
    db_session.expire_all()
    rows = (await db_session.execute(select(EvmMlSample).order_by(EvmMlSample.decided_at))).scalars().all()
    assert rows[0].verdicts["ml"] == IN_SAMPLE and rows[0].ml_shadow["out_of_sample"] is False
    assert rows[-1].ml_shadow["out_of_sample"] is True and rows[-1].verdicts["ml"] in ("BUY", "WAIT", "REJECT")
    assert "P_FAST_DUMP" in rows[-1].ml_shadow["scores"]
    again = await run_evm_cycle(sf, now=T + timedelta(days=30))  # nothing new: no retraining, no history overwritten
    assert again["evm_models"]["P_UPSIDE_50"] == "skipped_no_new_samples" and again["scored"] == 0


@pytest.mark.asyncio
async def test_training_reads_only_the_newest_rows_and_retrains_at_most_daily(db_session, monkeypatch):
    import app.evm_ml as m

    rng = random.Random(11)
    db_session.add_all([_row(i, rng) for i in range(300)])
    unknown = _row(999, rng)
    unknown.labels, unknown.features = {"unknown": "no T+5 snapshot was taken"}, {}
    db_session.add(unknown)
    await db_session.commit()
    monkeypatch.setattr(m, "MAX_TRAIN_ROWS", 250)
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    out = await run_evm_cycle(sf, now=T + timedelta(days=30))
    assert out["evm_models"]["samples"] == 250 and out["scored"] == 300  # the unknown row is never scored
    mv = (await db_session.execute(select(ModelVersion).where(ModelVersion.name == "shadow_evm_p_upside_50"))).scalar_one()
    assert mv.metrics["dataset_end"] == (T + timedelta(minutes=10 * 299)).isoformat()
    assert mv.metrics["split"]["holdout_start"] > (T + timedelta(minutes=10 * 50)).isoformat()  # oldest 50 left out
    db_session.add_all([_row(i, rng) for i in range(300, 320)])
    await db_session.commit()
    again = await run_evm_cycle(sf, now=datetime.now(timezone.utc))
    assert again["evm_models"]["P_UPSIDE_50"] == "skipped_retrained_within_24h"


@pytest.mark.asyncio
async def test_drain_runs_batches_until_short_or_out_of_time():
    import time as _time

    from app.evm_ml import _drain

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    sizes = iter([5, 5, 2])

    async def build(session, now, batch):
        return {"built": next(sizes), "skipped_other_quote": 1}
    out = await _drain(lambda: _S(), build, T, 5, "built", _time.monotonic() + 60)
    assert out == {"batches": 3, "built": 12, "skipped_other_quote": 3, "drained": True}

    async def full(session, now, batch):
        return {"built": batch}
    out = await _drain(lambda: _S(), full, T, 5, "built", _time.monotonic() - 1)
    assert out == {"batches": 1, "built": 5, "drained": False}
