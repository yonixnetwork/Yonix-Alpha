"""ML contribution governance (master §38-42).

Every model has a stage and a contribution percentage:

    OBSERVATION_ONLY  trained and validated on frozen sets, but not scored
                      onto new samples or decisions (no shadow verdicts)
    SHADOW            scored and compared, never read by a decision
    PAPER_CONTRIBUTOR its consumer may use it in PAPER decisions, at the
                      configured percentage
    LIVE_CONTRIBUTOR  locked: live use needs live trading authorization,
                      which this system does not grant (EVM live execution
                      is off; Solana live stays rules only for ML)

Contribution starts at 0 %. It can be raised only by an operator (audited),
STEP_PCT at a time, at most every MIN_DAYS_BETWEEN_INCREASES days, up to
MAX_PCT, and only while the model's latest frozen-validation report
(ml.validation) is PASS and it is the operator-promoted champion. Lowering
it, or setting 0 %, is always allowed at once. Nothing here raises a
contribution automatically, and a winning streak is not an input
(§40: "must NOT automatically increase contribution merely because a short
sequence of trades won").

Which models have a consumer for a percentage:

    solana_candidate_momentum  decision-engine: confidence =
                               (1 - w) * rules + w * model, w = percent / 100

The safety-gate models (gate_*) contribute only through the gate's own
min_ml_confidence (a WAIT, never an approval): shown here, governed there.
The EVM, wallet, exit and Solana opportunity models (shadow_*) have no
decision consumer: they stay SHADOW.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

KEY = "ml_contribution"
STAGES = ("OBSERVATION_ONLY", "SHADOW", "PAPER_CONTRIBUTOR", "LIVE_CONTRIBUTOR")
STEP_PCT = 5
MAX_PCT = 25
MIN_DAYS_BETWEEN_INCREASES = 7

CONTRIBUTABLE: dict[str, dict[str, Any]] = {
    "solana_candidate_momentum": {
        "family": "solana_candidate",
        "consumer": "decision-engine: blended into the candidate confidence, weight = contribution %",
        "max_stage": "PAPER_CONTRIBUTOR",
    },
}
LIVE_LOCKED = ("LIVE_CONTRIBUTOR is locked: live contribution needs live trading authorization for ML, which is not "
               "granted")


@dataclass
class Contribution:
    stage: str = "SHADOW"
    percent: int = 0
    changed_at: str | None = None
    changed_by: str | None = None
    raised_at: str | None = None  # last increase (the 7-day rule)

    def weight(self) -> float:
        """The weight a PAPER consumer may give the model (0.0 .. MAX_PCT/100)."""
        if self.stage != "PAPER_CONTRIBUTOR":
            return 0.0
        return max(0, min(self.percent, MAX_PCT)) / 100

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "percent": self.percent, "changed_at": self.changed_at,
                "changed_by": self.changed_by, "raised_at": self.raised_at}


def parse(value: dict | None) -> dict[str, Contribution]:
    out: dict[str, Contribution] = {}
    for name, v in ((value or {}).get("models") or {}).items():
        if not isinstance(v, dict):
            continue
        stage = v.get("stage") if v.get("stage") in STAGES else "SHADOW"
        try:
            pct = int(v.get("percent") or 0)
        except (TypeError, ValueError):
            pct = 0
        out[name] = Contribution(stage, max(0, min(pct, MAX_PCT)), v.get("changed_at"), v.get("changed_by"),
                                 v.get("raised_at"))
    return out


async def load(session) -> dict[str, Contribution]:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, KEY)
    return parse(dict(row.value) if row else None)


async def observation_only(session) -> set[str]:
    """Models an operator set to OBSERVATION_ONLY: scorers skip them."""
    return {n for n, c in (await load(session)).items() if c.stage == "OBSERVATION_ONLY"}


async def weight_for(session, name: str) -> float:
    """What a consumer reads: 0.0 unless an operator set a PAPER contribution."""
    return (await load(session)).get(name, Contribution()).weight()


def check_change(name: str, current: Contribution, stage: str, percent: int, *, champion: bool,
                 validation: dict | None, now: datetime) -> list[str]:
    """Reasons the change is refused (empty: allowed). validation: the
    model's latest frozen-validation report {"status", "evaluated_at", ...}."""
    errors: list[str] = []
    spec = CONTRIBUTABLE.get(name)
    if stage not in STAGES:
        errors.append(f"stage must be one of {', '.join(STAGES)}")
        return errors
    if name.startswith("gate_"):
        errors.append(f"{name} is a safety-gate model: it is governed by promotion (ML Review) and the engine's "
                      "min_ml_confidence (Risk settings), and can only make a decision WAIT")
        return errors
    if spec is None:
        if stage not in ("OBSERVATION_ONLY", "SHADOW") or percent:
            errors.append(f"{name} has no decision consumer: it can only be OBSERVATION_ONLY or SHADOW at 0 %")
        return errors
    if stage == "LIVE_CONTRIBUTOR":
        errors.append(LIVE_LOCKED)
    if percent < 0 or percent > MAX_PCT or percent % STEP_PCT:
        errors.append(f"percent must be a multiple of {STEP_PCT} between 0 and {MAX_PCT}")
    if stage != "PAPER_CONTRIBUTOR" and percent:
        errors.append("a percentage is used only at PAPER_CONTRIBUTOR; other stages are 0 %")
    raising = percent > current.weight() * 100 or (stage == "PAPER_CONTRIBUTOR" and current.stage != "PAPER_CONTRIBUTOR"
                                                    and percent > 0)
    if raising:
        if percent - int(current.weight() * 100) > STEP_PCT:
            errors.append(f"raise {STEP_PCT} % at a time (now {int(current.weight() * 100)} %)")
        if current.raised_at:
            last = datetime.fromisoformat(current.raised_at)
            if now - last < timedelta(days=MIN_DAYS_BETWEEN_INCREASES):
                errors.append(f"last raised {last.date()}: wait {MIN_DAYS_BETWEEN_INCREASES} days between increases")
        if not champion:
            errors.append("no operator-promoted champion: promote a validated challenger first (ML Review)")
        if not validation or validation.get("status") != "PASS":
            errors.append("the latest frozen-validation report is not PASS "
                          f"({(validation or {}).get('status') or 'none yet'})")
    return errors


def apply_change(current: Contribution, stage: str, percent: int, by: str, now: datetime) -> Contribution:
    raised = percent > int(current.weight() * 100)
    return Contribution(stage, percent if stage == "PAPER_CONTRIBUTOR" else 0, now.isoformat(), by,
                        now.isoformat() if raised else current.raised_at)


def health(*, validation: dict | None, drift: dict | None = None) -> tuple[str, str]:
    """(OK | DEGRADED | DRIFT | NOT_VALIDATED, why) from the latest frozen
    validation and, for gate models, the drift check."""
    if drift and (drift.get("status") == "MODEL_DRIFT_DETECTED"):
        return "DRIFT", "feature or accuracy drift detected"
    if not validation:
        return "NOT_VALIDATED", "no frozen validation report yet"
    st = validation.get("status")
    if st == "PASS":
        return "OK", "passed the frozen validation set"
    if st == "INSUFFICIENT_DATA":
        return "NOT_VALIDATED", validation.get("reason") or "too few labelled samples in the frozen set"
    return "DEGRADED", validation.get("reason") or "failed the frozen validation set"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
