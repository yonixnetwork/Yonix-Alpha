"""The ML champion on the Solana gate: it is scored and explained, it can
only add caution (ML_BELOW_MIN -> WAIT), and a drifted model is ignored.
(Previously exercised through the removed futures runner.)"""

import io

import joblib
from sklearn.linear_model import LogisticRegression
from sqlalchemy import select

from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion, PaperPosition
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, SOLANA_FEATURES
from yonixalpha_core.safety import store
from yonixalpha_core.safety.settings import default_settings_for, settings_to_dict
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.testing.pump import FakeRpc, seed_healthy_launch

from app.gate_eval import evaluate_with_gate

from .test_gate_eval import ENV, NOW, make_candidate


async def add_champion(session, positive: bool) -> ModelVersion:
    """A champion whose score is fixed by its intercept: ~0.99 when
    positive, ~0.01 when not, whatever the features."""
    import numpy as np

    n = len(SOLANA_FEATURES)
    est = LogisticRegression().fit([[0] * n, [1] * n], [0, 1])
    est.coef_ = np.zeros((1, n))
    est.intercept_ = np.array([5.0 if positive else -5.0])
    buf = io.BytesIO()
    joblib.dump(est, buf)
    mv = ModelVersion(name="gate_solana_fresh", version=1, status="active", feature_names=SOLANA_FEATURES,
                      training_sample_count=11, metrics={}, artifact=buf.getvalue())
    session.add(mv)
    await session.commit()
    return mv


async def require_ml_confidence(session, value: float) -> None:
    data = settings_to_dict(default_settings_for("solana_fresh"))
    data["min_ml_confidence"] = value
    await store.save_settings(session, "solana_fresh", data, None)
    await session.commit()


async def evaluate(db_session, redis_client):
    curve = await seed_healthy_launch(redis_client, NOW)
    cand = await make_candidate(db_session)
    return await evaluate_with_gate(db_session, redis_client, ENV, Sources(redis_client, FakeRpc(curve)), cand, NOW)


async def test_champion_scores_and_explains_but_cannot_bypass_gate(db_session, redis_client):
    mv = await add_champion(db_session, positive=True)
    a = await evaluate(db_session, redis_client)
    assert a.decision.value == "EXECUTE", a.reasons
    ml = a.inputs_snapshot["ml"]
    assert ml["status"] == "scored" and ml["version"] == 1 and ml["influenced"] is False, ml
    assert ml["explanation"] and {"feature", "value", "contribution"} <= set(ml["explanation"][0])
    assert a.versions["ml_model"] == "gate_solana_fresh v1"
    sample = (await db_session.execute(select(MLFeatureSnapshot))).scalar_one()
    assert sample.model_version_id == mv.id and sample.ml_score is not None


async def test_low_ml_confidence_can_only_make_the_gate_wait(db_session, redis_client):
    await add_champion(db_session, positive=False)
    await require_ml_confidence(db_session, 0.6)
    a = await evaluate(db_session, redis_client)
    assert a.decision.value == "WAIT", a.reasons
    assert a.inputs_snapshot["ml"]["influenced"] is True
    assert (await db_session.execute(select(PaperPosition))).first() is None


async def test_drifted_model_is_ignored(db_session, redis_client):
    await add_champion(db_session, positive=False)
    await redis_client.set(f"{DRIFT_FLAG_PREFIX}gate_solana_fresh", "1")
    await require_ml_confidence(db_session, 0.6)
    a = await evaluate(db_session, redis_client)
    assert a.decision.value == "EXECUTE", a.reasons
    assert a.inputs_snapshot["ml"]["status"] == "ignored - drift detected"
    assert a.versions["ml_model"] is None
