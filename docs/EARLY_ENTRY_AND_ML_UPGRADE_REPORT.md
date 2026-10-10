# Early entry intelligence and ML upgrade: report (2026-10-10)

Status: built and tested in the development container. **Not yet measured
on the production server. No LIVE behaviour was verified.** No real
transaction was sent. Every strategy added here starts in SHADOW. The
existing pipeline stays the champion and keeps placing the trades exactly
as before (its rules, thresholds, sizes and safety checks are unchanged).

Related documents: `EARLY_ENTRY_ROOT_CAUSE_AUDIT.md` (the trace),
`EARLY_ENTRY_RESEARCH.md` (repositories and documentation).

## 1. Root cause of late entries

Traced in code (details and file references in the audit):

1. The automatic stop is sized from volatility, which needs 3 or more
   10-second returns, so about 40 s of trading. Before that the gate
   returns REQUIRE_MANUAL_APPROVAL or NO_TRADE. This is a safety rule and
   was **not** changed.
2. Timers stacked: a 10 s observation window, a funnel pass every 10 s, a
   decision loop sleeping 15 s, and 30 s per-candidate pacing that ignored
   market events.
3. The fresh signal compared the price with the start of a 300 s window, so
   a token that had already pumped and was fading could still qualify.
4. LIVE adds about 3 s of confirmation, and LIVE trades of 0.0014-0.01 SOL
   make fixed fees a large share of each trade (server output, 2026-10-09).

## 2. Repositories inspected and findings

Six repositories were read at source level (the task lists one twice).
Summary (full evidence table in `EARLY_ENTRY_RESEARCH.md`):

| Repository | Useful, re-derived | Rejected |
|---|---|---|
| pumpsniper-main | inflow velocity, acceleration, SOL per trade, skip when decelerating | 94% paper win rate (random-draw simulator); no license |
| meme-sniper | record first, kill criterion fixed in advance, net-of-cost evaluation | none of its code (no license) |
| solana-sniping-bot | event-driven decisions (already present) | 100% slippage, MongoDB stack |
| Solana-Copy-Trading-Bot | none | wallet mirroring; copy trading stays disabled |
| pumpfun-2026 | none new (listener styles already covered) | learning code, "not for production" |
| solana_copy_trading | FIFO wallet results, minimum closed trades, hold-time statistics | genetic weight search on backtest PnL (overfits) |

## 3. Algorithms selected and rejected

Selected and implemented (all SHADOW, `packages/core-py/yonixalpha_core/entry_intel.py`):

- **Features** from the pump stream trades already held in Redis (no new
  RPC): 10 s / 30 s / 60 s windows with the previous 10 s and 60 s; inflow
  per second and its acceleration; trade rate and its acceleration; new
  buyers and sellers; net buy pressure; buy-size statistics; meaningful
  independent buyers (creator excluded); tiny-trade share; top-buyer share;
  synchronized buyer clusters; creator bought / sold; large buy then dump;
  displacement since launch, peak, drawdown and largest pullback so far;
  curve progress and its rate; round-trip cost at a reference size. A
  feature is computed only from trades at or before the decision time.
- **Phases**: EARLY_ACCEL, HEALTHY_CONTINUATION, EXHAUSTED, DISTRIBUTION,
  UNCLEAR.
- **Strategy A, EARLY_ACCELERATION**: young token, inflow and buyers
  accelerating, small displacement, organic demand checks pass.
- **Strategy B, SMART_WALLET_CONFIRMATION**: confirms an A or C candidate
  with wallets whose realized results (FIFO, fees, profit factor,
  drawdown, outlier dependence, resolved before the decision) are proven.
  It **never triggers by itself**. A proven wallet exiting, or a
  coordinated cluster, makes it NO_TRADE.
- **Strategy C, MOMENTUM_CONTINUATION**: older token in a healthy
  continuation (positive 60 s net flow, new buyers, sellers not dominating,
  not fading, limited drawdown).
- Each answers CANDIDATE, WAIT or NO_TRADE with exact reasons, an evidence
  level (LIMITED / MODERATE / STRONG) and a size factor (record only). With
  no reliable stop level, the answer is NO_TRADE.
- **Migrated tokens**: IMMEDIATE, DELAYED_CONFIRMATION, PULLBACK,
  CONTINUATION and NO_TRADE are recorded from PumpSwap pool reserves sampled
  in one `getMultipleAccounts` call per pass, and labelled with the pool's
  constant product and its observed fee (`entry_outcomes.label_migrated`).
