import io
from datetime import datetime, timezone
from typing import Any

import joblib
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ModelVersion
from yonixalpha_core.ml.model import MLModel, NullModel
from yonixalpha_core.ml.sklearn_model import SklearnModel


async def get_active_model_row(session: AsyncSession, name: str) -> ModelVersion | None:
    """The raw ORM row for the current `active` ModelVersion, if any —
    what a training job compares its own holdout metrics against before
    deciding whether to promote a new version. Distinct from
    get_active_model(), which returns something predict()-able instead.
    """
    result = await session.execute(select(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == "active"))
    return result.scalar_one_or_none()


async def get_active_model(session: AsyncSession, name: str) -> MLModel:
    """What an inference caller (decision-engine) actually wants: an
    MLModel it can call .predict() on. Returns NullModel() — never raises,
    never fabricates a score — when no `active` row exists for `name`,
    which per docs/ML.md is the expected state until enough labeled
    outcomes exist to train something real.
    """
    row = await get_active_model_row(session, name)
    if row is None:
        return NullModel()
    estimator = joblib.load(io.BytesIO(row.artifact))
    return SklearnModel(model_id=row.id, name=row.name, version=row.version, estimator=estimator, feature_names=row.feature_names)


async def register_trained_model(
    session: AsyncSession,
    *,
    name: str,
    estimator: Any,
    feature_names: list[str],
    training_sample_count: int,
    metrics: dict,
    status: str = "trained",
) -> ModelVersion:
    """Persists a newly trained estimator as a new, non-active ModelVersion
    row (status="trained") — registration and activation are deliberately
    separate steps (see activate_model) so a model that trained
    successfully but didn't clear the activation bar is still fully
    inspectable rather than silently discarded.
    """
    next_version_result = await session.execute(select(func.coalesce(func.max(ModelVersion.version), 0)).where(ModelVersion.name == name))
    next_version = next_version_result.scalar_one() + 1

    buffer = io.BytesIO()
    joblib.dump(estimator, buffer)

    model_version = ModelVersion(
        name=name,
        version=next_version,
        status=status,
        feature_names=feature_names,
        training_sample_count=training_sample_count,
        metrics=metrics,
        artifact=buffer.getvalue(),
        artifact_format="joblib",
    )
    session.add(model_version)
    await session.flush()
    return model_version


async def activate_model(session: AsyncSession, model_version: ModelVersion) -> None:
    """Retires whatever was previously `active` for this model `name`
    (there is no DB constraint enforcing at most one active row per name —
    a brief multi-active window during this swap is harmless, and a
    caller reading get_active_model_row concurrently just sees one or the
    other, never neither) before promoting the new one. Caller commits.
    """
    await session.execute(
        update(ModelVersion).where(ModelVersion.name == model_version.name, ModelVersion.status == "active").values(status="retired")
    )
    model_version.status = "active"
    model_version.activated_at = datetime.now(timezone.utc)


async def promote_challenger(session: AsyncSession, model_version: ModelVersion, user_id=None, note: str | None = None) -> None:
    """Controlled, audited promotion (spec §45): only a registered challenger
    whose evaluation marked it promotable, and only by an explicit call (the
    dashboard's Promote button). Nothing promotes automatically. Caller
    commits."""
    from yonixalpha_core.db.models import AuditLog

    if model_version.status != "challenger":
        raise ValueError(f"model is {model_version.status}, not a challenger")
    if not (model_version.metrics or {}).get("promotable"):
        raise ValueError("challenger did not pass the promotion criteria")
    previous = await get_active_model_row(session, model_version.name)
    await activate_model(session, model_version)
    session.add(AuditLog(user_id=user_id, event_type="ml.promoted", detail={
        "name": model_version.name, "version": model_version.version,
        "replaced_version": previous.version if previous else None, "note": note,
    }))


async def retire_champion(session: AsyncSession, name: str, user_id=None, reason: str | None = None) -> ModelVersion | None:
    """Fallback: stop using the active model (decisions revert to rules only).
    Audited. Caller commits."""
    from yonixalpha_core.db.models import AuditLog

    row = await get_active_model_row(session, name)
    if row is None:
        return None
    row.status = "retired"
    session.add(AuditLog(user_id=user_id, event_type="ml.champion_retired",
                         detail={"name": name, "version": row.version, "reason": reason}))
    return row
