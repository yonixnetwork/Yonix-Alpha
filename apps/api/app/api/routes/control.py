"""Control center: runtime risk settings, modes, blacklist, custom rules,
safety-gate decisions (incl. rejected/waiting opportunities and manual
approvals), paper accounts, and pipeline health.

Every write is validated by the same core functions the engines use and
recorded in the audit log. Nothing here can enable live trading: that
needs the environment locks, which the API only reports.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.schemas.common import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, Page
from app.schemas.control import (
    AssessmentDetail,
    AssessmentSummary,
    BlacklistIn,
    BlacklistOut,
    CustomRuleIn,
    CustomRuleOut,
    EnabledUpdate,
    FollowGlobal,
    ModesOut,
    ModeUpdate,
    PaperAccountOut,
    PaperResetIn,
    PipelineOut,
    SettingsChanges,
    SettingsOut,
    SettingsSaved,
    SettingsSavedAll,
    SettingsUpdate,
    SettingsVersionOut,
    TimelineEventOut,
)
from yonixalpha_core import config_validation, execution_funnel, kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import (
    AuditLog,
    BlacklistEntry,
    CustomRuleEntry,
    PaperAccount,
    PaperPosition,
    RiskAssessment,
    RiskSettingsVersion,
    TradeTimelineEvent,
    TradingCandidate,
    User,
)
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode
from yonixalpha_core.safety.rules import scam_name_preset, validate_blacklist_rule, validate_custom_rule
from yonixalpha_core.safety.settings import ENUM_FIELDS, HARD_LIMITS, default_settings_for, settings_to_dict
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.assembler import RULE_FIELDS
from yonixalpha_core.strategies.catalog import MODE_KEYS
from yonixalpha_core.state_machine import CandidateState, apply_transition

router = APIRouter(prefix="/control", tags=["control"])

SCOPES = ["GLOBAL", "solana_fresh", "solana_migration", "solana_momentum"]
STRATEGIES = MODE_KEYS
APPROVAL_WINDOW = timedelta(minutes=10)
FUNNEL_KEY = f"{pump_stream.PREFIX}:funnel"


async def _user_id(db: AsyncSession, username: str) -> UUID | None:
    return (await db.execute(select(User.id).where(User.username == username))).scalar_one_or_none()


async def _audit(db: AsyncSession, username: str, request: Request, event_type: str, detail: dict) -> None:
    db.add(AuditLog(user_id=await _user_id(db, username), event_type=event_type,
                    ip_address=request.client.host if request.client else None, detail=detail))


def _check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise HTTPException(404, f"unknown scope; one of {SCOPES}")


# --- settings -----------------------------------------------------------------

@router.get("/settings/{scope}", response_model=SettingsOut)
async def get_risk_settings(scope: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> SettingsOut:
    _check_scope(scope)
    effective, source = await store.load_settings(db, scope)
    return SettingsOut(
        scope_links=await _scope_links(db, scope),
        scope=scope,
        effective=settings_to_dict(effective),
        source=source,
        defaults=settings_to_dict(default_settings_for(scope)),
        hard_limits={k: {"kind": kind, "bound": str(bound)} for k, (kind, bound) in HARD_LIMITS.items()},
        enums={k: list(v) for k, v in ENUM_FIELDS.items()},
    )


async def _scope_links(db: AsyncSession, scope: str) -> dict:
    """GLOBAL applies to every engine except for the keys an engine
    overrides (listed with the engine's value)."""
    engines = {s: await store.engine_overrides(db, s) for s in SCOPES if s != store.GLOBAL_SCOPE}
    if scope == store.GLOBAL_SCOPE:
        return {"overridden_by": [{"scope": s, "version": e["version"], "mode": e["mode"], "keys": e["keys"],
                                   "values": e["values"]}
                                  for s, e in engines.items() if e["keys"]],
                "follows_global": [s for s, e in engines.items() if not e["keys"]]}
    e = engines[scope]
    glob, _ = await store.load_settings(db, store.GLOBAL_SCOPE)
    g = settings_to_dict(glob)
    return {"own_settings": bool(e["keys"]), "follows_global": not e["keys"], "mode": e["mode"],
            "override_keys": e["keys"], "global_values": {k: g.get(k) for k in e["keys"]}}


@router.put("/settings/{scope}", response_model=SettingsSaved)
async def put_risk_settings(
    scope: str, body: SettingsUpdate, request: Request,
    db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username),
) -> SettingsSaved:
    _check_scope(scope)
    try:
        row, notes = await store.save_settings(db, scope, body.settings, await _user_id(db, username), body.note)
    except store.SettingsError as exc:
        raise HTTPException(422, {"errors": exc.errors}) from exc
    await db.commit()
    return SettingsSaved(version=SettingsVersionOut.model_validate(row), clamp_notes=notes)


