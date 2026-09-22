import uuid

from yonixalpha_core.ml.model import MLPrediction, NullModel
from yonixalpha_core.ml.sklearn_model import SklearnModel


class _FakeEstimator:
    """Stands in for a fitted sklearn classifier: predict_proba's contract
    is a 2D array of [P(class=0), P(class=1)] per row — this fake returns
    a fixed probability so SklearnModel's feature-ordering logic can be
    tested without actually fitting a model.
    """

    def __init__(self, positive_proba: float):
        self._positive_proba = positive_proba
        self.last_vector = None

    def predict_proba(self, vector):
        self.last_vector = vector
        return [[1 - self._positive_proba, self._positive_proba]]


def test_null_model_has_no_model_version():
    prediction = NullModel().predict({"anything": 1.0})
    assert prediction.model_version is None
    assert prediction.model_id is None
    assert prediction.model_name is None


def test_sklearn_model_maps_named_features_to_trained_column_order():
    estimator = _FakeEstimator(positive_proba=0.73)
    model_id = uuid.uuid4()
    model = SklearnModel(model_id=model_id, name="test-model", version=3, estimator=estimator, feature_names=["b", "a"])

    prediction = model.predict({"a": 1.0, "b": 2.0})

    assert estimator.last_vector == [[2.0, 1.0]]  # ["b", "a"] order, not dict insertion order
    assert prediction == MLPrediction(score=0.73, model_id=model_id, model_name="test-model", model_version=3, detail={})


def test_sklearn_model_raises_on_missing_feature():
    model = SklearnModel(model_id=uuid.uuid4(), name="test-model", version=1, estimator=_FakeEstimator(0.5), feature_names=["a", "b"])

    try:
        model.predict({"a": 1.0})
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "b" in str(exc)
