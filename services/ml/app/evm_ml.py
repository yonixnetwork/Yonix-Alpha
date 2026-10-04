"""EVM and wallet-behaviour SHADOW models (master §36-44, §71-75; M12).

Each cycle:
  1. materialises new EVM opportunity samples (ml.evm_samples) and wallet
     episodes / missed winners (ml.wallet_labels), and fills executable
     returns of traded samples whose paper position has closed;
  2. trains shadow models per target with the same time split, purge and
     metrics as the Solana shadow models (app.shadow_ml), and registers each
     under shadow_evm_* / shadow_wallet_* with status "shadow" (never
     overwritten: a newer version supersedes, history is kept);
  3. scores unscored EVM samples and records the ML verdict (§41) there.
     A sample the model trained on is marked IN_SAMPLE, never compared as if
     it were new: only out-of-sample verdicts measure whether ML adds anything.

SHADOW: no entry, exit or size reads these models (they are not
"challenger", so the promotion flow cannot pick them up).

Scale (BSC opens ~20k observations a day): builders run in batches until
the backlog is drained or BUILD_BUDGET_S is spent, committing every batch;
training reads the newest MAX_TRAIN_ROWS labelled samples (columns only)
and retrains a target at most once per RETRAIN_EVERY, and only when newer
samples exist.
"""

from __future__ import annotations

import io
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import joblib
from sqlalchemy import select, update

from app.shadow_ml import MIN_ROWS, SHADOW, Sample, fit, time_split
from yonixalpha_core.db.models import EvmMlSample, ModelVersion, WalletTradeLabel
from yonixalpha_core.logging import get_logger
from yonixalpha_core.ml import evm_samples, registry, wallet_labels

log = get_logger("ml.evm")

EVM_BINARY = {"P_UPSIDE_50": "upside_50", "P_UPSIDE_100": "upside_100", "P_FAST_DUMP": "fast_dump",
              "P_MIGRATE": "migrate_60m"}
EVM_REGRESSION = {"E_RETURN_60M": "return_60m_pct", "E_MAX_DRAWDOWN": "max_drawdown_pct"}
WALLET_BINARY = {"P_SUCCESSFUL_ENTRY": "successful_entry"}
IN_SAMPLE = "IN_SAMPLE"
BUILD_BUDGET_S = 300.0
EVM_BATCH, WALLET_BATCH = 500, 200
MAX_TRAIN_ROWS = 20_000  # 2 GB server, shared with every other service
RETRAIN_EVERY = timedelta(hours=24)


def evm_sample(row: EvmMlSample) -> Sample | None:
    lab = row.labels or {}
    if not lab or "unknown" in lab or not row.features:
        return None
    return Sample(row.decided_at, row.decided_at + evm_samples.HORIZON, row.features, lab,
                  {"category": row.category, "chain": row.chain, "launchpad": row.launchpad})


def wallet_sample(row: WalletTradeLabel) -> Sample | None:
    out = row.outcome or {}
    if row.kind != "EPISODE" or "successful_entry" not in out or not row.features:
        return None
    return Sample(row.entry_at, datetime.fromisoformat(out["available_at"]), row.features, out,
                  {"chain": row.chain, "launchpad": row.launchpad})


async def _train(session, prefix: str, samples: list[Sample], names: tuple[str, ...], binary: dict[str, str],
                 regression: dict[str, str], feature_version: str, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"samples": len(samples)}
    if len(samples) < MIN_ROWS:
        out["status"] = f"skipped: {len(samples)} labelled samples (needs {MIN_ROWS})"
        return out
    dataset_end = max(s.decided_at for s in samples).isoformat()
    split: dict | None = None
    for target in (*binary, *regression):
        name = f"{prefix}{target.lower()}"
        prev = (await session.execute(select(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == SHADOW)
                                      .order_by(ModelVersion.version.desc()).limit(1))).scalar_one_or_none()
        if prev is not None and (prev.metrics or {}).get("dataset_end") == dataset_end:
            out[target] = "skipped_no_new_samples"
            continue
        if prev is not None and prev.trained_at is not None and prev.trained_at > now - RETRAIN_EVERY:
            out[target] = "skipped_retrained_within_24h"
            continue
        if split is None:
            train, hold, split = time_split(samples)
            out["split"] = split
        est, metrics = fit(target, train, hold, names, binary, regression)
        if est is None:
            out[target] = metrics.get("reason") or metrics.get("status")
            continue
        metrics.update({"target": target, "dataset_size": len(samples), "dataset_end": dataset_end, "split": split,
                        "max_train_rows": MAX_TRAIN_ROWS, "feature_version": feature_version,
                        "role": "SHADOW: review only, never used for decisions or sizing"})
        await session.execute(update(ModelVersion).where(ModelVersion.name == name, ModelVersion.status == SHADOW)
                              .values(status="superseded"))
        mv = await registry.register_trained_model(session, name=name, estimator=est, feature_names=list(names),
                                                   training_sample_count=metrics["train_rows"], metrics=metrics, status=SHADOW)
        out[target] = f"registered v{mv.version}"
    return out