@router.post("/settings/{scope}/follow-global", response_model=SettingsSaved)
async def follow_global(scope: str, request: Request, body: FollowGlobal | None = None, db: AsyncSession = Depends(get_db),
                        username: str = Depends(get_current_username)) -> SettingsSaved:
    """This engine uses GLOBAL for `keys` (every value when no keys are given)."""
    _check_scope(scope)
    if scope == store.GLOBAL_SCOPE:
        raise HTTPException(422, "GLOBAL cannot follow itself")
    row = await store.follow_global(db, scope, await _user_id(db, username), keys=body.keys if body else None)
    await db.commit()
    return SettingsSaved(version=SettingsVersionOut.model_validate(row), clamp_notes=[])


@router.post("/settings-all", response_model=SettingsSavedAll)
async def save_for_all_engines(body: SettingsChanges, request: Request, db: AsyncSession = Depends(get_db),
                               username: str = Depends(get_current_username)) -> SettingsSavedAll:
    """Saves the changed keys once for every engine: written into GLOBAL, and
    removed from every engine that overrode them. One transaction."""
    if not body.changes:
        raise HTTPException(422, "no changes")
    try:
        row, notes, touched = await store.save_for_all_engines(db, body.changes, await _user_id(db, username),
                                                               [s for s in SCOPES if s != store.GLOBAL_SCOPE], body.note)
    except store.SettingsError as exc:
        raise HTTPException(422, {"errors": exc.errors}) from exc
    await db.commit()
    return SettingsSavedAll(version=SettingsVersionOut.model_validate(row), clamp_notes=notes, engines_updated=touched)


@router.get("/settings/{scope}/history", response_model=list[SettingsVersionOut])
async def settings_history(scope: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)):
    _check_scope(scope)
    rows = (await db.execute(select(RiskSettingsVersion).where(RiskSettingsVersion.scope == scope)
                             .order_by(RiskSettingsVersion.version.desc()).limit(50))).scalars().all()
    return [SettingsVersionOut.model_validate(r) for r in rows]


# --- modes --------------------------------------------------------------------

@router.get("/modes", response_model=ModesOut)
async def get_modes(db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings),
                    _: str = Depends(get_current_username)) -> ModesOut:
    return ModesOut(
        global_mode=(await store.load_global_mode(db)).value,
        strategies={s: (await store.load_strategy_mode(db, s)).value for s in STRATEGIES},
        env={
            "trading_enabled": settings.TRADING_ENABLED,
            "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
            "paper_trading": settings.PAPER_TRADING,
            "live_permitted": store.live_trading_permitted(settings),
        },
    )


@router.put("/modes/global", response_model=ModesOut)
async def put_global_mode(body: ModeUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)):
    try:
        mode = GlobalMode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, f"mode must be one of {[m.value for m in GlobalMode]}") from exc
    if mode == GlobalMode.LIVE and not store.live_trading_permitted(settings):
        raise HTTPException(409, "LIVE refused: environment locks are closed (TRADING_ENABLED, LIVE_TRADING_ENABLED, "
                                 "PAPER_TRADING=false are all required and are set on the server, not here)")
    await store.set_global_mode(db, mode, await _user_id(db, username))
    if mode == GlobalMode.LIVE:
        await _refuse_on_config_errors(db, settings, None)
    await db.commit()
    return await get_modes(db, settings, username)


