"""ML engine monitoring (seven-day investigation, Phases 6-7, 2026-10-24).

One read-only view of what the learning system has actually done, from
saved records only (nothing here trains, promotes or trades):

  models        per model: the CHAMPION (operator-promoted, status active)
                and any CHALLENGER (registered, never promoted
                automatically), their frozen-validation health and the
                operator-set contribution (ml.governance)
  experiments   Signal Center experiments by status, each with its verdict
                (signal_center.report) in plain words
  predictions   Signal Center signals and outcomes (recorded before the
                outcome; immutable), and the shadow-model scores split into
                PROSPECTIVE / RETROSPECTIVE (ml.prediction_audit)
  trades        closed PAPER trades per strategy in the window (solana_performance):
                executed positions, kept apart from predictions
  rejected      ledger decisions not traded and what the ledger measured after them
  activity      the ML service's last run of each step (ml.steps) and the
                data sources' health (scanner_health)
  audit         proposed / accepted / rejected / retired experiments and model
                promotions, retirements and contribution changes (audit log)
  status        plain-language state per model, experiment and strategy:
                INSUFFICIENT DATA, OBSERVING, PAPER TESTING, VALIDATION FAILED,
                IMPROVEMENT NOT CONFIRMED, CHALLENGER OUTPERFORMING BASELINE,
                AWAITING MY APPROVAL

history() is the chronological record per decision: signal -> decision ->
paper entry (if any) -> exit -> the ledger's evaluation, each row opening in
the ML Training Lab timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import forward_test, signal_center, solana_performance
from yonixalpha_core.db.models import AuditLog, MlExperiment, MlSignal, MlSignalOutcome, ModelVersion
from yonixalpha_core.ml import prediction_audit

AUDIT_EVENTS = ("ml_lab.experiment_proposed", "ml_lab.experiment_accepted", "ml_lab.experiment_rejected",
                "ml_lab.experiment_retired", "ml_lab.experiment_edited", "ml.promoted", "ml.champion_retired",
                "ml.contribution_changed")
EXPERIMENT_STATUS = {
    "INSUFFICIENT_DATA": "OBSERVING",
    "NOT_SUPPORTED": "IMPROVEMENT NOT CONFIRMED",
    "SUPPORTED_FORWARD": "CHALLENGER OUTPERFORMING BASELINE",
}
STRATEGY_STATUS = {
    "INSUFFICIENT EVIDENCE": "INSUFFICIENT DATA", "NO BASELINE": "PAPER TESTING", "INCONCLUSIVE": "IMPROVEMENT NOT CONFIRMED",
    "IMPROVING": "CHALLENGER OUTPERFORMING BASELINE", "DETERIORATING": "IMPROVEMENT NOT CONFIRMED",
}
HISTORY_SQL = """
SELECT oo.id, oo.decided_at, oo.mint, oo.symbol, oo.engine, oo.stage, oo.decision, oo.traded, oo.status,
       oo.executable_return_pct, oo.peak_pct, left(coalesce(oo.reasons->>0, ''), 100) AS reason,
       pp.id AS position_id, pp.execution_mode, pp.entry_at, pp.exit_at, pp.realized_pnl, pp.exit_reason,
       pp.status AS position_status,
       (SELECT count(*) FROM ml_signals s WHERE s.opportunity_id = oo.id) AS signals
FROM opportunity_outcomes oo
LEFT JOIN paper_positions pp ON pp.id = oo.position_id
WHERE oo.decided_at >= :since AND oo.engine LIKE 'solana%'
  AND (CAST(:engine AS text) IS NULL OR oo.engine = CAST(:engine AS text))
  AND (CAST(:traded AS boolean) IS NULL OR oo.traded = CAST(:traded AS boolean))