- **Event-driven re-evaluation** of gate candidates (section 1, cause 2).
- **Candidate generation is separate from execution approval**: a strategy
  only records a signal. In PAPER mode it creates a gate candidate marked
  `paper_only`, which the unchanged safety gate evaluates and which can
  never become a LIVE order.
- **Labels** (`entry_outcomes.py`): the reference trade enters at the
  decision time plus the measured LIVE latency (median decision-to-confirm
  of the last confirmed LIVE buys, 3 s until 5 exist), never at the
  decision price. Exits: +30% / -20% / 300 s. The return is the executable
  round trip on the curve after fees and fixed costs. Horizons +30 s, +60 s,
  +5 min, +15 min; MFE / MAE; late entry; decelerating at entry; bad entry.
- **Evaluation** (`entry_eval.py`): chronological train 60% / validation
  20% / test 20%. The test period is frozen once 200 signals are labelled
  and never moves. A strategy beats the champion only with a higher median
  executable return, a higher profit factor, no worse bad-entry rate and a
  drawdown at most 1.5x the champion's, on the same frozen test period,
  then a non-negative forward period.
- **Readiness**: INSUFFICIENT_DATA, LEARNING, VALIDATING, SHADOW,
  PAPER_VALIDATED, PRODUCTION_CONTRIBUTOR, DRIFT_DETECTED, PAUSED. Nothing
  promotes itself: PAPER mode is an operator setting, and LIVE use does not
  exist in this release.
- **Entry-timing model** (`services/ml/app/entry_ml.py`): logistic
  regression on decision-time features. It is fitted on train, the
  regularization is chosen on validation, and it is scored once on the
  frozen test period against two baselines (base rate, the strategy's own
  score). It is registered as a new `entry_timing` version with status
  `shadow` (older versions are kept). Its probability is recorded next to
  each signal and is not used for any decision.

Rejected: random-draw paper friction, unbounded slippage, wallet mirroring,
genetic weight search, `processed` commitment, Jito bundles at the current
size, lowering any threshold to get more trades.

## 4. Files changed

New:

- `packages/core-py/yonixalpha_core/entry_intel.py`: features, phases, strategies, model vector, logistic inference
- `packages/core-py/yonixalpha_core/entry_outcomes.py`: fresh and migrated labels
- `packages/core-py/yonixalpha_core/entry_eval.py`: metrics, splits, frozen period, readiness, comparison
- `packages/core-py/yonixalpha_core/entry_store.py`: settings, signals, wallet evidence, labelling job, migrated sampling, dashboard state
- `packages/core-py/yonixalpha_core/entry_timing.py`: event timeline, latency summary, late entries
- `packages/core-py/yonixalpha_core/entry_parity.py`: paper vs LIVE per entry with reasons
- `packages/core-py/yonixalpha_core/gate_events.py`: wake list and event re-evaluation
- `packages/core-py/yonixalpha_core/tools/entry_timing.py`: server measurement tool (read-only)
- `packages/core-py/yonixalpha_core/testing/curve_sim.py`: deterministic curve simulator for tests
- `services/engine-solana-discovery/app/entry_shadow.py`: the shadow pass and background labelling
- `services/ml/app/entry_ml.py`: entry-timing model cycle
- `apps/api/app/api/routes/entry_intel.py`: `/api/entry-intel/*` (login required; settings change audited)
- `apps/api/migrations/versions/0047_entry_signals.py`: table `entry_signals`
- `apps/web/app/dashboard/entry-intel/page.tsx`: Entry Intelligence page
- Tests: `packages/core-py/tests/test_entry_intel.py`, `test_entry_store.py`, `services/engine-solana-discovery/tests/test_entry_shadow.py`, `services/decision-engine/tests/test_entry_gate.py`, `services/ml/tests/test_entry_ml.py`, `apps/api/tests/test_entry_intel_api.py`
- Docs: this report, the audit and the research.

Changed:

