"""Frozen-set validation (master §38-39).

Each pass:
  1. freezes the due validation windows (yonixalpha_core.ml.frozen): one UTC
     day per sample family, weekly, which every trainer excludes from then on;
  2. scores each current model on the frozen sets of its family that it
     never saw (the set is in the model's metrics.frozen_excluded, or the
     model's data ended before the window started), pooled: the newest
     MAX_SETS_PER_MODEL such windows together, so a family with few labels a
     day still reaches a meaningful sample. A set the model trained on is
     never used to judge it.
  3. stores one report per (newest pooled set, model version), so a newly
     frozen window brings a new report, with AUC and its lower
     bound, Brier, calibration error (ECE), accuracy, precision, recall and
     the false positives / negatives at 0.5, plus the family's decision
     outcomes (ML BUY group returns, missed winners and bad entries; bad and
     missed exits), and a verdict:

       PASS               n >= MIN_N, MIN_CLASS of each class, AUC >= MIN_AUC
                          with its lower bound above 0.5, and ECE <= MAX_ECE
       FAIL               enough data, but one of those is not met
       INSUFFICIENT_DATA  too few labelled samples in the window

A PASS is required before an operator may raise a model's contribution
(ml.governance); it never raises anything by itself. Review data only.

Not validated here (no model exists): copy-trade decisions and wallet
exits (NOT_AVAILABLE).
"""

from __future__ import annotations

import io
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Callable

import joblib
from sklearn.metrics import brier_score_loss, roc_auc_score
from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import insert

from app.ablation import ece
from app.dataset import FEATURE_NAMES as CANDIDATE_FEATURES
from app.evm_ml import EVM_BINARY, EXIT_BINARY, WALLET_BINARY, evm_sample, exit_sample, wallet_sample
from app.shadow_ml import BINARY_TARGETS, sample as solana_sample
from app.train import AUC_CONFIDENCE_Z, MODEL_NAME as CANDIDATE_MODEL, _auc_standard_error
from yonixalpha_core.db.models import (EvmExitSample, EvmMlSample, MLFeatureSnapshot, MlValidationReport, MlValidationSet,
                                       ModelVersion, OpportunityOutcome, WalletTradeLabel)
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import evm_samples, frozen
from yonixalpha_core.ml.gate_features import ENGINES_FOR_MODEL, vector
from yonixalpha_core.ml.opportunity_features import FEATURE_VERSION as OPPORTUNITY_FV

log = get_logger("ml.validation")

MIN_N, MIN_CLASS, MIN_AUC, MAX_ECE = frozen.MIN_N, frozen.MIN_CLASS, frozen.MIN_AUC, frozen.MAX_ECE
THRESHOLD = 0.5
MAX_ROWS = 20_000  # per set; a larger window is sampled evenly (hash order), never the first hours only
STREAM_CHUNK = 1000
MAX_SETS_PER_MODEL = 8  # the newest unseen windows, pooled (about two months)
BUDGET_S = 600.0
EVALUATED_STATUSES = ("shadow", "challenger", "active", "trained")
NOT_AVAILABLE = frozen.NOT_AVAILABLE


def never_saw(model: ModelVersion, s: MlValidationSet) -> bool:
    """True when the model's training data cannot contain the set."""
    m = model.metrics or {}
    if s.id in (m.get("frozen_excluded") or []):
        return True
    end = m.get("dataset_end")
    if end:
        return datetime.fromisoformat(end) < s.window_start
    return model.trained_at is not None and model.trained_at < s.window_start