async def score_evm(session, now: datetime, limit: int = 2000) -> int:
    models = (await session.execute(select(ModelVersion).where(ModelVersion.status == SHADOW,
                                                               ModelVersion.name.like("shadow_evm_%")))).scalars().all()
    if not models:
        return 0
    loaded = [(m, joblib.load(io.BytesIO(m.artifact))) for m in models]
    # unknown-labelled rows are never scored; left in the query they would
    # fill the batch and starve the labelled backlog
    rows = (await session.execute(select(EvmMlSample).where(EvmMlSample.ml_shadow.is_(None),
                                                            EvmMlSample.labels["unknown"].is_(None))
                                  .order_by(EvmMlSample.decided_at.desc()).limit(limit))).scalars().all()
    n = 0
    for row in rows:
        if not row.features or "unknown" in (row.labels or {}):
            continue
        scores: dict[str, Any] = {}
        oos = True
        for m, est in loaded:
            met = m.metrics or {}
            med = met.get("medians") or {}
            vec = [[row.features[f] if row.features.get(f) is not None else med.get(f, 0.0) for f in m.feature_names]]
            value = float(est.predict_proba(vec)[0, 1]) if met.get("kind") == "binary" else float(est.predict(vec)[0])
            if math.isfinite(value):
                scores[met.get("target") or m.name] = {"value": round(value, 4), "model": m.name, "version": m.version}
            start = (met.get("split") or {}).get("holdout_start")
            if start and row.decided_at < datetime.fromisoformat(start):
                oos = False  # this model saw the sample (or its neighbours) in training
        verdict = evm_samples.ml_verdict(scores)
        row.ml_shadow = {"scored_at": now.isoformat(), "scores": scores, "out_of_sample": oos,
                         "feature_version": evm_samples.FEATURE_VERSION,
                         "note": "SHADOW: never used for decisions or position sizing"}
        row.verdicts = {**(row.verdicts or {}), "ml": (verdict if oos else IN_SAMPLE) if verdict else "NOT_AVAILABLE"}
        n += 1
    return n


async def _drain(session_factory, build, now: datetime, batch: int, key: str, deadline: float) -> dict[str, Any]:
    """Runs build(session, now, batch) and commits until a batch comes back
    short (backlog drained) or the time budget is spent."""
    total: dict[str, Any] = {"batches": 0}
    while True:
        async with session_factory() as session:
            r = await build(session, now, batch)
            await session.commit()
        total["batches"] += 1
        for k, v in r.items():
            total[k] = total.get(k, 0) + v
        if r.get(key, 0) < batch:
            total["drained"] = True
            return total
        if time.monotonic() > deadline:
            total["drained"] = False
            return total


async def _latest(session, stmt, limit: int) -> list:
    """The newest `limit` rows of stmt (ordered newest first), oldest first."""
    return list(reversed((await session.execute(stmt.limit(limit))).all()))


async def run_evm_cycle(session_factory, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    deadline = time.monotonic() + BUILD_BUDGET_S
    out["samples"] = await _drain(session_factory, evm_samples.build, now, EVM_BATCH, "built", deadline)
    async with session_factory() as session:
        out["executable_filled"] = await evm_samples.refresh_executable(session)
        await session.commit()
    out["wallet_episodes"] = await _drain(session_factory, wallet_labels.build, now, WALLET_BATCH, "tokens",
                                          time.monotonic() + BUILD_BUDGET_S)
    async with session_factory() as session:
        out["missed_winners"] = await wallet_labels.build_missed(session, now)
        await session.commit()
    async with session_factory() as session:
        e = EvmMlSample
        evm_rows = await _latest(session, select(e.decided_at, e.features, e.labels, e.category, e.chain, e.launchpad).where(
            e.feature_version == evm_samples.FEATURE_VERSION, e.labels["unknown"].is_(None))
            .order_by(e.decided_at.desc()), MAX_TRAIN_ROWS)
        out["evm_models"] = await _train(session, "shadow_evm_", [s for s in map(evm_sample, evm_rows) if s],
                                         evm_samples.FEATURE_NAMES, EVM_BINARY, EVM_REGRESSION, evm_samples.FEATURE_VERSION,
                                         now)
        del evm_rows
        w = WalletTradeLabel
        w_rows = await _latest(session, select(w.kind, w.outcome, w.entry_at, w.features, w.chain, w.launchpad).where(
            w.kind == "EPISODE", w.feature_version == wallet_labels.FEATURE_VERSION).order_by(w.entry_at.desc()),
            MAX_TRAIN_ROWS)
        out["wallet_models"] = await _train(session, "shadow_wallet_", [s for s in map(wallet_sample, w_rows) if s],
                                            wallet_labels.FEATURE_NAMES, WALLET_BINARY, {}, wallet_labels.FEATURE_VERSION,
                                            now)
        await session.commit()
    async with session_factory() as session:
        out["scored"] = await score_evm(session, now)
        await session.commit()
    return out