- `db/models.py`: `EntrySignal` model.
- `solana/pump_stream.py`: the create meta records `received_at` and `received_via`.
- `solana/pumpswap.py`: caches the latest observed pool fee.
- `engine-solana-discovery/app/funnel.py`, `app/main.py`: 2 s fast promotion pass; wake the gate; CURRENT_PROMOTE baseline; the shadow task.
- `decision-engine/app/gate_eval.py`: event re-evaluation; `paper_only` never LIVE; event assessment key; CURRENT_GATE_ENTRY baseline; timeline marks.
- `decision-engine/app/main.py`: wake-list loop (5 s fallback), new candidates first; legacy candidates keep 15 s.
- `services/ml/app/main.py`, `apps/api/app/api/routes/ml.py`: the `entry_timing` training step and its interval.
- `apps/api/app/api/router.py`, `apps/web/app/dashboard/layout.tsx`: route and menu entry.

## 5. Entry-latency measurements before / after

**NOT MEASURED YET.** The production database is not reachable from the
build container, and no number is filled in here without a measurement.

- Before: run `entry_timing --hours 24` right **after** deploying. The tool
  is new, so it cannot run on the old image. It reads database timestamps
  that already exist, so the last 24 h are almost entirely candidates from
  before the deploy.
- After: run it again 24 h after deploying. The new timeline (stream
  receive time, features ready, first strategy candidate, risk completed)
  exists only for tokens seen after the deploy.

Expected effect, from the code only (an upper bound, not a measurement):
promotion to first gate evaluation goes from up to 10 s (funnel) + 15 s
(loop) to about 2 s + under 1 s. Re-evaluation after a meaningful event
goes from 30 s to 10 s. The volatility requirement (about 40 s of trading)
is unchanged, so the existing pipeline will still not enter automatically
before it.

## 6. Strategy comparison

**No result yet.** Signals start being recorded at deploy time. Each is
labelled 15 minutes after it is recorded. The test period is frozen at 200
labelled signals. Until then every strategy shows INSUFFICIENT_DATA or
LEARNING on the dashboard. No win rate, return or profit factor is claimed
in this report.

## 7. Paper / LIVE parity findings

From code and the 2026-10-09 server output, not from a new comparison run:

- Each signal goes to one target (PAPER or LIVE), so per signal the other
  path is a counterfactual. The parity view says which values are
  estimated.
- PAPER fills use the decision price plus measured LIVE drift, failure
  rates and fixed costs (`paper_execution`). LIVE pays the real price after
  about 3 s of latency. The new view estimates, for each PAPER entry, the
  stream price move during the measured latency
  (`est_live_displacement_pct`).
- LIVE entries are refused after gate approval when the wallet is low
  (`live_entry_refused` timeline events). The view counts them by reason.
- LIVE sizes of 0.0014-0.01 SOL make the fixed network and priority fees a
  large percent of size. The view shows fees as a percent of size per
  entry.