@router.put("/modes/strategy/{strategy}", response_model=ModesOut)
async def put_strategy_mode(strategy: str, body: ModeUpdate, request: Request, db: AsyncSession = Depends(get_db),
                            settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)):
    if strategy not in STRATEGIES:
        raise HTTPException(404, f"unknown strategy; one of {STRATEGIES}")
    try:
        mode = StrategyMode(body.mode)
    except ValueError as exc:
        raise HTTPException(422, f"mode must be one of {[m.value for m in StrategyMode]}") from exc
    await store.set_strategy_mode(db, strategy, mode, await _user_id(db, username))
    if mode in (StrategyMode.AUTO, StrategyMode.MANUAL):
        await _refuse_on_config_errors(db, settings, strategy)
    await db.commit()
    return await get_modes(db, settings, username)


async def _refuse_on_config_errors(db: AsyncSession, settings: Settings, mode_key: str | None) -> None:
    """Validates the module configuration as it would be after this (not
    yet committed) change: a module that would then run AUTO/MANUAL (and,
    with global LIVE, live) while in CONFIGURATION_ERROR refuses the change.
    Switching to OFF or PAPER, or anything that leaves such modules
    untouched, is never refused."""
    result = await config_validation.load_and_validate(db, settings)
    affected = [name for name, r in result.items()
                if r["mode"] in (StrategyMode.AUTO.value, StrategyMode.MANUAL.value)
                and (mode_key is None or mode_key in r["mode_keys"])]
    errors = [e for name in affected for e in config_validation.blocking_errors(result, name)]
    if errors:
        await db.rollback()
        raise HTTPException(409, {"detail": "CONFIGURATION_ERROR: the change needs configuration that is missing in .env",
                                  "errors": errors})


# --- blacklist ----------------------------------------------------------------

@router.get("/blacklist", response_model=list[BlacklistOut])
async def list_blacklist(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)):
    rows = (await db.execute(select(BlacklistEntry).order_by(BlacklistEntry.created_at.desc()))).scalars().all()
    return [BlacklistOut.model_validate(r) for r in rows]


@router.post("/blacklist", response_model=BlacklistOut, status_code=201)
async def add_blacklist(body: BlacklistIn, request: Request, db: AsyncSession = Depends(get_db),
                        username: str = Depends(get_current_username)):
    value = body.value.strip()
    errors = validate_blacklist_rule(body.scope, body.field, value, body.match_type, body.action)
    if errors:
        raise HTTPException(422, {"errors": errors})
    dup = await db.execute(select(BlacklistEntry.id).where(
        BlacklistEntry.scope == body.scope, BlacklistEntry.field == body.field, BlacklistEntry.action == body.action,
        BlacklistEntry.match_type == body.match_type, func.lower(BlacklistEntry.value) == value.lower()))
    if dup.first() is not None:
        raise HTTPException(409, "an identical rule already exists")
    row = BlacklistEntry(scope=body.scope, field=body.field, match_type=body.match_type, action=body.action, value=value,
                         reason=body.reason, created_by=await _user_id(db, username))
    db.add(row)
    await db.flush()
    await _audit(db, username, request, "blacklist.added", {"id": str(row.id), **body.model_dump()})
    await db.commit()
    return BlacklistOut.model_validate(row)


