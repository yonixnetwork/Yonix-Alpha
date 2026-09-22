import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"
)

import pytest_asyncio  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402 - registers models on Base.metadata
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.ml import registry  # noqa: E402
from yonixalpha_core.ml.model import NullModel  # noqa: E402
from yonixalpha_core.ml.sklearn_model import SklearnModel  # noqa: E402


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = make_session_factory(engine)

    async with session_factory() as session:
        yield session

    await engine.dispose()


def _fitted_estimator() -> LogisticRegression:
    estimator = LogisticRegression()
    estimator.fit([[0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0]], [0, 1, 0, 1])
    return estimator


async def test_get_active_model_returns_null_model_when_none_registered(db_session):
    model = await registry.get_active_model(db_session, "solana_candidate_momentum")
    assert isinstance(model, NullModel)


async def test_register_trained_model_does_not_activate_it(db_session):
    model_version = await registry.register_trained_model(
        db_session,
        name="solana_candidate_momentum",
        estimator=_fitted_estimator(),
        feature_names=["a", "b"],
        training_sample_count=4,
        metrics={"holdout_auc": 0.9},
    )
    await db_session.commit()

    assert model_version.status == "trained"
    assert model_version.version == 1
    assert model_version.activated_at is None

    model = await registry.get_active_model(db_session, "solana_candidate_momentum")
    assert isinstance(model, NullModel)  # trained but never activated -> still no active model


async def test_version_numbers_increment_per_name(db_session):
    first = await registry.register_trained_model(
        db_session, name="m", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    second = await registry.register_trained_model(
        db_session, name="m", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    other_name = await registry.register_trained_model(
        db_session, name="other", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    await db_session.commit()

    assert first.version == 1
    assert second.version == 2
    assert other_name.version == 1  # independent numbering per name


async def test_activate_model_makes_it_the_active_model(db_session):
    model_version = await registry.register_trained_model(
        db_session,
        name="solana_candidate_momentum",
        estimator=_fitted_estimator(),
        feature_names=["a", "b"],
        training_sample_count=4,
        metrics={"holdout_auc": 0.9},
    )
    await registry.activate_model(db_session, model_version)
    await db_session.commit()

    assert model_version.status == "active"
    assert model_version.activated_at is not None

    model = await registry.get_active_model(db_session, "solana_candidate_momentum")
    assert isinstance(model, SklearnModel)

    prediction = model.predict({"a": 1.0, "b": 1.0})
    assert 0.0 <= prediction.score <= 1.0
    assert prediction.model_name == "solana_candidate_momentum"
    assert prediction.model_version == model_version.version


async def test_activating_new_model_retires_previous_active(db_session):
    first = await registry.register_trained_model(
        db_session, name="m", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    await registry.activate_model(db_session, first)
    await db_session.commit()

    second = await registry.register_trained_model(
        db_session, name="m", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    await registry.activate_model(db_session, second)
    await db_session.commit()

    await db_session.refresh(first)
    assert first.status == "retired"
    assert second.status == "active"

    model = await registry.get_active_model(db_session, "m")
    assert isinstance(model, SklearnModel)
    prediction = model.predict({"a": 0.0, "b": 0.0})
    assert prediction.model_version == second.version


async def test_get_active_model_is_scoped_by_name(db_session):
    model_version = await registry.register_trained_model(
        db_session, name="model-a", estimator=_fitted_estimator(), feature_names=["a", "b"], training_sample_count=4, metrics={}
    )
    await registry.activate_model(db_session, model_version)
    await db_session.commit()

    assert isinstance(await registry.get_active_model(db_session, "model-a"), SklearnModel)
    assert isinstance(await registry.get_active_model(db_session, "model-b"), NullModel)
