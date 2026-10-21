"""ML Training Lab: the operator's own training input, kept separate from
what the market verifiably did.

  queue      tokens the system observed or decided on recently (opportunity
             ledger: fresh observations, gate decisions, momentum WATCH rows)
             with their category, data quality and what happened since
  timeline   one observation: the decision-time snapshot, the price path to
             T+60 m (executable returns per horizon) and the labels given
  labels     manual labels (LABELS): an opinion with the decision-time
             features copied at labelling time. `verify` compares the opinion
             with the recorded outcome when read; a label is never proof that
             a trade was profitable and is never rewritten by the outcome
  dataset    decision-time features + recorded outcomes + manual labels for
             export, deduplicated by ledger key, with the missing fields
             counted; outcomes are only ever columns, never features
  propose    turn a plain-language idea into a proposed experiment (shown to
             the operator before anything runs; see signal_center)

Nothing here trades, changes a mode or touches the gate.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import MlManualLabel, OpportunityOutcome
from yonixalpha_core.ml.opportunity_features import FEATURE_NAMES, FEATURE_VERSION, NUMERIC, features

CATEGORY = {"solana_fresh": "FRESH", "solana_migration": "MIGRATED", "solana_momentum": "MOMENTUM"}
ENGINE_OF = {v: k for k, v in CATEGORY.items()}

# label -> (plain words, how the recorded outcome verifies it, or None: opinion only)
LABELS: dict[str, tuple[str, str | None]] = {
    "GOOD_EARLY_ENTRY": ("Good early entry", "executable return at T+5m > 0"),
    "ENTRY_TOO_LATE": ("Entry was too late", "peak within 30 min below +10 % and executable return <= 0"),
    "MOMENTUM_CONFIRMED": ("Momentum confirmed", "peak within 30 min >= +30 %"),
    "MOMENTUM_FAILED": ("Momentum failed", "peak within 30 min below +10 %"),
    "LIQUIDITY_INSUFFICIENT": ("Liquidity was insufficient", None),
    "BUYING_WEAK": ("Buying pressure was weak", None),
    "SELLING_INCREASING": ("Selling pressure was increasing", None),
    "QUALIFIED_WALLETS_SUPPORTED": ("Qualified wallets supported the setup", None),
    "WALLET_SIGNAL_LATE": ("Wallet signal was too late", None),
    "EXCESSIVE_EXTENSION": ("Excessive price extension", None),
    "UNSAFE_TOKEN": ("Unsafe token", "drawdown within 30 min <= -50 %"),
    "SUCCESSFUL_EXIT": ("Successful exit", "post-exit classification GOOD_EXIT or RISK_CORRECT"),
    "FAILED_EXIT": ("Failed exit", "post-exit classification POSSIBLY_EARLY or POSSIBLY_LATE"),
    "NO_TRADE_CORRECT": ("No-trade was correct", "not traded and executable return at T+5m <= 0"),
    "NO_TRADE_MISSED": ("No-trade missed a valid opportunity", "not traded and executable return at T+5m > 0"),
}
MAX_NOTES = 2000
QUEUE_LIMIT = 200


class LabError(ValueError):
    pass


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def verify(label: str, row: OpportunityOutcome | None) -> dict[str, str]:
    """The recorded outcome next to the opinion: AGREES / DISAGREES / PENDING
    (outcome not recorded yet) / OPINION_ONLY (no outcome measures it)."""
    rule = LABELS.get(label, ("", None))[1]
    if rule is None:
        return {"status": "OPINION_ONLY", "evidence": "no recorded outcome measures this label; compare it in the dataset"}
    if row is None:
        return {"status": "PENDING", "evidence": "the observation is no longer in the ledger"}
    ex, peak, dd = _f(row.executable_return_pct), _f(row.peak_pct), _f(row.drawdown_pct)
    post = (row.post_exit or {}).get("classification")
    checks = {
        "GOOD_EARLY_ENTRY": (ex, lambda: ex > 0),
        "ENTRY_TOO_LATE": (ex if peak is not None else None, lambda: peak < 10 and ex <= 0),
        "MOMENTUM_CONFIRMED": (peak, lambda: peak >= 30),
        "MOMENTUM_FAILED": (peak, lambda: peak < 10),
        "UNSAFE_TOKEN": (dd, lambda: dd <= -50),
        "SUCCESSFUL_EXIT": (post, lambda: post in ("GOOD_EXIT", "RISK_CORRECT")),
        "FAILED_EXIT": (post, lambda: post in ("POSSIBLY_EARLY", "POSSIBLY_LATE")),
        "NO_TRADE_CORRECT": (ex, lambda: (not row.traded) and ex <= 0),
        "NO_TRADE_MISSED": (ex, lambda: (not row.traded) and ex > 0),
    }
    value, test = checks[label]
    if value is None:
        return {"status": "PENDING", "evidence": f"outcome not recorded yet ({rule})"}
    ok = test()
    facts = f"executable T+5m {ex}%, peak {peak}%, drawdown {dd}%, traded {row.traded}" + (f", post-exit {post}" if post else "")
    return {"status": "AGREES" if ok else "DISAGREES", "evidence": f"{rule}: {facts}"}


def _quality(row: OpportunityOutcome) -> dict[str, Any]:
    x = features(row.snapshot or {}, row.decided_at, row.engine, row.stage)
    missing = [n for n in NUMERIC if x.get(n) is None]
    return {"features_present": len(NUMERIC) - len(missing), "features_total": len(NUMERIC), "missing": missing[:40],
            "price_known": bool((row.snapshot or {}).get("price_raw"))}


def _brief(row: OpportunityOutcome, label_count: int = 0) -> dict[str, Any]:
    return {"id": str(row.id), "mint": row.mint, "symbol": row.symbol, "engine": row.engine,
            "category": CATEGORY.get(row.engine, row.engine), "stage": row.stage, "decision": row.decision,
            "traded": row.traded, "status": row.status, "decided_at": row.decided_at.isoformat(),
            "peak_pct": _f(row.peak_pct), "drawdown_pct": _f(row.drawdown_pct),
            "executable_return_pct": _f(row.executable_return_pct), "labels": label_count}


async def queue(session: AsyncSession, *, hours: float = 6, category: str | None = None, stage: str | None = None,
                limit: int = 100, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    q = select(OpportunityOutcome).where(OpportunityOutcome.decided_at >= now - timedelta(hours=hours))
    if category:
        if category not in ENGINE_OF:
            raise LabError(f"category must be one of {sorted(ENGINE_OF)}")
        q = q.where(OpportunityOutcome.engine == ENGINE_OF[category])
    if stage:
        q = q.where(OpportunityOutcome.stage == stage)
    rows = (await session.execute(q.order_by(OpportunityOutcome.decided_at.desc()).limit(min(limit, QUEUE_LIMIT)))).scalars().all()
    counts: dict[Any, int] = {}
    if rows:
        counts = dict((await session.execute(select(MlManualLabel.opportunity_id, func.count()).where(
            MlManualLabel.opportunity_id.in_([r.id for r in rows])).group_by(MlManualLabel.opportunity_id))).all())
    out = []
    for r in rows:
        b = _brief(r, counts.get(r.id, 0))
        b["data_quality"] = {k: v for k, v in _quality(r).items() if k != "missing"}
        out.append(b)
    return {"at": now.isoformat(), "hours": hours, "rows": out,
            "note": "observed whether or not anything was traded; observation never authorizes a trade"}


async def _row(session: AsyncSession, opp_id: UUID) -> OpportunityOutcome:
    row = await session.get(OpportunityOutcome, opp_id)
    if row is None:
        raise LabError("observation not found")
    return row


async def timeline(session: AsyncSession, opp_id: UUID) -> dict[str, Any]:
    row = await _row(session, opp_id)
    snap = row.snapshot or {}
    labs = (await session.execute(select(MlManualLabel).where(MlManualLabel.opportunity_id == row.id)
                                  .order_by(MlManualLabel.created_at))).scalars().all()
    path = []
    for name, point in (row.path or {}).items():
        ex = (point or {}).get("executable") or {}
        path.append({"horizon": name, "at": point.get("at"), "change_pct": point.get("change_pct"),
                     "peak_so_far_pct": point.get("peak_so_far_pct"), "drawdown_so_far_pct": point.get("drawdown_so_far_pct"),
                     "executable_return_pct": ex.get("executable_return_pct"), "unknown": point.get("unknown") or ex.get("unknown")})
    return {**_brief(row, len(labs)), "reasons": row.reasons, "data_quality": _quality(row),
            "decision_time": {k: snap.get(k) for k in ("price_raw", "market_cap_sol", "liquidity_sol", "curve_progress",
                                                         "age_seconds", "signal_qualified", "signal_strength", "creator_sold")},
            "features": features(snap, row.decided_at, row.engine, row.stage), "feature_version": FEATURE_VERSION,
            "path": path, "post_exit": row.post_exit, "trade_result": row.trade_result,
            "labels": [label_out(lab, row) for lab in labs]}


def label_out(lab: MlManualLabel, row: OpportunityOutcome | None) -> dict[str, Any]:
    return {"id": str(lab.id), "label": lab.label, "text": LABELS.get(lab.label, (lab.label,))[0],
            "value": _f(lab.value), "notes": lab.notes, "source": lab.source, "created_by": lab.created_by,
            "created_at": lab.created_at.isoformat() if lab.created_at else None,
            "decided_at": lab.decided_at.isoformat() if lab.decided_at else None,
            "feature_version": lab.feature_version, "model_version": lab.model_version,
            "verification": verify(lab.label, row)}


async def add_label(session: AsyncSession, opp_id: UUID, label: str, *, value: float | None = None,
                    notes: str | None = None, user: str | None = None) -> MlManualLabel:
    if label not in LABELS:
        raise LabError(f"unknown label; one of {sorted(LABELS)}")
    if notes is not None and len(notes) > MAX_NOTES:
        raise LabError(f"notes are limited to {MAX_NOTES} characters")
    row = await _row(session, opp_id)
    exists = (await session.execute(select(MlManualLabel.id).where(
        MlManualLabel.opportunity_id == row.id, MlManualLabel.label == label, MlManualLabel.created_by == user))).first()
    if exists:
        raise LabError("you already gave this label to this observation")
    shadow = row.ml_shadow or {}
    model_version = ",".join(sorted({f"{s.get('model')}:{s.get('version')}" for s in (shadow.get("scores") or {}).values()
                                     if isinstance(s, dict)})) or None
    lab = MlManualLabel(opportunity_id=row.id, mint=row.mint, engine=row.engine, label=label,
                        value=Decimal(str(value)) if value is not None else None, notes=notes, decided_at=row.decided_at,
                        features=features(row.snapshot or {}, row.decided_at, row.engine, row.stage),
                        feature_version=FEATURE_VERSION, model_version=(model_version or "")[:64] or None,
                        source="MANUAL", created_by=user)
    session.add(lab)
    await session.flush()
    return lab


async def label_summary(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(MlManualLabel, OpportunityOutcome).outerjoin(
        OpportunityOutcome, OpportunityOutcome.id == MlManualLabel.opportunity_id)
        .order_by(MlManualLabel.created_at.desc()).limit(5000))).all()
    by: dict[str, dict[str, int]] = {}
    for lab, row in rows:
        st = verify(lab.label, row)["status"]
        by.setdefault(lab.label, {"AGREES": 0, "DISAGREES": 0, "PENDING": 0, "OPINION_ONLY": 0})[st] += 1
    return {"labels": {k: {"text": LABELS[k][0], "verified_by": LABELS[k][1], **by.get(k, {})} for k in LABELS},
            "recent": [label_out(lab, row) for lab, row in rows[:50]],
            "note": "a manual label is an opinion; AGREES / DISAGREES compares it with the recorded outcome"}


DATASET_COLUMNS = ("key", "id", "mint", "engine", "category", "stage", "decision", "traded", "decided_at")
OUTCOME_COLUMNS = ("executable_return_pct", "theoretical_return_pct", "peak_pct", "drawdown_pct", "status")


async def dataset(session: AsyncSession, *, days: int = 7, category: str | None = None, limit: int = 20000,
                  now: datetime | None = None) -> dict[str, Any]:
    """Decision-time features (as of the decision; opportunity_features
    drops anything stamped later) + recorded outcomes as separate columns +
    manual labels. One row per ledger key. Winners, losers, rejected and
    untraded decisions alike."""
    now = now or datetime.now(timezone.utc)
    q = select(OpportunityOutcome).where(OpportunityOutcome.decided_at >= now - timedelta(days=days))
    if category:
        q = q.where(OpportunityOutcome.engine == ENGINE_OF.get(category, category))
    rows = (await session.execute(q.order_by(OpportunityOutcome.decided_at).limit(limit))).scalars().all()
    labs: dict[Any, list[str]] = {}
    if rows:
        for oid, lab in (await session.execute(select(MlManualLabel.opportunity_id, MlManualLabel.label).where(
                MlManualLabel.opportunity_id.in_([r.id for r in rows])))).all():
            labs.setdefault(oid, []).append(lab)
    seen: set[str] = set()
    out, missing = [], {n: 0 for n in NUMERIC}
    duplicates = 0
    for r in rows:
        if r.key in seen:
            duplicates += 1
            continue
        seen.add(r.key)
        x = features(r.snapshot or {}, r.decided_at, r.engine, r.stage)
        for n in NUMERIC:
            if x.get(n) is None:
                missing[n] += 1
        out.append({"key": r.key, "id": str(r.id), "mint": r.mint, "engine": r.engine, "category": CATEGORY.get(r.engine, r.engine),
                    "stage": r.stage, "decision": r.decision, "traded": r.traded, "decided_at": r.decided_at.isoformat(),
                    "features": {n: x.get(n) for n in FEATURE_NAMES},
                    "outcome": {"executable_return_pct": _f(r.executable_return_pct),
                                "theoretical_return_pct": _f(r.theoretical_return_pct), "peak_pct": _f(r.peak_pct),
                                "drawdown_pct": _f(r.drawdown_pct), "status": r.status},
                    "manual_labels": sorted(labs.get(r.id, []))})
    return {"rows": out, "count": len(out), "duplicates_dropped": duplicates, "feature_version": FEATURE_VERSION,
            "missing_by_feature": {k: v for k, v in sorted(missing.items(), key=lambda kv: -kv[1]) if v},
            "incomplete_outcomes": sum(1 for o in out if o["outcome"]["status"] != "COMPLETE"),
            "leakage_rule": "features are computed from the decision-time snapshot only; outcomes are separate columns "
                            "measured after the decision and must never be used as features",
            "truncated": len(rows) >= limit}


def dataset_csv(data: dict[str, Any]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(DATASET_COLUMNS) + list(FEATURE_NAMES) + [f"outcome_{c}" for c in OUTCOME_COLUMNS] + ["manual_labels"])
    for r in data["rows"]:
        w.writerow([r[c] for c in DATASET_COLUMNS] + [r["features"].get(n) for n in FEATURE_NAMES]
                   + [r["outcome"].get(c) for c in OUTCOME_COLUMNS] + [";".join(r["manual_labels"])])
    return buf.getvalue()


# --- idea -> proposed experiment -----------------------------------------------------------------------

OPS = (">=", "<=", ">", "<")
# phrase pattern -> (feature, op, default value, plain words)
TEMPLATES: tuple[tuple[str, str, str, float, str], ...] = (
    (r"sustained buying|buying (?:volume|pressure)|buy pressure|net buying",
     "now_buy_sell_ratio", ">=", 1.5, "buys outnumber sells at least 1.5 to 1"),
    (r"sustained buying|buying volume|net buying|inflow", "now_net_flow_sol", ">", 0.0, "more SOL bought than sold"),
    (r"liquidity", "liquidity_sol", ">=", 20.0, "liquidity of at least {v} SOL"),
    (r"independent buyers?", "independent_buyers", ">=", 10.0, "at least {v} independent buyers"),
    (r"qualified wallets?|smart (?:wallets?|money)|proven wallets?", "smart_proven_wallets", ">=", 1.0,
     "at least {v} proven (qualified) wallet bought"),
    (r"(?:before|without|no) (?:a )?(?:large |big )?(?:price )?extension|not (?:yet )?extended|early entry",
     "mom_return_5m", "<=", 0.5, "price up no more than {pct} over the last 5 minutes"),
    (r"volume acceleration|accelerating volume|rising volume", "mom_volume_acceleration", ">=", 1.5,
     "volume in the last window at least {v}x the window before"),
    (r"buyer acceleration|more buyers|accelerating buyers", "mom_buyer_acceleration", ">=", 1.5,
     "buyers in the last window at least {v}x the window before"),
    (r"holder concentration|top holders?|top ?10", "top10_share", "<=", 0.3, "top 10 holders own at most {pct}"),
    (r"creator|dev(?:eloper)? (?:holding|share)", "creator_share", "<=", 0.05, "creator holds at most {pct}"),
    (r"organic|not manufactured|no wash", "manufactured_pump_score", "<=", 0.5, "manufactured-pump score at most {v}"),
)
WORDS = {feat: (op, words) for _, feat, op, _, words in reversed(TEMPLATES)}  # feature -> (op, plain words)
UNTESTABLE = (r"twitter|\bx\.com\b|tweet|telegram|discord|sentiment|news|influencer|\bkol\b|celebrity|insider")
DEFAULT_HORIZON = {"FRESH": "T+5m", "MIGRATED": "T+15m", "MOMENTUM": "T+15m"}
HORIZON_WORDS = ((r"\b60 ?min|\b1 ?hour|\bone hour", "T+60m"), (r"\b30 ?min", "T+30m"), (r"\b15 ?min", "T+15m"),
                 (r"\b5 ?min", "T+5m"), (r"\b1 ?min|\b60 ?s", "T+60s"))
HORIZONS = ("T+60s", "T+5m", "T+15m", "T+30m", "T+60m")


def _categories(text: str) -> list[str]:
    cats = []
    if re.search(r"migrat|pumpswap|graduat", text):
        cats.append("MIGRATED")
    if re.search(r"\bfresh\b|new(?:ly)? launch|just launched|launches", text):
        cats.append("FRESH")
    if re.search(r"momentum|established", text) and not cats:
        cats.append("MOMENTUM")
    return cats or ["FRESH", "MIGRATED", "MOMENTUM"]


COUNTED = ("independent_buyers", "smart_proven_wallets")  # "3 qualified wallets": the number may come first


def _number_near(text: str, pattern: str, before: bool = False) -> float | None:
    # "liquidity above 30 sol" (or, for counted things, "3 qualified wallets"); never across "and", "with" or a comma, so
    # a number belonging to the next phrase is not taken
    sep = r"(?:(?!\band\b|\bwith\b|\bbut\b)[^0-9.,;])"
    m = (re.search(rf"(?:{pattern}){sep}{{0,24}}?(\d+(?:\.\d+)?)\s*(%|sol|x)?", text)
         or (before and re.search(rf"(\d+(?:\.\d+)?)()\s+(?:[a-z]+\s+)?(?:{pattern})", text)))
    if not m:
        return None
    v = float(m.group(1))
    return v / 100 if m.group(2) == "%" else v


def describe(c: dict[str, Any]) -> str:
    v = c["value"]
    words = c.get("words") or f"{c['feature']} {c['op']} {v}"
    return words.replace("{v}", f"{v:g}").replace("{pct}", f"{v * 100:g} %")


def hypothesis(cats: list[str], conds: list[dict[str, Any]], horizon: str, compare: str) -> str:
    return (f"For {', '.join(c.lower() for c in cats)} tokens, entering when "
            + ("; ".join(describe(c) for c in conds) if conds else "(no condition yet)")
            + f" produces a better net executable return at {horizon} than {compare}.")


def propose(idea: str) -> dict[str, Any]:
    """A proposed experiment from plain language: explicit, editable
    conditions on decision-time features only. Testable only when every
    part of the idea maps to data the system records."""
    text = " ".join((idea or "").lower().split())
    if len(text) < 10:
        raise LabError("describe the idea in a sentence or more")
    cats = _categories(text)
    conds: list[dict[str, Any]] = []
    for pattern, feat, op, default, words in TEMPLATES:
        if re.search(pattern, text) and all(c["feature"] != feat for c in conds):
            found = _number_near(text, pattern, before=feat in COUNTED)
            conds.append({"feature": feat, "op": op, "value": found if found is not None else default, "words": words})
    untestable = sorted(set(re.findall(UNTESTABLE, text)))
    horizon = next((h for p, h in HORIZON_WORDS if re.search(p, text)), DEFAULT_HORIZON[cats[0]])
    target = _number_near(text, r"target|take profit|gain") or 0.2
    stop = _number_near(text, r"stop|invalidat|loss") or 0.15
    reasons = []
    if untestable:
        reasons.append(f"the idea depends on data the system does not record: {', '.join(untestable)}")
    if not conds:
        reasons.append("no condition could be mapped to a recorded feature; add conditions before accepting")
    compare = ("the existing confirmation strategy (decisions the gate traded)" if re.search(r"existing|current|confirmation", text)
               else "every decision of the same category in the same period")
    prop = {
        "hypothesis": hypothesis(cats, conds, horizon, compare),
        "categories": cats,
        "entry_conditions": conds,
        "rejection_conditions": ["any condition's feature missing at the decision: no signal (never guessed)",
                                 "only decisions made after the experiment is accepted (no backfill)",
                                 "the gate's hard safety findings are recorded; the experiment never overrides them"],
        "exit": {"horizon": horizon, "target_pct": target * 100, "invalidation_pct": stop * 100},
        "risk_limits": "predictions only: no order, no position, no change to any strategy; returns are those of a "
                       "0.05 SOL reference trade after fees, curve impact, latency and fixed costs",
        "required_data": sorted({c["feature"] for c in conds}) + ["opportunity ledger: price path and executable return"],
        "evaluation_window": "at least 30 evaluated signals and 7 days; judged on the later half (chronological)",
        "success_metrics": ["later-half mean executable return above the comparison group's",
                            "profit factor > 1.3 and net > 0 without the single best signal",
                            "direction hit rate reported, never sufficient on its own"],
        "failure_criteria": ["fewer than 30 signals in 14 days (untestable at this frequency)",
                             "later-half mean at or below the comparison group's, or net <= 0 without the best signal"],
        "comparison": compare,
        "testable": not reasons,
        "untestable_reasons": reasons,
        "plain_language": [describe(c) for c in conds],
    }
    return prop


def validate_edit(prop: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    """An operator's edit of a DRAFT proposal: conditions, categories, exit."""
    out = dict(prop)
    if "entry_conditions" in edit:
        conds = []
        for c in edit["entry_conditions"] or []:
            feat, op = c.get("feature"), c.get("op")
            if feat not in NUMERIC:
                raise LabError(f"unknown feature {feat!r}; one of the recorded decision-time features")
            if op not in OPS:
                raise LabError(f"op must be one of {OPS}")
            try:
                value = float(c.get("value"))
            except (TypeError, ValueError) as exc:
                raise LabError(f"{feat}: value must be a number") from exc
            tmpl = WORDS.get(feat)
            conds.append({"feature": feat, "op": op, "value": value,
                          "words": c.get("words") or (tmpl[1] if tmpl and tmpl[0] == op else None)})
        out["entry_conditions"] = conds
        out["plain_language"] = [describe(c) for c in conds]
    if "categories" in edit:
        cats = [c for c in edit["categories"] or [] if c in ENGINE_OF]
        if not cats:
            raise LabError(f"categories: at least one of {sorted(ENGINE_OF)}")
        out["categories"] = cats
    if "exit" in edit:
        ex = edit["exit"] or {}
        if ex.get("horizon") not in HORIZONS:
            raise LabError(f"horizon must be one of {HORIZONS}")
        out["exit"] = {"horizon": ex["horizon"], "target_pct": float(ex.get("target_pct", 20)),
                       "invalidation_pct": float(ex.get("invalidation_pct", 15))}
    reasons = [r for r in out.get("untestable_reasons", []) if "does not record" in r]
    if not out.get("entry_conditions"):
        reasons.append("no condition could be mapped to a recorded feature; add conditions before accepting")
    out["untestable_reasons"] = reasons
    out["testable"] = not reasons
    out["hypothesis"] = hypothesis(out.get("categories") or [], out.get("entry_conditions") or [],
                                   (out.get("exit") or {}).get("horizon", "T+5m"), out.get("comparison") or "")
    return out
