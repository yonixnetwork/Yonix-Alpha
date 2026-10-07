from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import review_cache
from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import audit, jsonable, user_id
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.ml import MLStatsOut, ModelVersionOut
from yonixalpha_core import events, opportunities
from yonixalpha_core.db.models import DataQualityEvent, MLFeatureSnapshot, ModelVersion, OpportunityOutcome
from yonixalpha_core.ml import registry
from yonixalpha_core.ml.gate_features import ACTIVE_MODELS, DRIFT_FLAG_PREFIX, ENGINES_FOR_MODEL, FEATURE_VERSION, FEATURES_FOR_MODEL

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/models", response_model=Page[ModelVersionOut])
async def list_models(
    name: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[ModelVersionOut]:
    filters = []
    if name is not None:
        filters.append(ModelVersion.name == name)
    if status_filter is not None:
        filters.append(ModelVersion.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(ModelVersion).where(*filters))).scalar_one()
    result = await db.execute(select(ModelVersion).where(*filters).order_by(ModelVersion.trained_at.desc()).limit(limit).offset(offset))
    models = result.scalars().all()
    return Page(items=[ModelVersionOut.model_validate(m) for m in models], total=total, limit=limit, offset=offset)


@router.get("/stats", response_model=MLStatsOut)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> MLStatsOut:
    """Per docs/ML.md: labeled_features is expected to be 0 until paper
    trading (or a future live trade) closes a position with a known
    outcome — this reports the real count, never a placeholder.
    """
    total = (await db.execute(select(func.count()).select_from(MLFeatureSnapshot))).scalar_one()
    labeled = (await db.execute(select(func.count()).select_from(MLFeatureSnapshot).where(MLFeatureSnapshot.label.is_not(None)))).scalar_one()
    return MLStatsOut(total_features=total, labeled_features=labeled, unlabeled_features=total - labeled)


# --- review (spec §43-46): champion/challenger, promotion, drift, data quality ---

class PromoteIn(BaseModel):
    note: str | None = Field(None, max_length=500)


class RetireIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _model_summary(m: ModelVersion | None) -> dict | None:
    if m is None:
        return None
    metrics = {k: v for k, v in (m.metrics or {}).items() if k != "reference"}
    return jsonable({"id": m.id, "name": m.name, "version": m.version, "status": m.status, "feature_names": m.feature_names,
                     "training_samples": m.training_sample_count, "trained_at": m.trained_at, "activated_at": m.activated_at,
                     "metrics": metrics})


@router.get("/review")
async def review(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                 _: str = Depends(get_current_username)) -> list[dict]:
    out = []
    for name in ACTIVE_MODELS:
        engines = ENGINES_FOR_MODEL[name]
        champion = await registry.get_active_model_row(db, name)
        challenger = (await db.execute(select(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == "challenger")
                                       .order_by(ModelVersion.version.desc()).limit(1))).scalar_one_or_none()
        counts = (await db.execute(select(
            func.count(),
            func.count().filter(MLFeatureSnapshot.label.is_not(None)),
            func.count().filter(MLFeatureSnapshot.quality_status == "ok"),
            func.count().filter(MLFeatureSnapshot.quality_status == "quarantined"),
            func.count().filter(MLFeatureSnapshot.ml_score.is_not(None)),
        ).where(MLFeatureSnapshot.engine.in_(engines), MLFeatureSnapshot.feature_version == FEATURE_VERSION))).one()
        out.append({
            "model": name, "engines": engines, "feature_version": FEATURE_VERSION, "features": FEATURES_FOR_MODEL[name],
            "champion": _model_summary(champion), "challenger": _model_summary(challenger),
            "drift_flag": bool(await redis.exists(f"{DRIFT_FLAG_PREFIX}{name}")),
            "drift": ((champion.metrics or {}).get("drift") if champion else None),
            "samples": {"total": counts[0], "labeled": counts[1], "quality_ok": counts[2], "quarantined": counts[3],
                        "scored": counts[4]},
            "mode": "RULES ONLY" if champion is None else ("ML IGNORED (drift)" if await redis.exists(f"{DRIFT_FLAG_PREFIX}{name}")
                                                          else "ML ADVISORY (can only add caution)"),
        })
    return out