- No paper fill rule was changed in this release. The comparison has to
  be read on the server first ("do not alter the live strategy until this
  comparison has been performed").

## 8. ML training dataset and validation status

- Dataset: `entry_signals` rows of strategies A, B and C with labels.
  **Empty at deploy.**
- The model trains only with 200+ train signals (10+ wins) and 30+ in the
  frozen test period. Until then the step reports INSUFFICIENT_DATA or
  LEARNING and registers nothing.
- When it does train, it is SHADOW: the probability is recorded, never
  used. "Beats baselines" is computed on the frozen test period and shown
  as evidence only.
- No ML accuracy is claimed.

## 9. CPU / RAM

Measured in the build container (Intel Xeon 2.8 GHz, 4 cores; not the
server):

- Features plus the three strategies: **1.98 ms per token** for a token
  with 400 trades (300 tokens in 593 ms).
- The shadow pass runs every 3 s, recomputes only tokens with a new trade
  (or every 30 s) and at most 150 tokens per pass. Worst case is therefore
  about 0.3 s CPU per 3 s, about 10% of one core. Typical is much lower,
  because most tokens have no new trade in 3 s.
- Memory: no new process or service. State is small Redis keys with TTLs:
  `yx:ee:state:*` 1 h, timeline 3 h, wallet cache 10 min, migrated samples
  3 h. The shadow pass pauses when host resources are CRITICAL
  (`resources`).
- Database: one indexed table. Labels are read and written in batches of
  at most 200 by index (`ix_entry_signals_unlabelled`). No history table is
  scanned in full.
- RPC: none for fresh tokens (stream data). One `getMultipleAccounts` per
  30 s for migrated pools, at background priority.
- ML: one small logistic regression per hour at most, inside the existing
  ml service schedule (which yields to live work in low-resource mode).

**Server CPU and RAM after deploy: NOT MEASURED.** Use the commands in
section 14.

## 10. Tests passed and failed

Added (deterministic, no network, no real transaction):

| Suite | New tests | Covers |
|---|---|---|
| core `test_entry_intel.py` | 31 | early acceleration; tiny-trade false acceleration; large buy then dump; rising buyers with falling inflow; price up with weak organic demand; smart wallet then dump; continuation; exhausted momentum; stale stream; creator sell; coordinated wallets; insufficient sample; slippage removing the profit; no future data in features; labels (latency, late entry, migration during observation, unknown fee); frozen splits; readiness; event and route-change detection |
| core `test_entry_store.py` | 11 | duplicate signals (once per strategy and token, also across restarts); wallet evidence uses only launches resolved before the decision; missing wallet history; measured latency; labelling job; migrated sampling and variants; timeline and waiting split; late entries; paper entry that cannot be replicated live (parity reasons) |
| discovery `test_entry_shadow.py` | 6 | the pass records once and skips unchanged tokens; worker restart; PAPER mode creates a `paper_only` candidate and wakes the gate; PAUSED records nothing; migration during observation; fast promotion |
| decision-engine `test_entry_gate.py` | 5 | a `paper_only` candidate never goes LIVE in LIVE mode; event re-evaluation only on a meaningful event and not before the minimum interval; CURRENT_GATE_ENTRY baseline; wake list |
| ml `test_entry_ml.py` | 2 | no model without data; frozen period does not move; new shadow versions are added, never overwritten |
| api `test_entry_intel_api.py` | 4 | login required; settings validated (LIVE refused) and audited; read endpoints |

Full-suite result: see section 10a (filled in from the run log).

### 10a. Full run

RESULTS_PLACEHOLDER

## 11. Live behaviours not verified

- The real latency before and after (section 5).
- That the event trigger fires at the expected rate on real traffic, and
  the extra gate RPC this causes. Each event evaluation costs one gate
  assessment, at most one per candidate per 10 s, and only on a
  meaningful event.
- The fast pass and the wake list under real load.
- Shadow pass CPU on the 2-CPU server.
- Migrated reserve sampling against real PumpSwap pools, and the observed
  fee cache.
- Any strategy's outcome, and the ML model (no data yet).
- Paper/LIVE parity numbers on real positions.
- Nothing was sent on-chain.

## 12. Rollback plan

- Fastest, no deploy: on the Entry Intelligence page set every strategy to
  PAUSED and switch off "Re-evaluate gate candidates early". Or set
  `{"enabled": false}` with `PUT /api/entry-intel/settings`. The shadow
  pass then does nothing, and pacing returns to the 30 s timer.
- Code: redeploy the previous commit (`scripts/deploy.sh` on the previous
  ref). Migration 0047 only adds `entry_signals`. It can stay, or be
  removed with `alembic downgrade 0046` (drops only that table).
- No existing table, setting or model is modified, so nothing else needs
  restoring.

## 13. Exact deployment commands

```
cd /opt/yonixalpha
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
scripts/deploy.sh
$C exec -T api python -m yonixalpha_core.tools.entry_timing --hours 24 > /tmp/entry_timing_before.txt
```

The api applies migration 0047 on start. The timing run right after the
deploy covers the previous 24 h, so it holds the "before" numbers.

## 14. Post-deployment verification commands

```
cd /opt/yonixalpha
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
$C exec -T postgres psql -U yonixalpha -d yonixalpha -c "select version_num from alembic_version"
$C exec -T redis redis-cli hgetall yx:ee:pass
$C exec -T postgres psql -U yonixalpha -d yonixalpha -c "select strategy, count(*), count(outcome_at) from entry_signals group by 1 order by 1"
$C logs --since 10m engine-solana-discovery | grep -i -E "entry_shadow|fast_pass" | tail -20
$C logs --since 10m decision-engine | grep -E "evaluation_loop.woken|gate.decision" | tail -20
docker stats --no-stream
```

After 24 h:

```
$C exec -T api python -m yonixalpha_core.tools.entry_timing --hours 24
```

Dashboard: Market > Entry Intelligence. It shows tokens being evaluated
now, strategy decisions with reasons, readiness, latency, late entries and
parity.