@router.post("/blacklist/presets/scam-names")
async def add_scam_name_preset(request: Request, db: AsyncSession = Depends(get_db),
                               username: str = Depends(get_current_username)) -> dict[str, int]:
    """Installs the scam/impersonation word lists as ordinary GLOBAL BLOCK
    rules. Rules that already exist are skipped, so it is safe to repeat."""
    added = skipped = 0
    user_id = await _user_id(db, username)
    for r in scam_name_preset():
        dup = await db.execute(select(BlacklistEntry.id).where(
            BlacklistEntry.scope == r["scope"], BlacklistEntry.field == r["field"], BlacklistEntry.action == r["action"],
            BlacklistEntry.match_type == r["match_type"], func.lower(BlacklistEntry.value) == r["value"]))
        if dup.first() is not None:
            skipped += 1
            continue
        db.add(BlacklistEntry(created_by=user_id, **r))
        added += 1
    await db.flush()
    await _audit(db, username, request, "blacklist.preset_added", {"preset": "scam-names", "added": added, "skipped": skipped})
    await db.commit()
    return {"added": added, "skipped": skipped}


@router.patch("/blacklist/{rule_id}", response_model=BlacklistOut)
async def toggle_blacklist(rule_id: UUID, body: EnabledUpdate, request: Request, db: AsyncSession = Depends(get_db),
                           username: str = Depends(get_current_username)):
    row = await db.get(BlacklistEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    row.enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    await _audit(db, username, request, "blacklist.toggled", {"id": str(rule_id), "enabled": body.enabled})
    await db.commit()
    return BlacklistOut.model_validate(row)


@router.put("/blacklist/{rule_id}", response_model=BlacklistOut)
async def edit_blacklist(rule_id: UUID, body: BlacklistIn, request: Request, db: AsyncSession = Depends(get_db),
                         username: str = Depends(get_current_username)):
    row = await db.get(BlacklistEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    value = body.value.strip()
    errors = validate_blacklist_rule(body.scope, body.field, value, body.match_type, body.action)
    if errors:
        raise HTTPException(422, {"errors": errors})
    before = {"scope": row.scope, "field": row.field, "match_type": row.match_type, "action": row.action, "value": row.value,
              "reason": row.reason}
    row.scope, row.field, row.match_type, row.value, row.reason = body.scope, body.field, body.match_type, value, body.reason
    row.action = body.action
    row.updated_at = datetime.now(timezone.utc)
    await _audit(db, username, request, "blacklist.edited", {"id": str(rule_id), "before": before, "after": body.model_dump()})
    await db.commit()
    return BlacklistOut.model_validate(row)


@router.delete("/blacklist/{rule_id}", status_code=204)
async def delete_blacklist(rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                           username: str = Depends(get_current_username)) -> None:
    row = await db.get(BlacklistEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    await _audit(db, username, request, "blacklist.deleted",
                 {"id": str(rule_id), "scope": row.scope, "field": row.field, "value": row.value})
    await db.delete(row)
    await db.commit()


# --- custom rules -------------------------------------------------------------

@router.get("/rules", response_model=list[CustomRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)):
    rows = (await db.execute(select(CustomRuleEntry).order_by(CustomRuleEntry.created_at.desc()))).scalars().all()
    return [CustomRuleOut.model_validate(r) for r in rows]


@router.get("/rules/fields", response_model=list[str])
async def rule_fields(_: str = Depends(get_current_username)):
    return sorted(RULE_FIELDS)


@router.post("/rules", response_model=CustomRuleOut, status_code=201)
async def add_rule(body: CustomRuleIn, request: Request, db: AsyncSession = Depends(get_db),
                   username: str = Depends(get_current_username)):
    errors = validate_custom_rule(body.field, body.op, body.threshold, body.action, RULE_FIELDS)
    if body.scope not in SCOPES:
        errors.append(f"scope must be one of {SCOPES}")
    if errors:
        raise HTTPException(422, {"errors": errors})
    row = CustomRuleEntry(**body.model_dump(), created_by=await _user_id(db, username))
    db.add(row)
    await db.flush()
    await _audit(db, username, request, "custom_rule.added", {"id": str(row.id), **body.model_dump()})
    await db.commit()
    return CustomRuleOut.model_validate(row)


@router.patch("/rules/{rule_id}", response_model=CustomRuleOut)
async def toggle_rule(rule_id: UUID, body: EnabledUpdate, request: Request, db: AsyncSession = Depends(get_db),
                      username: str = Depends(get_current_username)):
    row = await db.get(CustomRuleEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    row.enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    await _audit(db, username, request, "custom_rule.toggled", {"id": str(rule_id), "enabled": body.enabled})
    await db.commit()
    return CustomRuleOut.model_validate(row)


@router.put("/rules/{rule_id}", response_model=CustomRuleOut)
async def edit_rule(rule_id: UUID, body: CustomRuleIn, request: Request, db: AsyncSession = Depends(get_db),
                    username: str = Depends(get_current_username)):
    row = await db.get(CustomRuleEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    errors = validate_custom_rule(body.field, body.op, body.threshold, body.action, RULE_FIELDS)
    if body.scope not in SCOPES:
        errors.append(f"scope must be one of {SCOPES}")
    if errors:
        raise HTTPException(422, {"errors": errors})
    before = {k: getattr(row, k) for k in body.model_dump()}
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    row.updated_at = datetime.now(timezone.utc)
    await _audit(db, username, request, "custom_rule.edited", {"id": str(rule_id), "before": before, "after": body.model_dump()})
    await db.commit()
    return CustomRuleOut.model_validate(row)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                      username: str = Depends(get_current_username)) -> None:
    row = await db.get(CustomRuleEntry, rule_id)
    if row is None:
        raise HTTPException(404, "rule not found")
    await _audit(db, username, request, "custom_rule.deleted", {"id": str(rule_id), "name": row.name})
    await db.delete(row)
    await db.commit()


# --- assessments / decisions --------------------------------------------------

def _summary(r: RiskAssessment) -> dict:
    a = r.assessment or {}
    size = ((a.get("plan") or {}).get("position_size") or {}).get("value")
    return dict(id=r.id, candidate_id=r.candidate_id, engine=r.engine, strategy=r.strategy, asset_id=r.asset_id,
                symbol=r.symbol, decision=r.decision, status_label=r.status_label, executable=r.executable,
                execution_target=r.execution_target, overall_risk=r.overall_risk, approval_state=r.approval_state,
                reasons=list(a.get("reasons") or []), position_size=size, evaluated_at=r.evaluated_at, outcome=r.outcome)


@router.get("/assessments", response_model=Page[AssessmentSummary])
async def list_assessments(
    decision: str | None = None,
    engine: str | None = None,
    approval_state: str | None = None,
    asset_id: str | None = None,
    strategy: str | None = None,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> Page[AssessmentSummary]:
    filters = []
    if decision:
        filters.append(RiskAssessment.decision.in_(decision.split(",")))
    if engine:
        filters.append(RiskAssessment.engine == engine)
    if approval_state:
        filters.append(RiskAssessment.approval_state == approval_state)
    if asset_id:
        filters.append(RiskAssessment.asset_id == asset_id)
    if strategy:
        filters.append(RiskAssessment.strategy == strategy)
    total = (await db.execute(select(func.count()).select_from(RiskAssessment).where(*filters))).scalar_one()
    rows = (await db.execute(select(RiskAssessment).where(*filters).order_by(RiskAssessment.evaluated_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return Page(items=[AssessmentSummary(**_summary(r)) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/assessments/{assessment_id}", response_model=AssessmentDetail)
async def get_assessment(assessment_id: UUID, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)):
    r = await db.get(RiskAssessment, assessment_id)
    if r is None:
        raise HTTPException(404, "assessment not found")
    q = select(TradeTimelineEvent).order_by(TradeTimelineEvent.occurred_at)
    q = q.where(TradeTimelineEvent.candidate_id == r.candidate_id) if r.candidate_id else q.where(
        TradeTimelineEvent.assessment_id == r.id)
    timeline = (await db.execute(q.limit(500))).scalars().all()
    return AssessmentDetail(**_summary(r), assessment=r.assessment, approved_at=r.approved_at,
                            timeline=[TimelineEventOut.model_validate(t) for t in timeline])


async def _decide_approval(assessment_id: UUID, approve: bool, request: Request, db: AsyncSession, username: str,
                           ignore: bool = False):
    r = await db.get(RiskAssessment, assessment_id)
    if r is None:
        raise HTTPException(404, "assessment not found")
    if r.approval_state != "PENDING":
        raise HTTPException(409, f"assessment is {r.approval_state}, not PENDING")
    now = datetime.now(timezone.utc)
    if approve and now - r.evaluated_at > APPROVAL_WINDOW:
        r.approval_state = "EXPIRED"
        await db.commit()
        raise HTTPException(409, "assessment is older than the approval window; wait for a fresh evaluation")
    if ignore:
        # Dismissed without a verdict: nothing is rejected, the candidate keeps
        # being evaluated, and a later assessment can ask again.
        r.approval_state = "IGNORED"
        await store.add_timeline_event(db, "manual_ignore", now, {"by": username}, candidate_id=r.candidate_id, assessment_id=r.id)
        await _audit(db, username, request, "assessment.ignored", {"assessment_id": str(r.id), "asset_id": r.asset_id, "engine": r.engine})
        await db.commit()
        return AssessmentSummary(**_summary(r))
    r.approval_state = "APPROVED" if approve else "DECLINED"
    r.approved_by = await _user_id(db, username)
    r.approved_at = now
    await store.add_timeline_event(db, "manual_approval" if approve else "manual_decline", now,
                                   {"by": username, "note": "approval lets the next full re-evaluation pass; every check still runs"}
                                   if approve else {"by": username},
                                   candidate_id=r.candidate_id, assessment_id=r.id)
    if not approve and r.candidate_id is not None:
        cand = await db.get(TradingCandidate, r.candidate_id)
        if cand is not None and cand.state in (CandidateState.DISCOVERED.value, CandidateState.OBSERVING.value):
            apply_transition(cand, CandidateState.REJECTED, reason=f"declined by operator {username}")
    await _audit(db, username, request, "assessment.approved" if approve else "assessment.declined",
                 {"assessment_id": str(r.id), "asset_id": r.asset_id, "engine": r.engine})
    await db.commit()
    return AssessmentSummary(**_summary(r))


@router.post("/assessments/{assessment_id}/approve", response_model=AssessmentSummary)
async def approve_assessment(assessment_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                             username: str = Depends(get_current_username)):
    return await _decide_approval(assessment_id, True, request, db, username)


@router.post("/assessments/{assessment_id}/decline", response_model=AssessmentSummary)
async def decline_assessment(assessment_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                             username: str = Depends(get_current_username)):
    return await _decide_approval(assessment_id, False, request, db, username)


@router.post("/assessments/{assessment_id}/ignore", response_model=AssessmentSummary)
async def ignore_assessment(assessment_id: UUID, request: Request, db: AsyncSession = Depends(get_db),
                            username: str = Depends(get_current_username)):
    return await _decide_approval(assessment_id, False, request, db, username, ignore=True)


# --- paper accounts -----------------------------------------------------------

async def _account_out(db: AsyncSession, redis: Redis, acct: PaperAccount) -> PaperAccountOut:
    now = datetime.now(timezone.utc)
    state = await store.account_state(db, acct, None, now, await kill_switch.is_engaged(redis))
    closed = (await db.execute(select(func.count(), func.count().filter(PaperPosition.realized_pnl > 0),
                                      func.coalesce(func.sum(PaperPosition.realized_pnl), 0),
                                      func.coalesce(func.sum(PaperPosition.fees_paid_quote), 0))
                               .where(PaperPosition.account_id == acct.id, PaperPosition.status == "closed",
                                      PaperPosition.exit_at >= acct.reset_at))).one()
    return PaperAccountOut(name=acct.name, quote_currency=acct.quote_currency, starting_balance=str(acct.starting_balance),
                           cash_balance=str(acct.cash_balance), equity=str(state.equity), open_positions=state.open_positions,
                           open_exposure=str(state.current_exposure), closed_positions=closed[0], winning_positions=closed[1],
                           realized_pnl=str(closed[2]), fees_paid=str(closed[3]), reset_at=acct.reset_at)


@router.get("/paper/accounts", response_model=list[PaperAccountOut])
async def paper_accounts(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                         _: str = Depends(get_current_username)):
    out = []
    for name in store.ACTIVE_PAPER_ACCOUNTS:
        acct = await store.get_paper_account(db, name)
        out.append(await _account_out(db, redis, acct))
    await db.commit()
    return out


@router.post("/paper/accounts/{name}/reset", response_model=PaperAccountOut)
async def reset_paper_account(name: str, body: PaperResetIn, request: Request, db: AsyncSession = Depends(get_db),
                              redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)):
    if name not in store.ACTIVE_PAPER_ACCOUNTS:
        raise HTTPException(404, "unknown paper account")
    if body.confirm != name:
        raise HTTPException(422, f"type the account name ({name}) in 'confirm' to reset it")
    try:
        balance = Decimal(body.starting_balance)
    except InvalidOperation as exc:
        raise HTTPException(422, "starting_balance must be numeric") from exc
    if not (Decimal(0) < balance <= Decimal(1_000_000)):
        raise HTTPException(422, "starting_balance must be > 0 and ≤ 1,000,000")
    acct = await store.get_paper_account(db, name)
    open_count = (await db.execute(select(func.count()).select_from(PaperPosition).where(
        PaperPosition.account_id == acct.id, PaperPosition.status == "open"))).scalar_one()
    if open_count:
        raise HTTPException(409, f"{open_count} open paper position(s); reset only when flat")
    before = str(acct.cash_balance)
    acct.starting_balance = acct.cash_balance = balance
    acct.reset_at = datetime.now(timezone.utc)
    await _audit(db, username, request, "paper_account.reset", {"name": name, "cash_before": before, "starting_balance": str(balance)})
    await db.commit()
    return await _account_out(db, redis, acct)


# --- pipeline health ----------------------------------------------------------

@router.get("/pipeline", response_model=PipelineOut)
async def pipeline(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)):
    now = datetime.now(timezone.utc)
    hb = await pump_stream.heartbeat(redis)
    stream = {"heartbeat": hb.isoformat() if hb else None,
              "heartbeat_age_seconds": int((now - hb).total_seconds()) if hb else None,
              "counters": await pump_stream.stats(redis),
              "tracked_recent_mints": await redis.zcard(pump_stream.RECENT)}
    funnel = await redis.hgetall(FUNNEL_KEY)
    rows = (await db.execute(select(TradingCandidate.state, func.count()).where(
        TradingCandidate.detail["source"].astext == "pump_stream").group_by(TradingCandidate.state))).all()
    decisions = (await db.execute(select(RiskAssessment.decision, func.count()).where(
        RiskAssessment.evaluated_at >= now - timedelta(hours=24)).group_by(RiskAssessment.decision))).all()
    last = (await db.execute(select(func.max(RiskAssessment.evaluated_at)))).scalar_one()
    return PipelineOut(stream=stream, funnel=funnel, candidates={s: n for s, n in rows},
                       decisions_24h={d: n for d, n in decisions}, last_assessment_at=last)


# --- execution funnel ---------------------------------------------------------

@router.get("/execution-funnel")
async def get_execution_funnel(hours: float = Query(24, gt=0, le=24 * 14), db: AsyncSession = Depends(get_db),
                               redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                               _: str = Depends(get_current_username)) -> dict:
    """Where every Pump.fun token stopped: observed -> candidate -> assessed
    -> BUY signal -> executable -> position -> live order, with the exact
    blocking codes. Counted from the database; nothing is estimated."""
    return await execution_funnel.funnel(db, execution_funnel.since_hours(hours), redis, settings)


@router.get("/execution-funnel/token/{mint}")
async def get_token_trace(mint: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    if not 32 <= len(mint) <= 64 or not mint.isalnum():
        raise HTTPException(422, "not a mint address")
    return await execution_funnel.token_trace(db, mint)
