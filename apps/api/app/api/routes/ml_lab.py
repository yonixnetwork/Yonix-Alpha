"""ML Training Lab and Signal Center (yonixalpha_core.ml_lab, signal_center).

The operator labels observations, writes ideas that become proposed
experiments, accepts or rejects them, and reads how their signals did. No
endpoint here places an order, changes a mode or a strategy, or promotes a
model: an accepted experiment only records predictions. Every write is
audited."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from app.api.util import audit, jsonable
from yonixalpha_core import ml_lab, signal_center
from yonixalpha_core.db.models import MlExperiment, MlSignal, MlSignalOutcome, OpportunityOutcome
from yonixalpha_core.ml.opportunity_features import NUMERIC

router = APIRouter(prefix="/ml-lab", tags=["ml-lab"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(422, str(exc))


@router.get("/queue")
async def queue(hours: float = Query(6, gt=0, le=72), category: str | None = None, stage: str | None = None,
                limit: int = Query(100, ge=1, le=ml_lab.QUEUE_LIMIT), db: AsyncSession = Depends(get_db),
                _: str = Depends(get_current_username)) -> dict:
    try:
        return jsonable(await ml_lab.queue(db, hours=hours, category=category, stage=stage, limit=limit))
    except ml_lab.LabError as exc:
        raise _bad(exc) from exc


@router.get("/observations/{opp_id}")
async def observation(opp_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    try:
        return jsonable(await ml_lab.timeline(db, opp_id))
    except ml_lab.LabError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/observations/{opp_id}/labels")
async def add_label(opp_id: UUID, body: dict, request: Request, db: AsyncSession = Depends(get_db),
                    username: str = Depends(get_current_username)) -> dict:
    value = body.get("value")
    try:
        value = float(value) if value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "value must be a number") from exc
    try:
        lab = await ml_lab.add_label(db, opp_id, str(body.get("label") or ""), value=value,
                                     notes=(body.get("notes") or None), user=username)
    except ml_lab.LabError as exc:
        raise _bad(exc) from exc
    await audit(db, username, request, "ml_lab.label_added",
                {"opportunity_id": str(opp_id), "label": lab.label, "value": value, "notes": lab.notes})
    await db.commit()
    return jsonable(ml_lab.label_out(lab, await db.get(OpportunityOutcome, opp_id)))


@router.get("/labels")
async def labels(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    return jsonable({**await ml_lab.label_summary(db),
                     "catalog": [{"label": k, "text": t, "verified_by": v} for k, (t, v) in ml_lab.LABELS.items()]})


@router.get("/dataset")
async def dataset(days: int = Query(7, ge=1, le=90), category: str | None = None,
                  format: str = Query("json", pattern="^(json|csv)$"), limit: int = Query(20000, ge=1, le=100000),
                  db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)):
    data = await ml_lab.dataset(db, days=days, category=category, limit=limit)
    if format == "csv":
        name = f"yonix-dataset-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
        return Response(ml_lab.dataset_csv(data), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return jsonable({**{k: v for k, v in data.items() if k != "rows"}, "rows": data["rows"][:200],
                     "rows_shown": min(200, data["count"]), "download": "use format=csv for every row"})


# --- experiments ---------------------------------------------------------------------------------------

def _exp(e: MlExperiment, counts: dict | None = None) -> dict:
    c = (counts or {}).get(e.id, (0, 0))
    return {"id": str(e.id), "idea": e.idea, "status": e.status, "testable": e.testable, "proposal": e.proposal,
            "created_by": e.created_by, "created_at": e.created_at, "decided_by": e.decided_by, "decided_at": e.decided_at,
            "signals": c[0], "evaluated": c[1]}


async def _get(db: AsyncSession, exp_id: UUID) -> MlExperiment:
    e = await db.get(MlExperiment, exp_id)
    if e is None:
        raise HTTPException(404, "experiment not found")
    return e


@router.get("/features")
async def feature_list(_: str = Depends(get_current_username)) -> dict:
    return {"features": list(NUMERIC), "ops": list(ml_lab.OPS), "horizons": list(ml_lab.HORIZONS),
            "categories": sorted(ml_lab.ENGINE_OF)}


@router.post("/ideas")
async def new_idea(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                   username: str = Depends(get_current_username)) -> dict:
    idea = str(body.get("idea") or "").strip()[:4000]
    try:
        prop = ml_lab.propose(idea)
    except ml_lab.LabError as exc:
        raise _bad(exc) from exc
    e = MlExperiment(idea=idea, status="DRAFT", proposal=prop, testable=prop["testable"], created_by=username)
    db.add(e)
    await db.flush()
    await audit(db, username, request, "ml_lab.experiment_proposed", {"id": str(e.id), "idea": idea, "testable": e.testable})
    await db.commit()
    await db.refresh(e)
    return jsonable(_exp(e))


@router.get("/experiments")
async def experiments(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    rows = (await db.execute(select(MlExperiment).order_by(MlExperiment.created_at.desc()).limit(200))).scalars().all()
    counts: dict = {}
    if rows:
        q = (select(MlSignal.experiment_id, func.count(MlSignal.id), func.count(MlSignalOutcome.signal_id))
             .outerjoin(MlSignalOutcome, MlSignalOutcome.signal_id == MlSignal.id)
             .where(MlSignal.experiment_id.in_([e.id for e in rows])).group_by(MlSignal.experiment_id))
        counts = {eid: (n, ev) for eid, n, ev in (await db.execute(q)).all()}
    return jsonable({"experiments": [_exp(e, counts) for e in rows],
                     "note": "an experiment records predictions only; it never places an order or changes a strategy"})


@router.put("/experiments/{exp_id}")
async def edit_experiment(exp_id: UUID, body: dict, request: Request, db: AsyncSession = Depends(get_db),
                          username: str = Depends(get_current_username)) -> dict:
    e = await _get(db, exp_id)
    if e.status != "DRAFT":
        raise HTTPException(409, f"only a DRAFT can be edited (this one is {e.status}); propose a new idea instead")
    try:
        prop = ml_lab.validate_edit(e.proposal or {}, body)
    except ml_lab.LabError as exc:
        raise _bad(exc) from exc
    before = e.proposal
    e.proposal, e.testable = prop, prop["testable"]
    await audit(db, username, request, "ml_lab.experiment_edited", {"id": str(e.id), "before": before, "after": prop})
    await db.commit()
    await db.refresh(e)
    return jsonable(_exp(e))


async def _decide(db: AsyncSession, request: Request, username: str, exp_id: UUID, to: str, allowed: tuple[str, ...]) -> dict:
    e = await _get(db, exp_id)
    if e.status not in allowed:
        raise HTTPException(409, f"cannot move a {e.status} experiment to {to}")
    if to == "ACCEPTED" and not e.testable:
        raise HTTPException(409, {"message": "this experiment is not testable", "reasons": (e.proposal or {}).get("untestable_reasons")})
    before = e.status
    e.status = to
    if to in ("ACCEPTED", "REJECTED"):
        e.decided_by, e.decided_at = username, datetime.now(timezone.utc)
    await audit(db, username, request, f"ml_lab.experiment_{to.lower()}", {"id": str(e.id), "from": before, "to": to})
    await db.commit()
    await db.refresh(e)
    return jsonable(_exp(e))


@router.post("/experiments/{exp_id}/accept")
async def accept(exp_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                 username: str = Depends(get_current_username)) -> dict:
    return await _decide(db, request, username, exp_id, "ACCEPTED", ("DRAFT",))


@router.post("/experiments/{exp_id}/reject")
async def reject(exp_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                 username: str = Depends(get_current_username)) -> dict:
    return await _decide(db, request, username, exp_id, "REJECTED", ("DRAFT",))


@router.post("/experiments/{exp_id}/retire")
async def retire(exp_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                 username: str = Depends(get_current_username)) -> dict:
    return await _decide(db, request, username, exp_id, "RETIRED", ("ACCEPTED",))


@router.get("/experiments/{exp_id}/report")
async def experiment_report(exp_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    await _get(db, exp_id)
    return jsonable(await signal_center.report(db, exp_id))


@router.get("/signals")
async def signals(experiment_id: UUID | None = None, limit: int = Query(100, ge=1, le=500),
                  db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    q = select(MlSignal, MlSignalOutcome).outerjoin(MlSignalOutcome, MlSignalOutcome.signal_id == MlSignal.id)
    if experiment_id:
        q = q.where(MlSignal.experiment_id == experiment_id)
    rows = (await db.execute(q.order_by(MlSignal.decided_at.desc()).limit(limit))).all()
    return jsonable({"signals": [{"id": str(s.id), "key": s.signal_key, "experiment_id": s.experiment_id, "mint": s.mint,
                                  "category": s.category, "decided_at": s.decided_at, "created_at": s.created_at,
                                  "horizon": s.horizon, "prediction": s.prediction, "inputs": s.inputs,
                                  "definition": s.definition, "outcome": o.outcome if o else None,
                                  "evaluated_at": o.evaluated_at if o else None} for s, o in rows],
                     "note": "signals are immutable predictions recorded before their horizon; outcomes are stored separately"})
