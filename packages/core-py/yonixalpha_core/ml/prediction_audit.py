"""Shadow-model predictions against what happened, counting only honest
predictions (seven-day investigation, 2026-10-24).

The ml service scores ledger rows (opportunity_outcomes.ml_shadow) after
training, up to SCORE_LOOKBACK after the decision. A stored score is
therefore one of:

  PROSPECTIVE    the model was trained before the decision AND the score was
                 written before the primary horizon (T+5m) ended: a genuine
                 prediction made before the outcome was known
  RETROSPECTIVE  scored after the horizon, or by a model trained after the
                 decision (the row may even be in its training data): never
                 evidence of predictive value, reported as a count only

For PROSPECTIVE scores of the executable-return model (E_EXECUTABLE_RETURN)
whose outcome the ledger has measured, the audit compares the decisions the
model favoured (prediction > 0) with the rest and with all of them: the
mean executable return of a 0.05 SOL reference trade after costs. Read-only.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ModelVersion

HORIZON = timedelta(minutes=5)  # the ledger's primary horizon (executable_return_pct)
RETURN_TARGET = "E_EXECUTABLE_RETURN"
ROWS_SQL = """
SELECT engine, decided_at, status, executable_return_pct AS outcome, ml_shadow->>'scored_at' AS scored_at,
       ml_shadow->'scores' AS scores
FROM opportunity_outcomes
WHERE decided_at >= :since AND decided_at < :until AND ml_shadow IS NOT NULL
ORDER BY decided_at LIMIT 50000
"""


def classify(decided_at: datetime, scored_at: datetime | None, trained_at: datetime | None) -> str:
    if scored_at is None or trained_at is None:
        return "RETROSPECTIVE"
    return "PROSPECTIVE" if trained_at < decided_at and scored_at <= decided_at + HORIZON else "RETROSPECTIVE"


def _mean(v: list[float]) -> float | None:
    return round(statistics.fmean(v), 3) if v else None


async def audit(session: AsyncSession, since: datetime, until: datetime) -> dict[str, Any]:
    trained = {(n, int(v)): t for n, v, t in (await session.execute(
        select(ModelVersion.name, ModelVersion.version, ModelVersion.trained_at))).all()}
    per_model: dict[str, dict[str, int]] = {}
    favoured: list[float] = []
    rest: list[float] = []
    rows = (await session.execute(text(ROWS_SQL), {"since": since, "until": until})).all()
    for r in rows:
        try:
            scored = datetime.fromisoformat(r.scored_at) if r.scored_at else None
        except ValueError:
            scored = None
        for target, s in (r.scores or {}).items():
            if not isinstance(s, dict):
                continue
            key = f"{s.get('model')} v{s.get('version')}"
            kind = classify(r.decided_at, scored, trained.get((s.get("model"), int(s.get("version") or 0))))
            c = per_model.setdefault(key, {"target": target, "scored": 0, "PROSPECTIVE": 0, "RETROSPECTIVE": 0,
                                           "prospective_with_outcome": 0})
            c["scored"] += 1
            c[kind] += 1
            if kind == "PROSPECTIVE" and r.outcome is not None:
                c["prospective_with_outcome"] += 1
                if target == RETURN_TARGET and s.get("value") is not None:
                    (favoured if float(s["value"]) > 0 else rest).append(float(r.outcome))
    allv = favoured + rest
    return {
        "since": since.isoformat(), "until": until.isoformat(), "rows_scored": len(rows), "models": per_model,
        "return_model_prospective": {
            "evaluated": len(allv),
            "favoured": {"n": len(favoured), "mean_executable_return_pct": _mean(favoured)},
            "not_favoured": {"n": len(rest), "mean_executable_return_pct": _mean(rest)},
            "all": {"n": len(allv), "mean_executable_return_pct": _mean(allv)},
            "note": "fewer than 30 evaluated predictions: anecdotal" if len(allv) < 30 else
                    "a difference here is one period's evidence, not proof",
        },
        "definitions": {"PROSPECTIVE": "model trained before the decision and score written before T+5m",
                        "RETROSPECTIVE": "scored after T+5m or by a model trained after the decision: counted, "
                                         "never evaluated"},
    }
