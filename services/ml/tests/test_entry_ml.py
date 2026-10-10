"""Entry-timing model (2026-10-10): no model without enough labelled
signals, the frozen test period is set once and never moves, and a trained
model is registered as SHADOW next to (never over) earlier versions."""

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from yonixalpha_core import entry_eval, entry_intel as ei
from yonixalpha_core.db.models import EntrySignal, ModelVersion, PlatformSetting

from app import entry_ml

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _signals(n: int, start: datetime, prefix: str = "M") -> list[EntrySignal]:
    rnd = random.Random(7)
    out = []
    for i in range(n):
        inflow = rnd.uniform(-1, 1)
        win = rnd.random() < (0.75 if inflow > 0 else 0.25)  # a learnable, noisy relation
        out.append(EntrySignal(mint=f"{prefix}{i:040d}", strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH", decision=ei.CANDIDATE,
                               decided_at=start + timedelta(minutes=10 * i), score=rnd.uniform(0, 1),
                               features={"age_seconds": rnd.uniform(5, 120), "inflow_sol_per_s": inflow,
                                         "trades_total": rnd.randint(5, 60)},
                               outcome={"executable_return_pct": 12.0 if win else -9.0},
                               outcome_at=start + timedelta(minutes=10 * i + 15)))
    return out


async def test_insufficient_data_trains_nothing(db_session):
    db_session.add_all(_signals(20, T0))
    await db_session.commit()
    res = await entry_ml.run_entry_cycle(db_session)
    assert res["status"] == "INSUFFICIENT_DATA"
    assert await db_session.get(PlatformSetting, entry_eval.FREEZE_SETTING) is None
    assert (await db_session.execute(select(ModelVersion))).scalars().all() == []


async def test_registers_shadow_versions_on_a_frozen_test_period(db_session):
    db_session.add_all(_signals(420, T0))
    await db_session.commit()
    first = await entry_ml.run_entry_cycle(db_session)
    assert first["status"] == "REGISTERED_SHADOW", first
    frozen = (await db_session.get(PlatformSetting, entry_eval.FREEZE_SETTING)).value
    # New signals arrive later: the frozen period does not move, they are "forward".
    db_session.add_all(_signals(30, T0 + timedelta(days=10), "N"))
    await db_session.commit()
    second = await entry_ml.run_entry_cycle(db_session)
    assert (await db_session.get(PlatformSetting, entry_eval.FREEZE_SETTING)).value == frozen
    rows = (await db_session.execute(select(ModelVersion).where(ModelVersion.name == "entry_timing")
                                     .order_by(ModelVersion.version))).scalars().all()
    assert [r.status for r in rows] == ["shadow", "shadow"] and second["version"] == rows[-1].version
    coef = rows[-1].metrics["coefficients"]
    assert coef["features"] == list(ei.MODEL_FEATURES)
    p = ei.logistic_predict(coef, ei.model_vector({"inflow_sol_per_s": 0.8, "age_seconds": 30}, 0.5))
    q = ei.logistic_predict(coef, ei.model_vector({"inflow_sol_per_s": -0.8, "age_seconds": 30}, 0.5))
    assert p is not None and q is not None and p > q