def scores(y: list[int], p: list[float]) -> dict[str, Any]:
    """Classification metrics at THRESHOLD; AUC only with both classes."""
    n, pos = len(y), sum(y)
    out: dict[str, Any] = {"n": n, "positives": pos, "negatives": n - pos}
    if not n:
        return out
    pred = [1 if v >= THRESHOLD else 0 for v in p]
    tp = sum(1 for a, b in zip(pred, y) if a and b)
    fp = sum(1 for a, b in zip(pred, y) if a and not b)
    fn = sum(1 for a, b in zip(pred, y) if not a and b)
    out.update({"accuracy": round((n - fp - fn) / n, 4),
                "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                "recall": round(tp / (tp + fn), 4) if tp + fn else None,
                "false_positives": fp, "false_negatives": fn,
                "brier": round(brier_score_loss(y, p), 4) if 0 < pos < n else None,
                "ece": ece(y, p)})
    if 0 < pos < n:
        auc = roc_auc_score(y, p)
        out["auc"] = round(auc, 4)
        out["auc_lower_bound"] = round(auc - AUC_CONFIDENCE_Z * _auc_standard_error(auc, pos, n - pos), 4)
    return out


def verdict(m: dict[str, Any]) -> tuple[str, str]:
    if m["n"] < MIN_N or m["positives"] < MIN_CLASS or m["negatives"] < MIN_CLASS:
        return ("INSUFFICIENT_DATA", f"{m['n']} labelled samples, {m['positives']} positive / {m['negatives']} negative "
                                     f"(needs {MIN_N}, at least {MIN_CLASS} of each)")
    fails = []
    if m["auc"] < MIN_AUC:
        fails.append(f"AUC {m['auc']} < {MIN_AUC}")
    if m["auc_lower_bound"] <= 0.5:
        fails.append(f"AUC lower bound {m['auc_lower_bound']} <= 0.5")
    if m["ece"] > MAX_ECE:
        fails.append(f"calibration error {m['ece']} > {MAX_ECE}")
    if fails:
        return "FAIL", "; ".join(fails)
    return "PASS", (f"AUC {m['auc']} (lower bound {m['auc_lower_bound']}), calibration error {m['ece']} on {m['n']} "
                    "samples it never saw")


def _mean(v: list[float]) -> float | None:
    return round(statistics.fmean(v), 4) if v else None


def _vec(x: dict[str, Any], model: ModelVersion) -> list[float]:
    med = (model.metrics or {}).get("medians") or {}
    return [x[n] if x.get(n) is not None else med.get(n, 0.0) for n in model.feature_names]


def _proba(est, rows: list[list[float]]) -> list[float]:
    return [float(v) for v in est.predict_proba(rows)[:, 1]] if rows else []


# --- loading a frozen set ----------------------------------------------------------------

