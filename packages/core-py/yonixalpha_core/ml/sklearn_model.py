from typing import Any
from uuid import UUID

from yonixalpha_core.ml.model import MLPrediction


class SklearnModel:
    """Wraps a fitted scikit-learn classifier (anything exposing
    predict_proba) plus the ordered feature_names it was trained on, so
    prediction always maps a caller's named feature dict onto the exact
    column order the estimator expects — never positional, which would
    silently mismatch if a future feature set is reordered.
    """

    def __init__(self, *, model_id: UUID, name: str, version: int, estimator: Any, feature_names: list[str]) -> None:
        self._model_id = model_id
        self._name = name
        self._version = version
        self._estimator = estimator
        self._feature_names = feature_names

    def predict(self, features: dict[str, float]) -> MLPrediction:
        missing = [f for f in self._feature_names if f not in features]
        if missing:
            raise ValueError(f"SklearnModel {self._name} v{self._version} missing required features: {missing}")
        vector = [[features[name] for name in self._feature_names]]
        score = float(self._estimator.predict_proba(vector)[0][1])
        return MLPrediction(score=score, model_id=self._model_id, model_name=self._name, model_version=self._version, detail={})
