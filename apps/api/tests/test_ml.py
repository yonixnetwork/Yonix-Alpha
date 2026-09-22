import pytest

from yonixalpha_core.db.models import MLFeatureSnapshot, ModelVersion

pytestmark = pytest.mark.asyncio


async def _seed_model(app, name="test-model", version=1, status="trained") -> ModelVersion:
    async with app.state.db_session_factory() as session:
        model = ModelVersion(
            name=name,
            version=version,
            status=status,
            feature_names=["a", "b"],
            training_sample_count=60,
            metrics={"holdout_auc": 0.9},
            artifact=b"fake-joblib-bytes",
            artifact_format="joblib",
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return model


async def _seed_feature(app, label=None) -> MLFeatureSnapshot:
    async with app.state.db_session_factory() as session:
        row = MLFeatureSnapshot(symbol="TESTMINT", features={"a": 1.0}, label=label)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_list_models_requires_auth(client):
    resp = await client.get("/api/ml/models")
    assert resp.status_code == 401


async def test_list_models_empty(client, auth_headers):
    resp = await client.get("/api/ml/models", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_list_models_returns_seeded_row_without_artifact_bytes(app, client, auth_headers):
    model = await _seed_model(app)

    resp = await client.get("/api/ml/models", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(model.id)
    assert item["name"] == "test-model"
    assert item["status"] == "trained"
    assert "artifact" not in item


async def test_list_models_filters_by_status(app, client, auth_headers):
    await _seed_model(app, name="m1", version=1, status="trained")
    await _seed_model(app, name="m1", version=2, status="active")

    resp = await client.get("/api/ml/models", params={"status": "active"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "active"


async def test_ml_stats_requires_auth(client):
    resp = await client.get("/api/ml/stats")
    assert resp.status_code == 401


async def test_ml_stats_all_zero_when_empty(client, auth_headers):
    resp = await client.get("/api/ml/stats", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"total_features": 0, "labeled_features": 0, "unlabeled_features": 0}


async def test_ml_stats_counts_labeled_and_unlabeled(app, client, auth_headers):
    await _seed_feature(app, label=None)
    await _seed_feature(app, label=1)
    await _seed_feature(app, label=0)

    resp = await client.get("/api/ml/stats", headers=auth_headers)
    body = resp.json()
    assert body == {"total_features": 3, "labeled_features": 2, "unlabeled_features": 1}
