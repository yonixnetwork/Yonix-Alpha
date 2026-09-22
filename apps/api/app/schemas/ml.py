from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    version: int
    status: str
    feature_names: list[Any]
    training_sample_count: int
    metrics: dict[str, Any]
    artifact_format: str
    trained_at: datetime
    activated_at: datetime | None
    created_at: datetime
    # `artifact` (the joblib-serialized estimator bytes) is deliberately
    # excluded — there is no dashboard use for shipping a binary blob to
    # the browser, and it can be large.


class MLStatsOut(BaseModel):
    total_features: int
    labeled_features: int
    unlabeled_features: int