@router.get("/readiness")
async def readiness(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                    _: str = Depends(get_current_username)) -> dict:
    """ML readiness lifecycle per Solana decision model, whether ML touches
    any decision right now (and why not), and what the learning dataset
    holds. Read-only."""
    from yonixalpha_core.ml import readiness as rd
    from yonixalpha_core.safety.store import load_settings

    engines = ("solana_fresh", "solana_momentum", "solana_migration")
    conf = {}
    for e in engines:
        s, _meta = await load_settings(db, e)
        conf[e] = s.min_ml_confidence
    models = [m for m in await rd.model_readiness(db, redis, conf) if set(m["engines"]) & set(engines)]
    return jsonable({"models": models, "dataset": await rd.dataset_overview(db), "states": list(rd.STATES),
                     "ml_contributing": any(m["contributing"] for m in models)})


@router.get("/ablation")
async def ablation(redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """The last scanner-intelligence feature ablation (A–F, leave-one-out,
    activity-matched validation), computed by the ml service every 6 h or by
    `python -m app.ablation`. Read-only; nothing here changes a decision."""
    import json

    from yonixalpha_core.ml.readiness import ABLATION_KEY

    raw = await redis.get(ABLATION_KEY)
    if not raw:
        return {"status": "NOT_RUN", "reason": "no ablation result yet: the ml service runs it every 6 h once at least 200 "
                                                "completed, labelled ledger rows exist"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"status": "UNREADABLE"}


@router.post("/models/{model_id}/promote")
async def promote(model_id: UUID, body: PromoteIn, request: Request, db: AsyncSession = Depends(get_db),
                  redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    mv = await db.get(ModelVersion, model_id)
    if mv is None:
        raise HTTPException(404, "model not found")
    try:
        await registry.promote_challenger(db, mv, await user_id(db, username), body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    await redis.delete(f"{DRIFT_FLAG_PREFIX}{mv.name}")
    await events.publish(redis, "ml.model.updated", {"model": mv.name, "version": mv.version, "status": "active"}, "api")
    return _model_summary(mv)


@router.post("/{name}/retire")
async def retire(name: str, body: RetireIn, request: Request, db: AsyncSession = Depends(get_db),
                 redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    row = await registry.retire_champion(db, name, await user_id(db, username), body.reason)
    if row is None:
        raise HTTPException(404, "no active model with that name")
    await db.commit()
    await events.publish(redis, "ml.model.updated", {"model": name, "version": row.version, "status": "retired"}, "api")
    return {"retired": _model_summary(row), "note": "decisions now use rules only for this model's engines"}


@router.get("/predictions")
async def predictions(model: str | None = None, limit: int = Query(100, ge=1, le=MAX_PAGE_LIMIT),
                      db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> list[dict]:
    """Scored decisions with their eventual outcome (null until the trade closes)."""
    q = select(MLFeatureSnapshot, ModelVersion.name, ModelVersion.version).join(
        ModelVersion, ModelVersion.id == MLFeatureSnapshot.model_version_id).order_by(MLFeatureSnapshot.created_at.desc()).limit(limit)
    if model:
        q = q.where(ModelVersion.name == model)
    rows = (await db.execute(q)).all()
    return jsonable([{"id": f.id, "model": n, "version": v, "engine": f.engine, "symbol": f.symbol, "score": f.ml_score,
                      "label": f.label, "outcome": f.outcome, "assessment_id": f.assessment_id, "at": f.created_at}
                     for f, n, v in rows])


@router.get("/data-quality")
async def data_quality(limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT), offset: int = Query(0, ge=0),
                       db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    total = (await db.execute(select(func.count()).select_from(DataQualityEvent))).scalar_one()
    by_issue = (await db.execute(select(DataQualityEvent.issue, func.count()).group_by(DataQualityEvent.issue))).all()
    rows = (await db.execute(select(DataQualityEvent).order_by(DataQualityEvent.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return jsonable({"total": total, "by_issue": dict(by_issue),
                     "items": [{"id": e.id, "source": e.source, "record_type": e.record_type, "record_id": e.record_id,
                                "issue": e.issue, "detail": e.detail, "at": e.created_at} for e in rows]})


@router.get("/samples")
async def samples(engine: str | None = None, labeled: bool | None = None, limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
                  offset: int = Query(0, ge=0), db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    filters = [MLFeatureSnapshot.feature_version == FEATURE_VERSION]
    if engine:
        filters.append(MLFeatureSnapshot.engine == engine)
    if labeled is not None:
        filters.append(MLFeatureSnapshot.label.is_not(None) if labeled else MLFeatureSnapshot.label.is_(None))
    total = (await db.execute(select(func.count()).select_from(MLFeatureSnapshot).where(*filters))).scalar_one()
    rows = (await db.execute(select(MLFeatureSnapshot).where(*filters).order_by(MLFeatureSnapshot.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return jsonable({"total": total, "items": [{"id": f.id, "engine": f.engine, "symbol": f.symbol, "features": f.features,
                                                "label": f.label, "outcome": f.outcome, "quality": f.quality_status,
                                                "score": f.ml_score, "at": f.created_at} for f in rows]})


@router.get("/opportunities")
async def opportunities_list(traded: bool | None = None, stage: str | None = None, losses_only: bool = False,
                             rejected_up: bool = False, mint: str | None = None, category: str | None = None,
                             days: int | None = Query(None, ge=1, le=365),
                             limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT), offset: int = Query(0, ge=0),
                             db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Every recorded opportunity (traded or not) with its decision snapshot,
    forward horizons, trade result and loss analysis. `days` bounds the rows
    (and the total) to the last N days; the dashboard lists always pass it so
    the total is not a count over the whole history (audit 2026-10-07)."""
    filters = []
    if days is not None:
        filters.append(OpportunityOutcome.decided_at >= datetime.now(timezone.utc) - timedelta(days=days))
    if traded is not None:
        filters.append(OpportunityOutcome.traded.is_(traded))
    if stage:
        filters.append(OpportunityOutcome.stage == stage)
    if losses_only:
        filters.append(OpportunityOutcome.loss_analysis.is_not(None))
    if rejected_up:
        filters += [OpportunityOutcome.traded.is_(False), OpportunityOutcome.peak_pct >= opportunities.REJECTED_WINNER_PEAK_PCT]
    if mint:
        filters.append(OpportunityOutcome.mint == mint)
    if category:
        f = opportunities.category_filter(category)
        if f is None:
            raise HTTPException(422, f"unknown category; one of {', '.join(opportunities.REVIEW_CATEGORIES)}")
        filters.append(f)
    total = (await db.execute(select(func.count()).select_from(OpportunityOutcome).where(*filters))).scalar_one()
    rows = (await db.execute(select(OpportunityOutcome).where(*filters).order_by(OpportunityOutcome.decided_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return jsonable({"total": total, "window_days": days, "items": [
        {"id": r.id, "mint": r.mint, "symbol": r.symbol, "engine": r.engine, "stage": r.stage, "decision": r.decision,
         "traded": r.traded, "execution_mode": r.execution_mode, "position_id": r.position_id, "reasons": r.reasons,
         "decided_at": r.decided_at, "snapshot": r.snapshot, "horizons": r.horizons, "peak_pct": r.peak_pct,
         "drawdown_pct": r.drawdown_pct, "migrated_at": r.migrated_at, "trade_result": r.trade_result,
         "loss_analysis": r.loss_analysis, "status": r.status, "path": r.path, "analysis": r.analysis, "labels": r.labels,
         "regime": r.regime, "post_exit": r.post_exit, "ml_shadow": r.ml_shadow, "feature_version": r.feature_version,
         "theoretical_return_pct": r.theoretical_return_pct, "executable_return_pct": r.executable_return_pct}
        for r in rows]})


@router.get("/ledger-review")
async def ledger_review(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db),
                        redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """Observed / traded / rejected, counterfactual classes, exit classes,
    recovery cases, signal vs execution quality, snipe latency, and the
    shadow models' holdout metrics. Review data only: nothing here changes a
    live rule or a position size."""
    async def compute() -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        out = await opportunities.review(db, since)
        shadow = (await db.execute(select(ModelVersion).where(ModelVersion.status == "shadow")
                                   .order_by(ModelVersion.name))).scalars().all()
        out["shadow_models"] = [{"name": m.name, "version": m.version, "target": (m.metrics or {}).get("target"),
                                 "kind": (m.metrics or {}).get("kind"), "trained_at": m.trained_at,
                                 "train_rows": (m.metrics or {}).get("train_rows"), "split": (m.metrics or {}).get("split"),
                                 "holdout": (m.metrics or {}).get("holdout")} for m in shadow]
        out["categories"] = list(opportunities.REVIEW_CATEGORIES)
        return out

    return await review_cache.cached(redis, f"ledger-review:{days}", compute)


@router.get("/opportunities/compare")
async def opportunities_compare(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db),
                                redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """Winning vs losing trades and traded vs rejected-then-up opportunities:
    averages of the decision-time features. Review data only."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return await review_cache.cached(redis, f"opportunities-compare:{days}",
                                     lambda: opportunities.comparison(db, since))


@router.get("/steps")
async def ml_steps(redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """When each ml service step last ran, how long it took and whether it
    failed (yonixalpha_core.ml.steps). A step RUNNING much longer than its
    interval is the one holding the service up."""
    from yonixalpha_core.ml import steps

    return jsonable({"steps": await steps.read(redis),
                     "intervals_s": {"solana_training": 3600, "gate_models": 3600, "solana_shadow": 3600,
                                     "ablation": 3600, "frozen_validation": 3600, "evm_wallet_ml": 1800},
                     "note": "Solana steps run one after another every hour; EVM / wallet ML runs in its own loop "
                             "every 30 minutes, so a slow Solana step cannot hold it up. Absent: not run since the "
                             "ml service started with this version."})


@router.get("/evm")
async def evm_knowledge(days: int = Query(14, ge=1, le=90), db: AsyncSession = Depends(get_db),
                        redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """EVM and wallet-behaviour ML; aggregated over days of samples, so it is
    served from the short review cache (app.api.review_cache)."""
    return await review_cache.cached(redis, f"ml-evm:{days}", lambda: _evm_knowledge(db, days))


async def _evm_knowledge(db: AsyncSession, days: int) -> dict:
    """EVM and wallet-behaviour ML (master §36-44, §75): what the models have
    learned from (samples by kind), the BUY / WAIT / REJECT comparison of the
    rules, the risk layer, the final action and the shadow ML (§41), and the
    shadow models' holdout metrics. ML contribution is 0 %: shadow only."""
    from yonixalpha_core.db.models import CopyEvent, EvmExitSample, WalletTradeLabel
    from yonixalpha_core.ml import evm_samples, exit_samples, wallet_labels

    since = datetime.now(timezone.utc) - timedelta(days=days)
    # aggregated in the database: BSC alone adds ~20k samples a day
    know = await evm_samples.knowledge(db, since)
    each = select(func.jsonb_array_elements_text(WalletTradeLabel.labels).label("l")).where(
        WalletTradeLabel.entry_at >= since, WalletTradeLabel.kind.in_(("EPISODE", "MISSED"))).subquery()
    label_counts = {k: n for k, n in (await db.execute(select(each.c.l, func.count()).group_by(each.c.l))).all()}
    episodes = (await db.execute(select(func.count()).where(WalletTradeLabel.kind == "EPISODE",
                                                            WalletTradeLabel.entry_at >= since))).scalar_one()
    copy_samples = (await db.execute(select(func.count()).where(CopyEvent.outcome.is_not(None),
                                                                CopyEvent.target_at >= since))).scalar_one()
    # §41 exits: a few checkpoints per open position every hour, so a plain load is small
    x = EvmExitSample
    exit_rows = (await db.execute(select(x.verdicts, x.labels).where(x.at >= since).order_by(x.at.desc())
                                  .limit(20000))).all()
    exits = exit_samples.compare([{"verdicts": v, "labels": lab} for v, lab in exit_rows])
    exits["checkpoints"] = len(exit_rows)
    exits["labelled"] = sum(1 for _, lab in exit_rows if lab and "unknown" not in lab)
    models = (await db.execute(select(ModelVersion).where(ModelVersion.status == "shadow", ModelVersion.name.like("shadow_evm_%")
                                                          | ModelVersion.name.like("shadow_wallet_%")
                                                          | ModelVersion.name.like("shadow_exit_%"))
                               .order_by(ModelVersion.name))).scalars().all()
    return jsonable({
        "window_days": days,
        "samples": {**know["samples"], "wallet_episodes": episodes, "wallet_labels": label_counts,
                    "copy_outcomes": copy_samples},
        "comparison": know["comparison"],
        "exits": exits,
        "models": [{"name": m.name, "version": m.version, "target": (m.metrics or {}).get("target"),
                    "kind": (m.metrics or {}).get("kind"), "trained_at": m.trained_at,
                    "train_rows": (m.metrics or {}).get("train_rows"), "dataset_size": (m.metrics or {}).get("dataset_size"),
                    "split": (m.metrics or {}).get("split"), "holdout": (m.metrics or {}).get("holdout")} for m in models],
        "contribution": {"percent": 0, "status": "SHADOW",
                         "why": "EVM, wallet and exit models have no decision consumer: they are scored and compared only "
                                "and stay SHADOW (ML Review, ML governance). No contribution is ever raised by a winning "
                                "streak."},
        "definitions": {"decision_point": f"T+{evm_samples.DECISION_MINUTE} min of each observation",
                        "labels": "upside_50 / upside_100: the price reached +50 % / +100 % within the hour; fast_dump: "
                                  "-50 % within 10 min; return_60m: the price an hour later",
                        "wallet_labels": list(wallet_labels.LABELS),
                        "feature_versions": [evm_samples.FEATURE_VERSION, wallet_labels.FEATURE_VERSION]},
    })


# --- ML governance (master §38-42) ---------------------------------------------------------

class ContributionIn(BaseModel):
    stage: str = Field(min_length=3, max_length=32)
    percent: int = Field(ge=0, le=100)
    note: str | None = Field(None, max_length=500)


async def _latest_reports(db: AsyncSession) -> dict[tuple[str, int], dict]:
    """(model name, version) -> its newest frozen-validation report (pooled
    over the frozen windows the model never saw)."""
    from yonixalpha_core.db.models import MlValidationReport, MlValidationSet

    r, v = MlValidationReport, MlValidationSet
    rows = (await db.execute(select(r, v.family, v.window_start).join(v, v.id == r.set_id)
                             .order_by(v.window_start.desc(), r.evaluated_at.desc()))).all()
    out: dict[tuple[str, int], dict] = {}
    for rep, family, newest in rows:
        key = (rep.model_name, rep.model_version)
        if key in out:
            continue
        m = rep.metrics or {}
        out[key] = {"status": rep.status, "reason": rep.reason, "evaluated_at": rep.evaluated_at, "family": family,
                    "newest_window": newest, "windows": m.get("sets") or [], "n": m.get("n"),
                    "positives": m.get("positives"), "negatives": m.get("negatives"), "auc": m.get("auc"),
                    "auc_lower_bound": m.get("auc_lower_bound"), "brier": m.get("brier"), "ece": m.get("ece"),
                    "accuracy": m.get("accuracy"), "precision": m.get("precision"), "recall": m.get("recall"),
                    "false_positives": m.get("false_positives"), "false_negatives": m.get("false_negatives"),
                    "scored_per": m.get("scored_per"), "decisions": m.get("decisions"),
                    "by_category": m.get("by_category")}
    return out


@router.get("/governance")
async def governance_view(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                          _: str = Depends(get_current_username)) -> dict:
    """Every model: stage, contribution %, consumer, the version that counts
    (champion, else the newest), training and frozen-validation samples,
    out-of-sample score (AUC), calibration (ECE), confidence (AUC lower
    bound) and health; plus the frozen validation sets. Read-only."""
    from yonixalpha_core.db.models import MlValidationReport, MlValidationSet
    from yonixalpha_core.ml import frozen, governance

    contrib = await governance.load(db)
    reports = await _latest_reports(db)
    rows = (await db.execute(select(ModelVersion).where(ModelVersion.status.in_(("active", "challenger", "shadow", "trained")))
                             .order_by(ModelVersion.name, ModelVersion.version.desc()))).scalars().all()
    by_name: dict[str, dict[str, ModelVersion]] = {}
    for m in rows:
        if frozen.family_of(m.name) is not None:
            by_name.setdefault(m.name, {}).setdefault(m.status, m)
    for name in governance.CONTRIBUTABLE:
        by_name.setdefault(name, {})
    models = []
    for name in sorted(by_name):
        st = by_name[name]
        champion = st.get("active")
        shown = champion or st.get("challenger") or st.get("shadow") or st.get("trained")
        met = (shown.metrics or {}) if shown else {}
        rep = reports.get((shown.name, shown.version)) if shown else None
        drift = met.get("drift") if champion is not None else None
        c = contrib.get(name, governance.Contribution())
        spec = governance.CONTRIBUTABLE.get(name)
        if name.startswith("gate_"):
            kind, consumer = "gate", ("safety gate: may only make a decision WAIT below the engine's min_ml_confidence "
                                      "(Risk settings); never approves, sizes or overrides safety")
        elif spec:
            kind, consumer = "contributor", spec["consumer"]
        else:
            kind, consumer = "shadow", "none: scored and compared only (ML Review)"
        regression = met.get("kind") == "regression"
        health, why = (("NOT_APPLICABLE", "regression model: no AUC; see its holdout MAE") if regression
                       else governance.health(validation=rep, drift=drift))
        challenger = st.get("challenger")
        models.append({
            "name": name, "family": frozen.family_of(name), "kind": kind, "consumer": consumer,
            "stage": (c.stage if spec else "GATE_CAUTION_ONLY" if kind == "gate"
                      else c.stage if c.stage in ("OBSERVATION_ONLY", "SHADOW") else "SHADOW"),
            "percent": c.percent if spec else 0, "weight": c.weight() if spec else 0.0,
            "max_stage": (spec or {}).get("max_stage", "SHADOW"), "changed_at": c.changed_at, "changed_by": c.changed_by,
            "raised_at": c.raised_at,
            "version": shown.version if shown else None, "status": shown.status if shown else None,
            "champion_version": champion.version if champion else None,
            "challenger": ({"id": challenger.id, "version": challenger.version,
                            "promotable": bool((challenger.metrics or {}).get("promotable"))} if challenger else None),
            "trained_at": shown.trained_at if shown else None,
            "training_samples": shown.training_sample_count if shown else None,
            "target": met.get("target"), "model_kind": met.get("kind") or ("binary" if shown else None),
            "frozen_excluded": len(met.get("frozen_excluded") or []),
            "validation": rep,
            "validation_samples": (rep or {}).get("n"),
            "out_of_sample_auc": (rep or {}).get("auc"),
            "calibration_ece": (rep or {}).get("ece"),
            "confidence_auc_lower_bound": (rep or {}).get("auc_lower_bound"),
            "health": health, "health_reason": why,
        })
    sets = (await db.execute(select(MlValidationSet).order_by(MlValidationSet.window_start.desc()).limit(60))).scalars().all()
    counts = {k: (n, p) for k, n, p in (await db.execute(select(
        MlValidationReport.set_id, func.count(), func.count().filter(MlValidationReport.status == "PASS"))
        .group_by(MlValidationReport.set_id))).all()}
    return jsonable({
        "models": models,
        "frozen_sets": [{"id": s.id, "family": s.family, "window_start": s.window_start, "window_end": s.window_end,
                         "frozen_at": s.frozen_at, "samples_at_freeze": s.samples, "note": s.note,
                         "reports": counts.get(s.id, (0, 0))[0], "passed": counts.get(s.id, (0, 0))[1]} for s in sets],
        "not_validated": frozen.NOT_AVAILABLE,
        "rules": {"stages": list(governance.STAGES), "step_pct": governance.STEP_PCT, "max_pct": governance.MAX_PCT,
                  "min_days_between_increases": governance.MIN_DAYS_BETWEEN_INCREASES, "pass_rule": frozen.PASS_RULE,
                  "live": governance.LIVE_LOCKED,
                  "freeze": f"one UTC day per family every {frozen.FREEZE_EVERY.days} days, {frozen.LAG.days} days back; "
                            "never trained on from then on"},
        "note": "Contribution starts at 0 % and is raised only by an operator, 5 % at a time, at most weekly, while the "
                "champion's latest frozen-set report is PASS. Nothing raises it automatically; a winning streak is not "
                "an input. ML never overrides a safety rule.",
    })


@router.put("/governance/{name}")
async def governance_set(name: str, body: ContributionIn, request: Request, db: AsyncSession = Depends(get_db),
                         redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Sets a model's stage and contribution %, within ml.governance's rules
    (refused with the reasons otherwise). Audited."""
    from sqlalchemy.dialects.postgresql import insert

    from yonixalpha_core.db.models import PlatformSetting
    from yonixalpha_core.ml import governance

    now = governance.now_utc()
    all_ = await governance.load(db)
    current = all_.get(name, governance.Contribution())
    champion = await registry.get_active_model_row(db, name)
    validation = (await _latest_reports(db)).get((name, champion.version)) if champion else None
    errors = governance.check_change(name, current, body.stage, body.percent, champion=champion is not None,
                                     validation=validation, now=now)
    if errors:
        raise HTTPException(409, {"errors": errors})
    new = governance.apply_change(current, body.stage, body.percent, username, now)
    all_[name] = new
    value = {"models": {k: v.to_dict() for k, v in all_.items()}}
    await db.execute(insert(PlatformSetting).values(key=governance.KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "ml.contribution_changed",
                {"model": name, "before": current.to_dict(), "after": new.to_dict(), "note": body.note,
                 "champion_version": champion.version if champion else None,
                 "validation": (validation or {}).get("status")})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": governance.KEY}, "api")
    return jsonable({"model": name, "contribution": new.to_dict(), "weight": new.weight()})
