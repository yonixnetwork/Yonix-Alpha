import io
import joblib
from sklearn.linear_model import LogisticRegression
from sqlalchemy import select

from yonixalpha_core.db.base import make_session_factory
from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion, PaperPosition, RiskAssessment
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FUTURES_FEATURES
from yonixalpha_core.safety import store
from yonixalpha_core.safety.settings import default_settings_for, settings_to_dict

from app.futures_eval import run_strategy

from .test_futures_eval import ENV, NOW, FakeVenue


async def add_champion(session, positive: bool) -> ModelVersion:
    """A champion whose output is dominated by signal_strength: positive
    weight -> high score for a strong signal, negative -> low score."""
    import numpy as np

    est = LogisticRegression().fit([[0, 0, 0, 0, 0], [1, 1, 1, 1, 1]], [0, 1])
    w = 5.0 if positive else -5.0
    est.coef_ = np.array([[0.0, 0.0, 0.0, w, 0.0]])
    est.intercept_ = np.array([-w / 2])
    buf = io.BytesIO()
    joblib.dump(est, buf)
    mv = ModelVersion(name="gate_futures", version=1, status="active", feature_names=FUTURES_FEATURES,
                      training_sample_count=11, metrics={}, artifact=buf.getvalue())
    session.add(mv)
    await session.commit()
    return mv


async def require_ml_confidence(sf, value: float) -> None:
    async with sf() as s:
        data = settings_to_dict(default_settings_for("binance_futures"))
        data["min_ml_confidence"] = value
        await store.save_settings(s, "binance_futures", data, None)
        await s.commit()


async def test_champion_scores_and_explains_but_cannot_bypass_gate(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    mv = await add_champion(db_session, positive=True)
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "EXECUTE", r
    async with sf() as s:
        a = (await s.execute(select(RiskAssessment))).scalar_one()
        sample = (await s.execute(select(MLFeatureSnapshot))).scalar_one()
    ml = a.assessment["inputs_snapshot"]["ml"]
    assert ml["status"] == "scored" and ml["version"] == 1 and ml["influenced"] is False
    assert ml["explanation"] and {"feature", "value", "contribution"} <= set(ml["explanation"][0])
    assert a.assessment["versions"]["ml_model"] == "gate_futures v1"
    assert sample.model_version_id == mv.id and sample.ml_score is not None


async def test_low_ml_confidence_can_only_make_the_gate_wait(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await add_champion(db_session, positive=False)
    await require_ml_confidence(sf, 0.6)
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "WAIT", r
    async with sf() as s:
        a = (await s.execute(select(RiskAssessment))).scalar_one()
        assert a.assessment["inputs_snapshot"]["ml"]["influenced"] is True
        assert (await s.execute(select(PaperPosition))).first() is None


async def test_drifted_model_is_ignored(db_session, redis_client):
    sf = make_session_factory(db_session.bind)
    await add_champion(db_session, positive=False)
    await redis_client.set(f"{DRIFT_FLAG_PREFIX}gate_futures", "1")
    await require_ml_confidence(sf, 0.6)
    r = await run_strategy(sf, redis_client, ENV, {"binance": FakeVenue()}, "meta_muse", NOW)
    assert r["status"] == "EXECUTE", r
    async with sf() as s:
        a = (await s.execute(select(RiskAssessment))).scalar_one()
    assert a.assessment["inputs_snapshot"]["ml"]["status"] == "ignored - drift detected"
    assert a.assessment["versions"]["ml_model"] is None
