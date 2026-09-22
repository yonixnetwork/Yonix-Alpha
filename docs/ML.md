# ML: training, registry, inference

Phase 6 adds a real training/registry/inference pipeline. It is honestly
expected to sit idle — training nothing, activating nothing — for a long
time, because the one thing it needs to do anything useful, labeled trade
outcomes, does not exist anywhere in this database yet. This document
explains why, and what the pipeline does once that changes.

## Why there is no trained model today

Supervised learning needs examples of what actually happened. In this
codebase, that means: a `TradingCandidate` reached a real outcome (closed
profitably or not) and that outcome got recorded. Neither has ever
happened:

- **No Solana price feed exists anywhere in this codebase** (see
  `ARCHITECTURE_AUDIT.md` and `services/decision-engine`'s own Phase 5
  notes). Without one, there is no way to compute whether a hypothetical
  Solana position would have been profitable, even retroactively.
- **`decision-engine`'s own confidence cap keeps every Solana evaluation
  at `WAIT` or `NO_TRADE`** (Phase 5: `DEGRADED` data quality caps
  confidence at 0.35, below the 0.6 entry threshold). No candidate has
  ever reached `QUALIFIED`, let alone `ENTERED` or `CLOSED`.
- **Paper trading (Phase 7) doesn't exist yet.** That's the first phase
  in this project's plan expected to actually close positions and record
  real outcomes without needing live capital.

So `yonixalpha_core.db.models.MLFeatureSnapshot.label` — the supervised
target column this whole pipeline reads — is `NULL` on every row in this
database, and will stay that way until a future phase starts setting it.
Building a "model" today would mean training on fabricated labels, which
the project's core rule (never fabricate data — return `NO_TRADE` /
"insufficient data" instead) explicitly forbids. So Phase 6 ships the
pipeline, not a model.

## What's real today

- **A genuine feature store.** Every `decision-engine` evaluation persists
  an `MLFeatureSnapshot` row (`ml_features` table): the exact feature
  vector it computed, the symbol, the candidate it came from, which
  (if any) `ModelVersion` scored it and what it said, and a `label`
  column that starts `NULL` and is designed to be backfilled later —
  never during evaluation itself, which cannot know the outcome yet.
- **A genuine model registry** (`yonixalpha_core.ml.registry`,
  `model_versions` table). Training never overwrites a prior version —
  every trained artifact is kept, append-only, with its own metrics,
  `feature_names`, and status (`trained` / `active` / `retired`). Exactly
  one version per model `name` is `active` at a time; `SklearnModel`
  loads that version's joblib-serialized estimator straight out of
  Postgres (no separate object-storage dependency for this phase) and
  maps a caller's named feature dict onto the exact column order it was
  trained on.
- **A genuine training job** (`services/ml`). Runs hourly (training is
  infrequent by nature; there's no reason to poll faster). Every run:
  loads every `ml_features` row with a non-`NULL` label, and if there are
  fewer than `MIN_TRAINING_SAMPLES` (50) or only one class present, it
  stops there and records exactly that in a `SystemEvent` — which, per
  everything above, is the expected outcome of every run for the
  foreseeable future. Given enough labeled, two-class data, it trains a
  `LogisticRegression`, evaluates it on a held-out split, registers the
  result unconditionally, and **only** promotes it to `active` if its
  holdout AUC clears `MIN_ACTIVATION_AUC` (0.55) *and* is at least as
  good as whatever is currently active. A model that trains but doesn't
  clear that bar stays `trained` — inspectable, never silently discarded,
  never auto-promoted either.
- **Genuine, safe inference integration.** `decision-engine` calls
  `registry.get_active_model()` on every evaluation. When nothing is
  active — today, always — it gets `NullModel`, whose `model_version` is
  `None`; `evaluate.py` checks exactly that field to decide whether to
  blend ML into confidence at all, and when there's no real model it
  says so directly in the decision's `reason` list
  (`"no active trained ML model — confidence is rule-based only"`)
  rather than blending in some fabricated "neutral" score that would
  silently bias confidence upward for a candidate with nothing backing
  it. When a real model *is* active, its score is averaged with the
  rule-based confidence, and Phase 5's `DEGRADED`-data cap is
  **re-applied after blending** — a confident model can never push a
  DEGRADED-data candidate's confidence back above the safety ceiling.

## What happens once real outcomes exist

Nothing in this pipeline needs to change. A future phase (most likely
Phase 7, paper trading) sets `MLFeatureSnapshot.label` on rows whose
candidate reached a known outcome, `services/ml`'s hourly loop picks up
enough labeled rows to clear `MIN_TRAINING_SAMPLES`, trains for real, and
— if the model is actually good — activates it. `decision-engine` starts
blending its score in on the very next evaluation, with no deploy or code
change required. That's the point of building the plumbing now: so the
first real model can go live the moment there's real data, not require a
whole extra phase of infrastructure work once it exists.

## Where things live

| Concern | Location |
|---|---|
| `ModelVersion` / `MLFeatureSnapshot` schema | `packages/core-py/yonixalpha_core/db/models.py` |
| `MLModel` protocol, `NullModel`, `SklearnModel` | `packages/core-py/yonixalpha_core/ml/model.py`, `sklearn_model.py` |
| Registry (register / activate / load active) | `packages/core-py/yonixalpha_core/ml/registry.py` |
| Feature vector construction | `services/decision-engine/app/ml_features.py` |
| Inference blending + feature-snapshot persistence | `services/decision-engine/app/evaluate.py` |
| Labeled-dataset loading | `services/ml/app/dataset.py` |
| Training job + activation gating | `services/ml/app/train.py` |
| Periodic training loop | `services/ml/app/main.py` |

`numpy`/`scikit-learn`/`joblib` are an optional `[ml]` extra on
`yonixalpha-core` (`packages/core-py/pyproject.toml`) rather than a base
dependency — only `decision-engine` (inference) and `ml` (training) need
them; every other service's image stays free of them.