async def _rows(session, stmt, col, convert: Callable[[Any], dict[str, Any] | None],
                limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """The set's rows (at most `limit`, spread evenly over the window),
    streamed: each row becomes its small sample dict before the next chunk
    is read (a Solana snapshot is the full intel blob; the server has 2 GB)."""
    out: list[dict[str, Any]] = []
    result = await session.stream(stmt.order_by(func.md5(cast(col, Text))).limit(limit)
                                  .execution_options(yield_per=STREAM_CHUNK))
    async for part in result.partitions(STREAM_CHUNK):
        out.extend(x for x in map(convert, part) if x is not None)
    return out


async def load_set(session, s: MlValidationSet, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """[{x, labels, segment, fv, set, ...}] of a frozen window ({row, set}
    for the Solana candidate family, scored per model below)."""
    out = await _load(session, s, limit)
    for d in out:
        d["set"] = s.id
    return out


async def _load(session, s: MlValidationSet, limit: int) -> list[dict[str, Any]]:
    a, b = s.window_start, s.window_end
    if s.family == "evm_entry":
        e = EvmMlSample

        def conv(r):
            smp = evm_sample(r)
            return smp and {"x": smp.x, "labels": smp.labels, "segment": smp.segment, "fv": r.feature_version,
                            "final": (r.verdicts or {}).get("final"), "executable": r.executable_return_pct}
        return await _rows(session, select(e.decided_at, e.features, e.labels, e.category, e.chain, e.launchpad,
                                           e.feature_version, e.verdicts, e.executable_return_pct)
                           .where(e.decided_at >= a, e.decided_at < b, e.labels["unknown"].is_(None)), e.decided_at, conv, limit)
    if s.family == "wallet_entry":
        w = WalletTradeLabel

        def conv(r):
            smp = wallet_sample(r)
            return smp and {"x": smp.x, "labels": smp.labels, "segment": smp.segment, "fv": r.feature_version}
        return await _rows(session, select(w.kind, w.outcome, w.entry_at, w.features, w.chain, w.launchpad,
                                           w.feature_version)
                           .where(w.entry_at >= a, w.entry_at < b, w.kind == "EPISODE"), w.entry_at, conv, limit)
    if s.family == "evm_exit":
        x = EvmExitSample

        def conv(r):
            smp = exit_sample(r)
            return smp and {"x": smp.x, "labels": smp.labels, "segment": smp.segment, "fv": r.feature_version,
                            "final": (r.verdicts or {}).get("final")}
        return await _rows(session, select(x.at, x.features, x.labels, x.chain, x.launchpad, x.feature_version, x.verdicts)
                           .where(x.at >= a, x.at < b, x.labels.is_not(None)), x.at, conv, limit)
    if s.family == "solana_opportunity":
        o = OpportunityOutcome

        def conv(r):
            smp = solana_sample(r)
            return smp and {"x": smp.x, "labels": smp.labels, "segment": smp.segment, "fv": OPPORTUNITY_FV}
        return await _rows(session, select(o.decided_at, o.labels, o.snapshot, o.engine, o.stage, o.regime)
                           .where(o.decided_at >= a, o.decided_at < b, o.status == "COMPLETE", o.labels.is_not(None)),
                           o.decided_at, conv, limit)
    if s.family == "solana_candidate":
        f = MLFeatureSnapshot
        return await _rows(session, select(f.id, f.candidate_id, f.assessment_id, f.created_at, f.features, f.label,
                                           f.engine, f.quality_status)
                           .where(f.created_at >= a, f.created_at < b, f.label.is_not(None)), f.created_at,
                           lambda r: {"row": r}, limit)
    return []


# --- scoring one model on one set ------------------------------------------------------

def _binary_key(model: ModelVersion) -> str | None:
    """The label a binary shadow model predicts (P_UPSIDE_50 -> upside_50)."""
    target = (model.metrics or {}).get("target")
    for table in (EVM_BINARY, WALLET_BINARY, EXIT_BINARY, BINARY_TARGETS):
        if target in table:
            return table[target]
    return None


def score_model(model: ModelVersion, est, family: str, data: list[dict[str, Any]]) -> tuple[list[int], list[float], dict]:
    """(labels, probabilities, info) of the model on the sets' samples;
    info["sets"]: the frozen set of each scored sample, aligned."""
    info: dict[str, Any] = {}
    if family == "solana_candidate":
        return _score_candidate(model, est, data, info)
    key = _binary_key(model)
    fv = (model.metrics or {}).get("feature_version")
    usable = [d for d in data if isinstance(d["labels"].get(key), bool) and (fv is None or d["fv"] == fv)]
    info["other_feature_version"] = sum(1 for d in data if fv is not None and d["fv"] != fv)
    p = _proba(est, [_vec(d["x"], model) for d in usable])
    ok = [(d, v) for d, v in zip(usable, p) if math.isfinite(v)]
    info["rows"] = ok
    info["sets"] = [d["set"] for d, _ in ok]
    return [int(d["labels"][key]) for d, _ in ok], [v for _, v in ok], info


def _score_candidate(model: ModelVersion, est, data, info) -> tuple[list[int], list[float], dict]:
    if model.name == CANDIDATE_MODEL:
        # per candidate, as training scores it (rows of one candidate share one outcome)
        groups: dict[str, list] = {}
        for d in sorted(data, key=lambda d: d["row"].created_at):
            r = d["row"]
            if any(n not in (r.features or {}) for n in CANDIDATE_FEATURES):
                continue
            groups.setdefault(str(r.candidate_id or r.id), []).append(d)
        y, p, sets = [], [], []
        for ds in groups.values():
            probs = _proba(est, [[float(d["row"].features[n]) for n in CANDIDATE_FEATURES] for d in ds])
            if probs:
                y.append(int(ds[-1]["row"].label))
                p.append(sum(probs) / len(probs))
                sets.append(ds[-1]["set"])
        info["scored_per"], info["sets"] = "candidate", sets
        return y, p, info
    # gate model: one sample per trade, the last snapshot (as gate_ml trains)
    latest: dict = {}
    for d in sorted(data, key=lambda d: d["row"].created_at):
        r = d["row"]
        if r.engine in ENGINES_FOR_MODEL.get(model.name, ()) and r.quality_status == "ok":
            latest[r.candidate_id or r.assessment_id or r.id] = d
    vecs = [(d, vector(model.name, d["row"].features)) for d in latest.values()]
    vecs = [(d, v) for d, v in vecs if v is not None]
    p = _proba(est, [[v[n] for n in model.feature_names] for _, v in vecs])
    info["scored_per"], info["sets"] = "trade", [d["set"] for d, _ in vecs]
    return [int(d["row"].label) for d, _ in vecs], p, info


def per_set(y: list[int], p: list[float], sets: list[int], windows: dict[int, str]) -> list[dict[str, Any]]:
    """The pooled samples split back by frozen window (AUC where defined)."""
    out = []
    for sid, day in windows.items():
        idx = [i for i, x in enumerate(sets) if x == sid]
        ys = [y[i] for i in idx]
        entry: dict[str, Any] = {"set_id": sid, "window": day, "n": len(idx), "positives": sum(ys)}
        if 0 < sum(ys) < len(ys):
            entry["auc"] = round(roc_auc_score(ys, [p[i] for i in idx]), 4)
        out.append(entry)
    return out


def evm_entry_outcomes(data: list[dict[str, Any]], up: dict[int, float], dump: dict[int, float]) -> dict[str, Any]:
    """The ML BUY group (P_UPSIDE_50 >= 0.5 and P_FAST_DUMP < 0.5) on the set,
    next to the rules' final BUY group: expected (60 min) and executable
    returns, drawdown, missed winners and bad entries."""
    def group(idx: list[int]) -> dict[str, Any]:
        lab = [data[i]["labels"] for i in idx]
        ex = [data[i]["executable"] for i in idx if data[i].get("executable") is not None]
        return {"n": len(idx), "mean_return_60m_pct": _mean([x["return_60m_pct"] for x in lab
                                                             if isinstance(x.get("return_60m_pct"), (int, float))]),
                "executable": {"n": len(ex), "mean_pct": _mean(ex)} if ex else
                {"n": 0, "note": "NOT AVAILABLE: no paper trade in this group closed"},
                "mean_max_drawdown_pct": _mean([x["max_drawdown_pct"] for x in lab
                                                if isinstance(x.get("max_drawdown_pct"), (int, float))]),
                "fast_dump_rate": _mean([float(bool(x.get("fast_dump"))) for x in lab])}

    both = [i for i in range(len(data)) if i in up and i in dump]
    ml_buy = [i for i in both if up[i] >= evm_samples.ML_BUY_UPSIDE and dump[i] < evm_samples.ML_REJECT_DUMP]
    chosen = set(ml_buy)
    ml_not = [i for i in both if i not in chosen]
    rules_buy = [i for i in both if data[i].get("final") == "BUY"]
    return {"scored": len(both), "ml_buy": group(ml_buy), "rules_final_buy": group(rules_buy),
            "ml_missed_winners": sum(1 for i in ml_not if data[i]["labels"].get("upside_50")),
            "ml_bad_entries": sum(1 for i in ml_buy if data[i]["labels"].get("fast_dump")),
            "rules_missed_winners": sum(1 for i in both if data[i].get("final") != "BUY" and data[i]["labels"].get("upside_50")),
            "rules_bad_entries": sum(1 for i in rules_buy if data[i]["labels"].get("fast_dump")),
            "definitions": {"ml_buy": "P_UPSIDE_50 >= 0.5 and P_FAST_DUMP < 0.5",
                            "missed_winner": "not bought, then rose 50 % within 60 min",
                            "bad_entry": "bought, then dumped fast"}}


def exit_outcomes(info: dict) -> dict[str, Any]:
    """SELL = P_FELL_10 >= 0.5. A bad exit: SELL, then the price rose 10 %; a
    missed exit: HOLD, then it fell 10 % (next 15 minutes). The system's own
    SELL / HOLD on the same checkpoints alongside."""
    rows = info.get("rows") or []
    ml_sell = [d for d, v in rows if v >= THRESHOLD]
    ml_hold = [d for d, v in rows if v < THRESHOLD]
    fin_sell = [d for d, _ in rows if d.get("final") == "SELL"]
    fin_hold = [d for d, _ in rows if d.get("final") != "SELL"]
    return {"ml": {"sell": len(ml_sell), "hold": len(ml_hold),
                   "bad_exits": sum(1 for d in ml_sell if d["labels"].get("rose_10")),
                   "missed_exits": sum(1 for d in ml_hold if d["labels"].get("fell_10"))},
            "rules_final": {"sell": len(fin_sell), "hold": len(fin_hold),
                            "bad_exits": sum(1 for d in fin_sell if d["labels"].get("rose_10")),
                            "missed_exits": sum(1 for d in fin_hold if d["labels"].get("fell_10"))},
            "definitions": {"bad_exit": "SELL, then the price rose 10 % in the next 15 min",
                            "missed_exit": "HOLD, then the price fell 10 % in the next 15 min"}}


def solana_outcomes(data: list[dict[str, Any]], up: dict[int, float], dump: dict[int, float]) -> dict[str, Any]:
    """Executable return of the ML BUY group on the frozen Solana set."""
    both = [i for i in range(len(data)) if i in up and (not dump or i in dump)]
    buy = [i for i in both if up[i] >= THRESHOLD and (not dump or dump[i] < THRESHOLD)]

    def ex(idx):
        v = [data[i]["labels"].get("executable_return_primary_pct") for i in idx]
        v = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
        return {"n": len(idx), "with_executable_return": len(v), "mean_executable_return_pct": _mean(v)}
    return {"scored": len(both), "ml_buy": ex(buy), "all": ex(both),
            "definitions": {"ml_buy": "P_UPSIDE_50 >= 0.5" + (" and P_FAST_DUMP < 0.5" if dump else "")}}


def segment_auc(info: dict, key: str, label_key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rows = info.get("rows") or []
    for val in sorted({str(d["segment"].get(key)) for d, _ in rows}):
        part = [(int(d["labels"][label_key]), v) for d, v in rows if str(d["segment"].get(key)) == val]
        ys = [a for a, _ in part]
        entry: dict[str, Any] = {"n": len(part), "positives": sum(ys)}
        if 0 < sum(ys) < len(ys):
            entry["auc"] = round(roc_auc_score(ys, [b for _, b in part]), 4)
        else:
            entry["note"] = "single class: AUC undefined"
        out[val] = entry
    return out


# --- the pass ------------------------------------------------------------------------------

async def _models(session) -> list[ModelVersion]:
    """Per model name, the newest version of each status: the operator-
    promoted champion (active), the challenger, the shadow model and the
    newest registered (trained) one. Regressions have no AUC: not here."""
    rows = (await session.execute(select(ModelVersion).where(ModelVersion.status.in_(EVALUATED_STATUSES))
                                  .order_by(ModelVersion.name, ModelVersion.version.desc()))).scalars().all()
    out, seen = [], set()
    for m in rows:
        if frozen.family_of(m.name) is None or (m.metrics or {}).get("kind") == "regression" or (m.name, m.status) in seen:
            continue
        seen.add((m.name, m.status))
        out.append(m)
    return out


async def run_validation(session_factory, now: datetime | None = None,
                         clock: Callable[[], float] = time.monotonic,
                         families: tuple[str, ...] = frozen.FAMILIES) -> dict[str, Any]:
    """`families`: the sample families in scope (frozen.SOLANA_FAMILIES in
    the SOLANA_ONLY system profile); models of other families are left as
    they are, neither frozen against nor scored."""
    now = now or datetime.now(timezone.utc)
    deadline = clock() + BUDGET_S
    async with session_factory() as session:
        frozen_now = await frozen.freeze_due(session, now, families)
        await session.commit()
    out: dict[str, Any] = {"frozen": frozen_now, "evaluated": 0, "no_unseen_set": 0, "not_available": NOT_AVAILABLE,
                           "families": list(families)}
    async with session_factory() as session:
        models = await _models(session)
        sets = (await session.execute(select(MlValidationSet).order_by(MlValidationSet.window_start.desc()))).scalars().all()
        done = {(a, b, c) for a, b, c in (await session.execute(select(
            MlValidationReport.set_id, MlValidationReport.model_name, MlValidationReport.model_version))).all()}
    # models that share the same unseen windows are scored on one load of them
    groups: dict[tuple[str, tuple[int, ...]], list[ModelVersion]] = {}
    for m in models:
        fam = frozen.family_of(m.name)
        if fam not in families:
            continue
        unseen = [s for s in sets if s.family == fam and never_saw(m, s)][:MAX_SETS_PER_MODEL]
        if not unseen:
            out["no_unseen_set"] += 1  # every frozen window of its family was in its training data
            continue
        if (unseen[0].id, m.name, m.version) not in done:
            groups.setdefault((fam, tuple(s.id for s in unseen)), []).append(m)
    by_id = {s.id: s for s in sets}
    for (family, ids), ms in groups.items():
        if clock() > deadline:
            out["budget_spent"] = True
            break
        windows = {i: str(by_id[i].window_start.date()) for i in ids}
        async with session_factory() as session:
            data: list[dict[str, Any]] = []
            for i in ids:  # the row cap is shared by the pooled windows
                data.extend(await load_set(session, by_id[i], max(1, MAX_ROWS // len(ids))))
            idx = {id(d): i for i, d in enumerate(data)}
            fam_scores: dict[str, dict[int, float]] = {}  # target -> {sample index: probability}
            scored = []
            for m in ms:
                y, p, info = score_model(m, joblib.load(io.BytesIO(m.artifact)), family, data)
                scored.append((m, y, p, info))
                if family in ("evm_entry", "solana_opportunity"):
                    fam_scores[(m.metrics or {}).get("target")] = {idx[id(d)]: v for d, v in info.get("rows") or []}
            for m, y, p, info in scored:
                metrics = scores(y, p)
                status, reason = verdict(metrics)
                metrics["scored_per"] = info.get("scored_per", "sample")
                metrics["sets"] = per_set(y, p, info.get("sets") or [], windows)
                if info.get("other_feature_version"):
                    metrics["other_feature_version_skipped"] = info["other_feature_version"]
                target = (m.metrics or {}).get("target")
                if family == "evm_entry":
                    metrics["by_category"] = segment_auc(info, "category", _binary_key(m))
                    if target == "P_UPSIDE_50" and "P_FAST_DUMP" in fam_scores:
                        metrics["decisions"] = evm_entry_outcomes(data, fam_scores[target], fam_scores["P_FAST_DUMP"])
                elif family == "evm_exit":
                    metrics["decisions"] = exit_outcomes(info)
                elif family == "solana_opportunity" and target == "P_UPSIDE_50":
                    metrics["decisions"] = solana_outcomes(data, fam_scores[target], fam_scores.get("P_FAST_DUMP", {}))
                await session.execute(insert(MlValidationReport).values(
                    set_id=ids[0], model_name=m.name, model_version=m.version, evaluated_at=now, status=status,
                    reason=reason[:300], metrics=metrics).on_conflict_do_nothing(
                    constraint="uq_ml_validation_reports_set_model"))
                out["evaluated"] += 1
                log.info("validation.report", model=m.name, version=m.version, family=family,
                         windows=list(windows.values()), status=status)
            await session.commit()
            del data
    return out
