# ML Training Lab and Signal Center (2026-10-21)

Dashboard: **Machine learning → ML Training Lab** (`/dashboard/ml/lab`).
API: `/api/ml-lab/*`. Code: `yonixalpha_core/ml_lab.py`, `yonixalpha_core/signal_center.py`, the ml service `_signal_loop`.
Migration: `0048` adds four new, empty tables. Nothing existing is changed.

**Nothing on this page trades.**
- It cannot place an order, change a mode, change a strategy, raise a size or promote a model.
- Every write is audited (`ml_lab.*` in the audit log).

## 1. Observation (Phase 4)

The system already observes continuously, whatever the trading mode. Every one of these is a row in the opportunity ledger (`opportunity_outcomes`), with the decision-time snapshot and the price path to T+60 m:

- fresh launch observations;
- gate decisions (approved or rejected);
- momentum passes, recorded as WATCH rows:
  - when momentum is OFF;
  - when no slot is free (since PR #93).

The lab reads this ledger. It does not add a second observation engine.

Controls:

| Control | Where | Effect |
|---|---|---|
| Momentum `monitor_when_off` (default on) | Strategies → Momentum | Records momentum passes as WATCH rows while momentum is OFF or has no slot. |
| Experiment Accept / Stop testing | ML Training Lab | Starts or stops recording signals for that experiment. |

Observation never authorises a trade.

## 2. Labelling observations (Phase 5)

1. Open **Observation queue**. Choose a category and a time window.
2. Press **Open** on a row. You see:
   - the data at the decision;
   - how many decision-time features were present;
   - the price path (T+60 s … T+60 m);
   - the executable return of a 0.05 SOL round trip after costs;
   - the labels already given.
3. Choose a label. You can add a value and a note. Press **Save label**.

There are 15 labels. Each saved label stores:
- the timestamp, the token and the decision time;
- the decision-time features, with their feature version;
- the shadow model version, if one was scored;
- the source (`MANUAL`) and your user name.

**A label is your opinion. It is never proof.**
- The outcome never rewrites it.
- The **Recorded outcome** column compares it with what the ledger measured:

| Status | Meaning |
|---|---|
| `agrees` | The ledger's numbers match your label. |
| `disagrees` | The ledger's numbers contradict your label. |
| `pending` | The outcome is not measured yet. |
| `opinion only` | No recorded outcome measures this label, for example "buying pressure was weak". |

The rule behind each status is shown in **Your labels against the recorded outcomes**.

You can give each label once per observation. Another operator can give the same label.

## 3. From an idea to an experiment (Phase 5)

1. Under **Ideas and experiments**, write the idea in plain words, then press **Propose experiment**. Example:
   > migrated tokens with sustained buying volume, qualified wallets and liquidity above 30 SOL before a large price extension may outperform the existing confirmation strategy
2. The system shows a proposal. Read it before doing anything else. It contains:
   - the hypothesis;
   - the categories;
   - the entry conditions in plain words, each with its exact rule (feature, operator, value);
   - what never happens;
   - the horizon, target and invalidation;
   - the comparison group;
   - the success and failure criteria.
3. Edit it if needed while it is a DRAFT. You can change conditions, categories and horizon, then press **Save changes**.
   - Only recorded decision-time features can be used.
   - An outcome (for example the executable return) can never be a condition.
4. Press **Accept and start testing**, or **Reject**. Nothing runs before you accept.
5. An idea is **not testable**, and cannot be accepted, if it depends on data the system does not record (Twitter/X, Telegram, news, sentiment, influencers) or if no condition could be mapped.
6. An accepted experiment cannot be edited. To change it, write a new idea. **Stop testing** retires it.

An experiment is never turned into a production strategy by itself.

## 4. Signals and their outcomes (Phase 6)

While an experiment is ACCEPTED, the ml service checks new ledger decisions every 30 s.

Recording rules:
- A decision of the experiment's categories whose decision-time features meet every condition becomes a **signal**.
- The signal has a unique id and a timestamp. It stores the inputs used and the success definition.
- Only decisions made after you accepted, and at most 120 s old, become signals. A prediction is always recorded before its horizon has passed. Nothing is backfilled.
- A missing feature means no signal. A value is never guessed.

The `ml_signals` table refuses UPDATE and DELETE (database trigger), so a recorded prediction can never be rewritten.

When the ledger has the price path to the signal's horizon, the outcome is stored separately (`ml_signal_outcomes`):

| Measure | Meaning |
|---|---|
| direction | Was the price above the decision price at the horizon? |
| executable | Did a 0.05 SOL round trip exist? |
| executable return / net profitable | After fees, curve impact, latency and fixed costs. |
| MFE / MAE | Best and worst move by the horizon. |
| first touched | TARGET, INVALIDATION, NEITHER, or SAME_INTERVAL. Measured at the ledger's horizon resolution. |
| would fail | Why a real trade could not have been made. |

Default horizons by category: fresh T+5m, migrated T+15m, momentum T+15m. You can change the horizon in a draft.

### The report (Details on an experiment)

The report shows:
- the direction hit rate. This is never enough on its own;
- the executable rate;
- mean, win rate, profit factor and drawdown, for all signals, the earlier half and the later half;
- the result without the best signal;
- which level was touched first;
- the comparison group, measured the same way. This is either the same categories' decisions since acceptance, or, if the idea named the existing strategy, the decisions the gate traded.

| Verdict | Meaning |
|---|---|
| `insufficient data` | Fewer than 30 evaluated signals. |
| `not supported` | The later half did not beat the comparison, or profit factor ≤ 1.3, or net ≤ 0 without the best signal. |
| `supported forward` | Evidence for more testing. Not proof of future profit, and never a live switch. |

## 5. Dataset export (Phase 5)

**Training dataset export** downloads a CSV with:
- one row per ledger decision (deduplicated by ledger key);
- the decision-time features;
- the recorded outcomes, as separate `outcome_*` columns;
- your labels.

Winners, losers, rejected and untraded decisions are all included. The page shows how many rows have missing features and how many outcomes are still being measured.

**Leakage rule:** the features come from the decision-time snapshot only. The `outcome_*` columns were measured after the decision. Never use them as features.

## 6. The learning cycle (Phase 7)

| Step | Where |
|---|---|
| OBSERVE | Opportunity ledger (section 1). |
| RECORD | Ledger, plus signals for accepted experiments. |
| LABEL | Manual labels (section 2). The ledger's own outcome labels are separate. |
| EVALUATE | Signal outcomes and reports (section 4). ML Review for models. |
| TRAIN | The ml service trains shadow models on a schedule, as before. |
| VALIDATE | Frozen validation sets (`ml/validation`, `ml/frozen`). |
| COMPARE | Champion / challenger on ML Review; experiment against comparison group here. |
| PROPOSE | The report verdict; ML Review shows candidates. |
| APPROVE | Only an operator. ML governance (`ml/governance.py`). |

Approval rules:
- A model's contribution starts at 0 %.
- It rises only by an audited operator change, one step at a time.
- It can rise only while frozen validation passes and the model is the operator-promoted champion.
- Lowering or setting 0 % is always allowed at once. This is the rollback.
- LIVE_CONTRIBUTOR is locked.

Nothing raises a size, enables live trading or activates a model because results were good.

## 7. Limits

- Experiment conditions are simple thresholds on recorded features. An idea that needs anything else must be written as such a threshold, or it stays untestable.
- "First touched" uses the ledger's horizons (60 s, 5, 15, 30, 60 min). Inside one interval the order cannot be known, so it shows SAME_INTERVAL.
- A signal is a prediction for the 0.05 SOL reference trade. It is not a fill.
