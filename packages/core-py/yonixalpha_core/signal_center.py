"""Signal Center: testable predictions and their measured outcomes.

1. generate   for every ACCEPTED experiment (ml_lab.propose), each new
              ledger decision of its categories whose DECISION-TIME features
              meet every entry condition becomes a signal: a timestamped,
              immutable prediction (ml_signals; a trigger refuses UPDATE and
              DELETE) holding the inputs used and the success definition.
              Only decisions made after the acceptance and at most
              FRESH_SECONDS old are signalled: a prediction is always recorded
              before its horizon has passed, never backfilled.
2. evaluate   once the ledger has the price path to the signal's horizon,
              the outcome is written separately (ml_signal_outcomes):
                direction_correct   price above the decision price at the horizon
                executable          the 0.05 SOL reference round trip existed
                executable_return_pct / net_profitable   after fees, impact,
                                    latency and fixed costs
                mfe_pct / mae_pct   best / worst move by the horizon
                first               TARGET / INVALIDATION / NEITHER / SAME_INTERVAL
                                    (which level was touched first, at the
                                    ledger's horizon resolution)
                would_fail          why a real trade could not have been made
3. report     per experiment: hit rate, executable availability, net
              expectancy, profit factor, drawdown, without the best signal,
              target-first rate, by category, chronological halves, and the
              comparison group (the same categories' decisions in the same
              period, or the gate's traded decisions) measured the same way.

A signal is a prediction, never an order: nothing here trades or changes a
strategy. Success needs the later half to beat the comparison after costs,
with PF > 1.3 and net > 0 without the single best signal; a correct
direction alone is never success.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MlExperiment, MlSignal, MlSignalOutcome, OpportunityOutcome
from yonixalpha_core.ml.opportunity_features import FEATURE_VERSION, features
from yonixalpha_core.ml_lab import CATEGORY, ENGINE_OF, describe

FRESH_SECONDS = 120  # a decision older than this when first seen is not signalled
HORIZON_SECONDS = {"T+60s": 60, "T+5m": 300, "T+15m": 900, "T+30m": 1800, "T+60m": 3600}
MIN_SIGNALS = 30
GENERATE_LIMIT = 2000
EVALUATE_LIMIT = 2000


def matches(conds: list[dict[str, Any]], x: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    """(all conditions hold, the inputs used, features that were missing)."""
    used, missing = {}, []
    ok = True
    for c in conds:
        v = x.get(c["feature"])
        used[c["feature"]] = v
        if v is None:
            missing.append(c["feature"])
            ok = False
            continue
        t, op = float(c["value"]), c["op"]
        if not ((op == ">=" and v >= t) or (op == "<=" and v <= t) or (op == ">" and v > t) or (op == "<" and v < t)):
            ok = False
    return ok and bool(conds), used, missing


async def generate(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    exps = (await session.execute(select(MlExperiment).where(MlExperiment.status == "ACCEPTED"))).scalars().all()
    out = {"experiments": len(exps), "checked": 0, "signals": 0, "missing_inputs": 0}
    for e in exps:
        prop = e.proposal or {}
        engines = [ENGINE_OF[c] for c in prop.get("categories", []) if c in ENGINE_OF]
        conds = prop.get("entry_conditions") or []
        if not engines or not conds or e.decided_at is None:
            continue
        since = max(e.decided_at, now - timedelta(seconds=FRESH_SECONDS))
        signalled = select(MlSignal.opportunity_id).where(MlSignal.experiment_id == e.id, MlSignal.opportunity_id.is_not(None),
                                                          MlSignal.decided_at >= since).scalar_subquery()
        rows = (await session.execute(select(OpportunityOutcome).where(
            OpportunityOutcome.decided_at > since, OpportunityOutcome.engine.in_(engines),
            OpportunityOutcome.id.not_in(signalled)).order_by(OpportunityOutcome.decided_at).limit(GENERATE_LIMIT))).scalars().all()
        ex = prop.get("exit") or {}
        horizon = ex.get("horizon") if ex.get("horizon") in HORIZON_SECONDS else "T+5m"
        for r in rows:
            out["checked"] += 1
            x = features(r.snapshot or {}, r.decided_at, r.engine, r.stage)
            ok, used, missing = matches(conds, x)
            if missing:
                out["missing_inputs"] += 1
            if not ok:
                continue
            res = await session.execute(insert(MlSignal).values(
                signal_key=f"exp:{e.id}:{r.id}", source="EXPERIMENT", experiment_id=e.id, opportunity_id=r.id, mint=r.mint,
                engine=r.engine, category=CATEGORY.get(r.engine, r.engine), decided_at=r.decided_at, horizon=horizon,
                prediction={"direction": "UP", "horizon": horizon, "target_pct": ex.get("target_pct", 20),
                            "invalidation_pct": ex.get("invalidation_pct", 15),
                            "decision_price_raw": (r.snapshot or {}).get("price_raw")},
                inputs={"features": used, "stage": r.stage, "decision": r.decision},
                definition={"success": f"executable return at {horizon} > 0 after costs",
                            "conditions": [describe(c) for c in conds], "experiment_proposal_hypothesis": prop.get("hypothesis")},
                feature_version=FEATURE_VERSION).on_conflict_do_nothing(index_elements=["signal_key"]))
            out["signals"] += res.rowcount or 0
    return out


def outcome_from_path(path: dict[str, Any], horizon: str, prediction: dict[str, Any]) -> dict[str, Any] | None:
    """The outcome at `horizon` from a ledger price path; None while the
    horizon has not been measured."""
    point = (path or {}).get(horizon)
    if not point:
        return None
    ex = point.get("executable")
    if ex is None and "unknown" not in point:
        return None  # point recorded, executable round trip not computed yet (ledger not final)
    ex = ex or {}
    target, inval = float(prediction.get("target_pct", 20)), float(prediction.get("invalidation_pct", 15))
    first = "NEITHER"
    for name, _ in sorted(HORIZON_SECONDS.items(), key=lambda kv: kv[1]):
        p = (path or {}).get(name) or {}
        up = p.get("peak_so_far_pct")
        down = p.get("drawdown_so_far_pct")
        hit_up = up is not None and float(up) >= target
        hit_dn = down is not None and float(down) <= -inval
        if hit_up and hit_dn:
            first = "SAME_INTERVAL"
            break
        if hit_up or hit_dn:
            first = "TARGET" if hit_up else "INVALIDATION"
            break
        if name == horizon:
            break
    ret = ex.get("executable_return_pct")
    change = point.get("change_pct")
    return {"direction_correct": (float(change) > 0) if change is not None else None,
            "change_pct": change, "executable": ret is not None, "executable_return_pct": ret,
            "net_profitable": (float(ret) > 0) if ret is not None else None,
            "mfe_pct": point.get("peak_so_far_pct"), "mae_pct": point.get("drawdown_so_far_pct"),
            "first": first, "would_fail": ex.get("unknown") or point.get("unknown")}


async def evaluate(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(
        select(MlSignal, OpportunityOutcome).join(OpportunityOutcome, OpportunityOutcome.id == MlSignal.opportunity_id)
        .outerjoin(MlSignalOutcome, MlSignalOutcome.signal_id == MlSignal.id)
        .where(MlSignalOutcome.signal_id.is_(None)).order_by(MlSignal.decided_at).limit(EVALUATE_LIMIT))).all()
    done = 0
    for sig, row in rows:
        if now < sig.decided_at + timedelta(seconds=HORIZON_SECONDS.get(sig.horizon, 300)):
            continue
        o = outcome_from_path(row.path or {}, sig.horizon, sig.prediction or {})
        if o is None:
            if row.status == "COMPLETE":  # the ledger finished without this horizon: record it as unmeasurable
                o = {"direction_correct": None, "executable": False, "executable_return_pct": None, "net_profitable": None,
                     "mfe_pct": None, "mae_pct": None, "first": "NEITHER", "would_fail": "horizon not measured by the ledger"}
            else:
                continue
        await session.execute(insert(MlSignalOutcome).values(signal_id=sig.id, evaluated_at=now, outcome=o)
                              .on_conflict_do_nothing(index_elements=["signal_id"]))
        done += 1
    return {"pending_checked": len(rows), "evaluated": done}


def metrics(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0}
    wins, losses = [x for x in rets if x > 0], [x for x in rets if x <= 0]
    eq = peak = mdd = 0.0
    for x in rets:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"n": len(rets), "mean": round(statistics.fmean(rets), 3), "median": round(statistics.median(rets), 3),
            "win_rate": round(len(wins) / len(rets), 3),
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
            "sum": round(sum(rets), 2), "sum_without_best": round(sum(rets) - max(rets), 2),
            "max_drawdown_pct_points": round(mdd, 2)}


def verdict(n: int, later: dict[str, Any], comp_later: dict[str, Any]) -> dict[str, str]:
    if n < MIN_SIGNALS:
        return {"state": "INSUFFICIENT_DATA", "reason": f"{n} evaluated signals (need {MIN_SIGNALS})"}
    why = []
    if not later.get("n"):
        return {"state": "INSUFFICIENT_DATA", "reason": "no executable return in the later half"}
    if comp_later.get("n") and later["mean"] <= comp_later["mean"]:
        why.append(f"later-half mean {later['mean']} % not above the comparison's {comp_later['mean']} %")
    if (later.get("profit_factor") or 0) <= 1.3:
        why.append(f"profit factor {later.get('profit_factor')} not above 1.3")
    if later["sum_without_best"] <= 0:
        why.append(f"net without the best signal {later['sum_without_best']} not above 0")
    return ({"state": "NOT_SUPPORTED", "reason": "; ".join(why)} if why else
            {"state": "SUPPORTED_FORWARD", "reason": "beat the comparison in the later half after costs; evidence for more "
                                                       "testing, not proof of future profit and never a live switch"})


async def report(session: AsyncSession, experiment_id: UUID) -> dict[str, Any]:
    e = await session.get(MlExperiment, experiment_id)
    if e is None:
        raise ValueError("experiment not found")
    prop = e.proposal or {}
    rows = (await session.execute(select(MlSignal, MlSignalOutcome).outerjoin(
        MlSignalOutcome, MlSignalOutcome.signal_id == MlSignal.id).where(MlSignal.experiment_id == e.id)
        .order_by(MlSignal.decided_at))).all()
    evaluated = [(s, o.outcome) for s, o in rows if o is not None]
    rets = [float(o["executable_return_pct"]) for _, o in evaluated if o.get("executable_return_pct") is not None]
    half = len(rets) // 2
    dir_known = [o["direction_correct"] for _, o in evaluated if o.get("direction_correct") is not None]
    firsts: dict[str, int] = {}
    by_cat: dict[str, list[float]] = {}
    for s, o in evaluated:
        firsts[o.get("first", "NEITHER")] = firsts.get(o.get("first", "NEITHER"), 0) + 1
        if o.get("executable_return_pct") is not None:
            by_cat.setdefault(s.category, []).append(float(o["executable_return_pct"]))
    # Comparison group, measured the same way from the ledger: the same
    # categories' decisions (or the gate's traded ones) since the acceptance.
    engines = [ENGINE_OF[c] for c in prop.get("categories", []) if c in ENGINE_OF]
    horizon = (prop.get("exit") or {}).get("horizon", "T+5m")
    comp: list[float] = []
    if e.decided_at is not None and engines:
        cond = [OpportunityOutcome.decided_at > e.decided_at, OpportunityOutcome.engine.in_(engines)]
        if "traded" in (prop.get("comparison") or ""):
            cond.append(OpportunityOutcome.traded.is_(True))
        for (path,) in (await session.execute(select(OpportunityOutcome.path).where(and_(*cond))
                                              .order_by(OpportunityOutcome.decided_at).limit(20000))).all():
            v = (((path or {}).get(horizon) or {}).get("executable") or {}).get("executable_return_pct")
            if v is not None:
                comp.append(float(v))
    later, comp_later = metrics(rets[half:]), metrics(comp[len(comp) // 2:])
    return {
        "experiment": {"id": str(e.id), "status": e.status, "idea": e.idea, "hypothesis": prop.get("hypothesis"),
                       "conditions": prop.get("plain_language"), "categories": prop.get("categories"), "horizon": horizon,
                       "accepted_at": e.decided_at.isoformat() if e.decided_at else None},
        "signals": len(rows), "evaluated": len(evaluated),
        "direction_hit_rate": round(sum(dir_known) / len(dir_known), 3) if dir_known else None,
        "executable_rate": round(len(rets) / len(evaluated), 3) if evaluated else None,
        "all": metrics(rets), "earlier_half": metrics(rets[:half]), "later_half": later,
        "first_touched": firsts, "by_category": {k: metrics(v) for k, v in by_cat.items()},
        "median_mfe_pct": _median([o.get("mfe_pct") for _, o in evaluated]),
        "median_mae_pct": _median([o.get("mae_pct") for _, o in evaluated]),
        "would_fail": sum(1 for _, o in evaluated if o.get("would_fail")),
        "comparison": {"group": prop.get("comparison"), "all": metrics(comp), "later_half": comp_later},
        "verdict": verdict(len(evaluated), later, comp_later),
        "recent": [{"id": str(s.id), "mint": s.mint, "category": s.category, "decided_at": s.decided_at.isoformat(),
                    "horizon": s.horizon, "inputs": s.inputs, "outcome": o.outcome if o else None}
                   for s, o in rows[-30:][::-1]],
    }


def _median(values: list[Any]) -> float | None:
    v = [float(x) for x in values if x is not None]
    return round(statistics.median(v), 3) if v else None
