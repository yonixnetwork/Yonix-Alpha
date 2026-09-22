from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass
class MLPrediction:
    """score is on the same [0, 1] scale as Decision.confidence, so a
    caller can blend it directly. model_id/model_name/model_version are
    always None together (NullModel) or populated together (a real,
    active ModelVersion) — a caller checks model_version to decide whether
    a prediction came from a real model at all, never the score alone
    (a real model can legitimately also score near 0).
    """

    score: float
    model_id: UUID | None
    model_name: str | None
    model_version: int | None
    detail: dict = field(default_factory=dict)


class MLModel(Protocol):
    def predict(self, features: dict[str, float]) -> MLPrediction: ...


class NullModel:
    """The honest default wherever no `active` ModelVersion row exists for
    a given name — which, per docs/ML.md, is every model name in this
    database today (training requires labeled outcomes, and nothing has
    ever closed a real or paper position). Its prediction's model_version
    is always None: callers use that as the signal to skip ML blending
    entirely rather than blending in a fabricated "neutral" score, which
    would silently bias confidence toward 0.5 for candidates that have no
    real model backing them.
    """

    def predict(self, features: dict[str, float]) -> MLPrediction:
        return MLPrediction(
            score=0.0,
            model_id=None,
            model_name=None,
            model_version=None,
            detail={"reason": "no active trained model"},
        )