ORDER BY oo.decided_at DESC LIMIT :limit
"""


def _model_status(champion: ModelVersion | None, challenger: ModelVersion | None, report: dict | None) -> tuple[str, str]:
    if challenger is not None:
        st = (report or {}).get("status")
        if st == "PASS":
            return "AWAITING MY APPROVAL", (f"challenger v{challenger.version} passed its frozen validation; promotion is "
                                            "your decision on ML Review, never automatic")
        if st in (None, "INSUFFICIENT_DATA"):
            return "INSUFFICIENT DATA", f"challenger v{challenger.version}: not validated yet"
        return "VALIDATION FAILED", f"challenger v{challenger.version}: {(report or {}).get('reason') or st}"
    if champion is not None:
        return "CHAMPION IN USE", f"v{champion.version}, promoted by an operator"
    return "OBSERVING", "shadow or trained only: scored for review, never used for a decision"


async def _reports(session: AsyncSession) -> dict[tuple[str, int], dict]:
    from yonixalpha_core.db.models import MlValidationReport

    rows = (await session.execute(select(MlValidationReport).order_by(MlValidationReport.evaluated_at.desc())
                                  .limit(500))).scalars()
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        key = (r.model_name, int(r.model_version))
        if key not in out:
            out[key] = {"status": r.status, "reason": r.reason, "at": r.evaluated_at.isoformat()}
    return out


async def overview(session: AsyncSession, redis: Any = None, now: datetime | None = None, days: int = 7) -> dict[str, Any]:
    from yonixalpha_core.ml import governance, steps

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    contrib = await governance.load(session)
    reports = await _reports(session)
    by_name: dict[str, dict[str, ModelVersion]] = {}
    for m in (await session.execute(select(ModelVersion).where(ModelVersion.status.in_(("active", "challenger", "shadow")))
                                    .order_by(ModelVersion.name, ModelVersion.version.desc()))).scalars():
        by_name.setdefault(m.name, {}).setdefault(m.status, m)
    models = []
    for name, st in sorted(by_name.items()):
        champ, chal = st.get("active"), st.get("challenger")
        status, why = _model_status(champ, chal, reports.get((chal.name, chal.version)) if chal else None)
        c = contrib.get(name, governance.Contribution())
        shown = champ or chal or st.get("shadow")
        models.append({"name": name, "champion": f"v{champ.version}" if champ else None,
                       "challenger": f"v{chal.version}" if chal else None,
                       "shadow": f"v{st['shadow'].version}" if st.get("shadow") else None,
                       "trained_at": shown.trained_at.isoformat() if shown else None,
                       "training_samples": shown.training_sample_count if shown else None,
                       "stage": c.stage, "contribution_pct": c.percent, "status": status, "why": why})

    exps = (await session.execute(select(MlExperiment).order_by(MlExperiment.created_at.desc()).limit(50))).scalars().all()
    exp_rows, counts = [], {"DRAFT": 0, "ACCEPTED": 0, "REJECTED": 0, "RETIRED": 0}
    for e in exps:
        counts[e.status] = counts.get(e.status, 0) + 1
        if e.status == "DRAFT":
            status, why = "AWAITING MY APPROVAL", "proposed; nothing runs until you accept it"
        elif e.status == "REJECTED":
            status, why = "REJECTED", "rejected by an operator"
        else:
            rep = await signal_center.report(session, e.id)
            v = rep["verdict"]
            status = EXPERIMENT_STATUS.get(v["state"], v["state"])
            if v["state"] == "SUPPORTED_FORWARD":
                status = "CHALLENGER OUTPERFORMING BASELINE: AWAITING MY APPROVAL"
            if e.status == "RETIRED":
                status = f"RETIRED ({status})"
            why = v["reason"]
        exp_rows.append({"id": str(e.id), "idea": e.idea[:160], "experiment_status": e.status, "status": status, "why": why,
                         "created_at": e.created_at.isoformat(), "decided_at": e.decided_at.isoformat() if e.decided_at else None})

    sig_total = (await session.execute(select(func.count()).select_from(MlSignal))).scalar_one()
    sig_done = (await session.execute(select(func.count()).select_from(MlSignalOutcome))).scalar_one()
    sig_window = (await session.execute(select(func.count()).select_from(MlSignal).where(MlSignal.decided_at >= since))).scalar_one()

    trades = {}
    for engine in forward_test.ENGINES:
        r = await solana_performance.report(session, since=since, until=now, engine=engine, outcomes=False)
        o = (r["trades_by_mode"].get("PAPER") or {}).get("overall")
        trades[engine] = {"closed": o, "open": [x for x in r["open_positions"] if x["mode"] == "PAPER"]}
    sigs = await forward_test.signals(session, since, now)
    not_traded = [g for g in sigs if not g["traded"]]
    rejected = {"decisions": sum(g["count"] for g in not_traded), "measured": sum(g["measured"] for g in not_traded),
                "positive_after": sum(g["positive"] for g in not_traded),
                "top_reasons": sorted(not_traded, key=lambda g: -g["count"])[:8]}

    period = await forward_test.current(session)
    strategies = {}
    if period:
        ft = await forward_test.report(session, None, now, period)
        for engine, s in ft["strategies"].items():
            for mode, m in s["modes"].items():
                j = m["judgement"]
                strategies[f"{engine} {mode}"] = {"status": STRATEGY_STATUS.get(j["verdict"], j["verdict"]),
                                                  "verdict": j["verdict"], "gaps": j["evidence_gaps"], "edge": j["edge"],
                                                  "closed": j["closed_trades"], "baseline_closed": j["baseline_trades"]}

    audit_counts = dict((await session.execute(select(AuditLog.event_type, func.count()).where(
        AuditLog.event_type.in_(AUDIT_EVENTS)).group_by(AuditLog.event_type))).all())
    audit_recent = [{"at": a.created_at.isoformat(), "event": a.event_type,
                     "detail": {k: v for k, v in (a.detail or {}).items() if k in ("id", "idea", "name", "from", "to",
                                                                                  "stage", "percent", "version", "testable")}}
                    for a in (await session.execute(select(AuditLog).where(AuditLog.event_type.in_(AUDIT_EVENTS))
                                                    .order_by(AuditLog.created_at.desc()).limit(20))).scalars()]

    activity, sources = {}, None
    if redis is not None:
        from yonixalpha_core import scanner_health

        activity = await steps.read(redis)
        try:
            h = await scanner_health.report(session, redis, now)
            sources = {"overall": h.get("overall"),
                       "stages": [{"stage": x["stage"], "label": x["label"], "state": x["state"], "age_seconds": x["age_seconds"]}
                                  for x in h["stages"]]}
        except Exception as exc:  # noqa: BLE001 - shown as unknown, never hidden
            sources = {"overall": "UNKNOWN", "error": f"{type(exc).__name__}"}
    return {
        "at": now.isoformat(), "window_days": days, "models": models, "experiments": exp_rows, "experiment_counts": counts,
        "signals": {"total": sig_total, "evaluated": sig_done, "pending": sig_total - sig_done, "in_window": sig_window},
        "shadow_predictions": await prediction_audit.audit(session, since, now),
        "paper_trades": trades, "rejected": rejected, "forward_test": {"period": period and {
            k: period.get(k) for k in ("id", "label", "started_at")}, "strategies": strategies},
        "audit": {"counts": audit_counts, "recent": audit_recent},
        "activity": activity, "data_sources": sources,
        "notes": ["Predictions (signals, shadow scores) and executed paper positions are counted separately.",
                  "Nothing here trains, promotes or trades: a model becomes champion only by an operator's promotion, and "
                  "an experiment never becomes a strategy by itself.",
                  "RETROSPECTIVE shadow scores were written after their outcome or by a model trained later: counted, "
                  "never evaluated."],
    }


async def history(session: AsyncSession, *, engine: str | None = None, traded: bool | None = None, limit: int = 100,
                  hours: int = 48, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(text(HISTORY_SQL), {"since": now - timedelta(hours=hours), "engine": engine,
                                                       "traded": traded, "limit": max(1, min(limit, 500))})).all()
    out = []
    for r in rows:
        steps_ = [{"step": "decision", "at": r.decided_at.isoformat(), "text": f"{r.stage} {r.decision}"
                   + (f": {r.reason}" if r.reason else "")}]
        if r.signals:
            steps_.insert(0, {"step": "signal", "at": r.decided_at.isoformat(),
                              "text": f"{r.signals} experiment prediction(s) recorded"})
        if r.position_id is not None:
            steps_.append({"step": "entry", "at": r.entry_at.isoformat() if r.entry_at else None,
                           "text": f"{r.execution_mode} position opened"})
            if r.exit_at is not None:
                steps_.append({"step": "exit", "at": r.exit_at.isoformat(),
                               "text": f"{r.exit_reason}: net {float(r.realized_pnl):+.5f} SOL" if r.realized_pnl is not None
                               else str(r.exit_reason)})
        steps_.append({"step": "evaluation", "at": None,
                       "text": (f"executable return at T+5m {float(r.executable_return_pct):+.2f} %, peak "
                                f"{float(r.peak_pct or 0):+.1f} %" if r.executable_return_pct is not None
                                else f"outcome {r.status.lower()}: not measured yet")})
        out.append({"id": str(r.id), "decided_at": r.decided_at.isoformat(), "mint": r.mint, "symbol": r.symbol,
                    "engine": r.engine, "decision": r.decision, "traded": r.traded, "outcome_status": r.status,
                    "position_status": r.position_status, "steps": steps_})
    return out
