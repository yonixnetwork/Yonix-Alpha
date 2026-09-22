# YonixAlpha — Final Production Audit

Independent verification of the implemented system. Nothing here is taken
on the word of a previous phase, including phases written by the same
author. Every claim below is either backed by a command that was actually
run, or explicitly marked as unverified.

Companion document: `FINAL_AUDIT_ARCHITECTURE.md`.

**Safety posture held throughout:** `TRADING_ENABLED=false`,
`LIVE_TRADING_ENABLED=false`, paper/simulation only. No real order, swap,
wallet or exchange credential was used. All external integrations were
exercised against mocked transports or a local database. This was not a
matter of discipline alone — see C-5: the codebase has no reachable path
to a live order at all.

---

## Executive summary

The system is **structurally sound and, in its current state, incapable
of losing money** — because it is also incapable of placing a trade. The
foundations are genuinely good: money is `Decimal` end to end, the risk
engine is well-designed and has real authority, authentication survived
every attack thrown at it, and state recovery across restarts is clean.

The audit nonetheless found **9 real defects**, three of which would have
been serious in live operation:

1. A stop-loss that **silently never fires** — one poisoned row halted
   position management for every other open position, permanently.
2. Three of the four risk limits the deployment runbook tells operators
   to set **could not fire at all**, giving false assurance that capital
   was capped.
3. The ML holdout was **leaking**, measured at AUC 0.73 on pure noise,
   and would have activated a worthless model into live decisioning
   roughly 42% of the time.

All nine were fixed, each with a regression test that fails against the
pre-audit code. Test count went from 281 to 307.

Two findings were **disproven** during investigation and are recorded as
such, because an audit that only confirms its own hypotheses is not an
audit.

**Production ready: CONDITIONAL.** Safe to deploy for observation and
paper trading. Not ready for live capital — not because of the defects
found, but because the execution layer does not exist.

---

## Verification status by component

| Component | Status | Evidence |
|---|---|---|
| Authentication / authorization | **VERIFIED** | 37/37 adversarial checks passed |
| Money precision (`Decimal` end to end) | **VERIFIED** | grep + 7 hand-computed PnL cases |
| PnL arithmetic | **VERIFIED** | independent recomputation, 7/7 exact |
| Risk engine logic | **VERIFIED** | 29 tests; fail-closed on unknown inputs |
| State recovery (app restart) | **VERIFIED** | 11/12 invariants; no dup/loss/false PnL |
| Database schema + migrations | **VERIFIED** | clean-DB upgrade, `alembic check`, reversible |
| Idempotent order placement | **VERIFIED** | intent committed pre-exchange; never resubmits |
| Order reconciliation | **VERIFIED** (after fix) | 48 tests incl. 6 new ambiguous-4xx cases |
| Frontend dashboard (7 pages) | **VERIFIED** | real browser, real API, real DB |
| Kill switch (engage/disengage) | **VERIFIED** | API + UI round trip in a real browser |
| nginx TLS / headers / rate limit | **VERIFIED** | real nginx binary, live HTTP |
| ML leakage + activation gate | **VERIFIED** (after fix) | measured false-activation 42% → 2-10% |
| Paper-trading failure isolation | **VERIFIED** (after fix) | reproduced, fixed, regression-tested |
| Secret hygiene | **VERIFIED** | gitleaks clean on full history |
| Kill-switch durability | **PARTIALLY VERIFIED** | config measured; loss not reproduced — see H-4 |
| Docker build / compose up | **NOT VERIFIED** | Docker Hub unreachable from this sandbox |
| DigitalOcean droplet / DNS / TLS cert | **NOT VERIFIED** | no droplet, no DNS, no ACME reachability |
| Live Solana RPC / Binance / Telegram | **NOT VERIFIED** | egress blocked; mocks only |
| Execution engine end-to-end | **NOT APPLICABLE** | no caller exists — see C-5 |
| WebSocket layer | **DOES NOT EXIST** | see L-1 |

---

## Findings

Severity reflects impact **if live trading were enabled**.

### C-1 — CRITICAL (fixed): a stop-loss that silently never fires

*Files:* `services/paper-trading/app/main.py`, `app/manage.py`,
`app/entry.py`, `packages/core-py/yonixalpha_core/db/models.py`,
migration `0008`

`close_position()` divided by `entry_price * quantity` with no zero
guard. `_manage_open_positions()` had no per-position isolation, and the
loop's single `try/except` wrapped *both* the open and manage phases.

So one position with a zero cost basis raised `DivisionByZero`, aborted
the entire manage batch, and **every other open position stopped being
evaluated** — including its stop-loss. The poisoned row was never cleaned
up, so this repeated every 15s cycle forever.

Reachable for real: `StrategySignal.entry` is `Numeric(38,18)`, so a
genuine Solana memecoin price below 1e-18 rounds to exactly 0 on write.

**Reproduced.** Price 0.5 against a 0.9 stop-loss: position stayed
`open`, `_manage_open_positions` aborted with `DivisionByZero`.

*Fixed at all three layers:* entry rejects non-positive prices; two DB
`CHECK` constraints make the row shape impossible; `close_position`
tolerates legacy rows (`realized_pnl_pct = NULL`, undefined ≠ zero); both
loop phases isolate per item, matching `decision-engine`'s existing
pattern. 6 regression tests.

### C-2 — CRITICAL (fixed): three of four risk limits could not fire

*File:* `packages/core-py/yonixalpha_core/risk.py`, `docs/DEPLOYMENT.md`

`RiskContext` encoded "unknown" as `Decimal(0)` for
`proposed_position_size`, `current_portfolio_exposure` and
`daily_realized_pnl`. No caller in the codebase can measure any of them,
so all three always compared as 0 — silently **satisfying every
corresponding limit**.

`docs/DEPLOYMENT.md` instructs the operator, as step 1 before enabling
live trading, to set `MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`,
`MAX_SLIPPAGE`, `MAX_OPEN_POSITIONS`. Measured reality:

| Limit | Before | Now |
|---|---|---|
| `MAX_OPEN_POSITIONS` | enforced | enforced |
| `MAX_POSITION_SIZE` | **silently inert** | blocks trading (fails closed) |
| `MAX_DAILY_LOSS` | **silently inert** | blocks trading (fails closed) |
| `MAX_SLIPPAGE` | never mapped into `RiskConfig` | unchanged; documented as inert |

A limit that cannot fire is worse than no limit, because the operator
believes capital is protected.

*Fixed:* unknown is now `None` and distinct from a measured zero. A
configured limit with an unmeasurable input is a **rejection**. If that
blocks all trading, that is the correct and honest outcome until the
measurement exists. 5 regression tests, including one proving a genuine
measured zero still passes. `docs/DEPLOYMENT.md` now states which limits
actually work.

### C-3 — HIGH (fixed): ML holdout leaked; worthless models would activate

*Files:* `services/ml/app/dataset.py`, `app/train.py`

`decision-engine` writes one `ml_features` row per candidate **per 15s
cycle**; `paper-trading` then stamps the *same* label on all of them. A
candidate watched for 10 minutes yields ~40 near-identical rows sharing
one outcome. `train.py` split them with
`train_test_split(..., random_state=42)` — **randomly, by row** — so
siblings landed on both sides and a model could score its own training
data.

`MIN_TRAINING_SAMPLES = 50` counted rows, not outcomes: 50 rows can be
two candidates.

**Measured on a dataset of pure coin flips (no signal whatsoever):**

| Split | Holdout AUC | Gate (≥0.55) |
|---|---|---|
| random by row (pre-audit) | **0.730** | **activates a worthless model** |
| grouped by candidate | 0.000 | correctly refuses |

*Fixed:* split is grouped by candidate **and** forward in time (train on
older candidates, test on strictly newer). The sample gate counts
distinct candidates.

### C-4 — HIGH (fixed): the activation gate was statistically meaningless

Found only because C-3's fix was re-measured rather than assumed. With
grouping fixed, mean AUC correctly returned to ~0.50 — but a fixed 0.55
threshold on a small holdout **still activated noise models ~42% of the
time**, and it did *not* improve with more data (42% at 50 candidates,
45% at 100, 42% at 200), because the holdout grows proportionally while
the threshold stays put. Training runs hourly, so "unlikely per run"
becomes "certain by tomorrow".

*Fixed:* the holdout is scored **per candidate** (the independent unit,
not per row), and activation additionally requires a Hanley–McNeil 95%
one-sided lower confidence bound above 0.5, plus ≥10 holdout candidates.

**Measured after the fix:**

| Candidates | False activation on noise | True activation on real signal |
|---|---|---|
| 50 | 42% → **2%** | 65% |
| 100 | 45% → **10%** | 90% |
| 200 | 42% → **8%** | 100% |

Residual ~5-10% is the nominal α of a one-sided 95% test. Stated as a
remaining limitation (R-1) rather than hidden.

### C-5 — HIGH (documentation corrected; not a code defect)

`README.md` stated the execution router "currently sends [every] approved
Binance decision" to `place_order_idempotent()`.

**That is not true.** `execution_router.route()` is a pure classification
function returning an enum; it sends nothing. `place_order_idempotent()`
has **no production caller** — its only non-test reference anywhere is a
docstring.

This is a safety *positive* (no autonomous trading is possible by
construction), but a documentation defect that materially misrepresents
system capability. Corrected in `README.md`.

### H-1 — HIGH (fixed): reconciliation could permanently abandon a live order

*File:* `services/engine-binance-futures/app/orders.py`

`reconcile_pending_orders()` treated **any HTTP 400** as "Binance has no
record of this order" and marked it `not_found` — a terminal state that
stops reconciliation forever.

Binance returns 400 for a family of conditions that say nothing about
whether an order is live: `-1121` invalid symbol, `-1102` malformed
parameter, and others. A transient config or parameter problem would
therefore mark a **live, open position** as nonexistent, and the system
would go on believing it had no position.

The brief's rule is "never blindly retry an unknown-status order"; the
converse matters just as much — never blindly *abandon* one.

*Fixed:* only Binance's documented `-2013` ("Order does not exist"),
parsed from the response body, is terminal. Every other 4xx stays
non-terminal and is retried. Added a stuck-order alarm
(`orders.reconcile_stuck`) after 15 minutes so limbo is visible rather
than silent. 6 new tests covering `-1121`, `-1102`, non-JSON bodies,
missing `code`, empty body, plus a retry-then-resolve case.

### H-2 — MEDIUM (fixed): stale prices could trigger exits

*File:* `services/paper-trading/app/pricing.py`

`latest_price()` returned the most recent `market_snapshots` row
**regardless of age**, while its own docstring claimed it "never falls
back to a stale... guess". A price from an arbitrarily dead feed could
mark or exit a position.

*Fixed:* a 300s maximum age, matching the horizon `decision-engine` and
`engine-solana-momentum` already use. Returns `None` beyond that —
declining to act rather than acting on a memory. 2 regression tests.

### H-3 — MEDIUM (fixed): one malformed JSONB row white-screened the dashboard

*Files:* `apps/api/app/schemas/candidates.py`, `apps/web/lib/format.ts`,
candidates pages

`state_history` was typed `list[Any]` and forwarded verbatim from a JSONB
column; the dashboard called `.replace()` on `entry.state`. A row without
a `state` key raised an uncaught `TypeError` and **the entire candidate
detail page failed to render**.

*Honesty note:* this surfaced because an audit fixture used the wrong
key shape. The normal write path (`apply_transition`) is correct, so this
is **not** a defect in everyday operation. It is a real robustness defect
nonetheless — a JSONB column has no schema guarantee, and a dashboard
that white-screens is an operational hazard.

*Fixed:* a typed `StateHistoryEntry` at the API boundary (defaults
`state` to `"unknown"`), plus a tolerant `formatState()` helper replacing
all four raw `.replace()` call sites. Verified in a real browser with
deliberately malformed data injected into Postgres: page renders, zero
page errors. 1 API regression test.

### H-4 — MEDIUM (mitigated; partially verified): kill-switch durability

*File:* `infra/docker/docker-compose.yml`

The kill switch — the emergency stop the risk engine checks first and
unconditionally — lives **only in Redis**, with no durable copy.

**Measured:** the Redis in use reports `appendonly no` and
`save 3600 1 300 100 60 10000`. For a single key change the binding rule
is `save 3600 1`: on an otherwise quiet system, engaging the kill switch
can sit unpersisted for up to an hour. A crash in that window brings
Redis back with the switch **disengaged** — it fails *open*, re-arming
trading, which is the wrong direction for an emergency stop.

**Verified:** the config values above, read directly.
**Not verified:** actual loss. Attempts to reproduce it were inconclusive
— this sandbox would not let me cleanly isolate a second Redis instance,
and the shared instance had unrelated write activity triggering
snapshots. The durability consequence is therefore reasoned from
documented Redis semantics, not reproduced. Saying so is the point.

*Mitigated:* compose now starts Redis with `--appendonly yes`, reducing
the worst-case loss window from ~1 hour to ~1 second. Recommended
follow-up in R-2.

### L-1 — LOW (fixed): CSP allowed `wss:` for a feature that does not exist

*File:* `infra/nginx/nginx.conf`

The Phase 10 CSP included `connect-src 'self' wss:`, justified by a
comment describing "the dashboard's WebSocket subscriptions". There is no
WebSocket server endpoint and no WebSocket client anywhere in the
codebase; `NEXT_PUBLIC_WS_URL` is configured and unused.

*Fixed:* `connect-src 'self'`. Re-verified live against the real nginx
binary — all five headers present, `/api/` proxying unaffected, login
rate limit still returns 429 on the 7th rapid request.

### L-2 — LOW (fixed): paper PnL silently assumed zero trading costs

*Files:* `packages/core-py/yonixalpha_core/config.py`,
`services/paper-trading/app/manage.py`

No fee, spread or slippage term existed anywhere in `close_position()`.
Because the ML label is literally `realized_pnl > 0`, a trade clearing
+0.10% gross — a clear net loss after real Solana DEX costs — trained the
model as a **winner**. Verified: entry 100 → exit 100.10 booked
`realized_pnl = 0.10`.

*Fixed:* `PAPER_TRADING_PER_LEG_COST_BPS`, charged per leg, default `0`.
The default is deliberately not a claim that trading is free — it is a
refusal to invent a cost constant that cannot be verified from this
environment. It is now a declared, tunable assumption instead of an
invisible one. 2 regression tests.

---

## Findings raised and DISPROVEN

Recorded because an audit that only confirms its own hypotheses is not an
audit.

**D-1 — "Zero entry price crashes position entry."** Hypothesis: the
unguarded `SIZE / signal.entry` in `entry.py:71` would raise. **Tested:
it did not.** `route()` rejects every Solana candidate before that line
is reached, so the division is currently unreachable. Reclassified as
latent and guarded anyway, since the guard is free and the protection
disappears the moment migration routing is implemented.

**D-2 — "The candidate detail page is unreachable."** The browser probe
found no `<a>` elements in table rows. **Wrong:** navigation is
`onClick` + `router.push` on the `<tr>`. The page is reachable and
renders correctly. (The rows are, separately, not keyboard-focusable —
noted in R-4.)

---

## Security findings

37/37 adversarial checks passed against the running application.

| Attack | Result |
|---|---|
| Anonymous access to all 13 protected routes | 401 on every one |
| Forged JWT (attacker-chosen secret) | 401 |
| `alg: none` JWT | 401 |
| Expired token | 401 |
| Refresh token used as access token | 401 (type checked) |
| Malformed / empty / `a.b.c` tokens | 401 |
| SQL injection via `?state`, `?service`, `?severity` | parameterized; table intact |
| Path traversal in candidate id | 404 |
| Malformed UUID | 422 |
| Oversized `?limit`, negative `?offset` | 422 |
| Empty / 100KB kill-switch reason | 422 |
| Error body leakage (traceback, driver, paths) | none |
| Refresh token reuse after logout | 401 (revoked server-side) |
| Secrets in git history (`gitleaks`, full history) | clean |
| CORS wildcard with credentials | not present; origins explicitly scoped |

**Known accepted:** an access token stays valid until expiry (≤15 min)
after logout — stateless JWT with no denylist. Standard tradeoff for a
single-operator tool; recorded in R-3.

---

## Database findings

- **VERIFIED:** every monetary column is `Numeric`; every monetary
  computation is `Decimal`. The only `float()` in the codebase is an ML
  probability, which is not money.
- **VERIFIED:** migrations apply cleanly from a completely empty
  database (0001 → 0008), `alembic check` reports no drift from the
  models, and the new 0008 is fully reversible (constraints drop and
  re-create).
- **VERIFIED:** PnL reconciles exactly against hand-computed values
  across long/short, winners/losers, flat, memecoin-scale (1e-9 prices
  with 1e9 quantity) and 100× moves — 7/7 exact, no precision drift.
- **FIXED:** `paper_positions` now carries `CHECK (entry_price > 0)` and
  `CHECK (quantity > 0)`.

---

## Performance measurements

Measured on this sandbox (shared CPU, local Postgres/Redis, Next.js in
**dev** mode — production builds are materially faster). Reported because
the brief asks for numbers, not adjectives.

| Operation | Measured |
|---|---|
| `GET /api/health` | ~3 ms |
| Authenticated list endpoint (12 rows) | ~10-25 ms |
| Login (argon2 verify, deliberately slow) | ~250-400 ms |
| `apps/api` full suite (62 tests, real DB) | 25.1 s |
| All 307 Python tests, 11 projects | ~67 s |
| Frontend production build | ~40 s |
| Dashboard page render (dev mode, cold) | 1-8 s |

Argon2 login latency is intentional. No bottleneck was found that would
matter at single-operator scale; the dominant cost everywhere else is the
database round trip.

**Resource usage:** not measured against the production droplet — no
droplet exists yet. The workload (13 containers, mostly idle polling
loops at 15-60s intervals, one hourly sklearn job on a logistic
regression) is modest. No evidence was found that would justify
recommending a larger droplet, and none is recommended.

---

## Deployment verification

| Item | Status |
|---|---|
| `nginx.conf` logic, headers, rate limit | **VERIFIED** against the real nginx binary |
| Shell scripts (`shellcheck`) | **VERIFIED** clean |
| Compose YAML parses; services/volumes correct | **VERIFIED** |
| Docker image build / `compose up` | **NOT VERIFIED** — Docker Hub unreachable here |
| Droplet provisioning, DNS, Let's Encrypt cert | **NOT VERIFIED** — no droplet, no DNS |
| `https://yonixalpha.com` | **NOT VERIFIED** — not deployed |
| Postgres/Redis not exposed publicly | **VERIFIED by inspection** of prod overlay (no host port bindings) |

---

## Remaining issues

### R-1 — ML activation retains a ~5-10% false-positive rate
*Component:* `services/ml/app/train.py` · *Impact:* a noise model can
still occasionally activate. Mitigated by: risk has final authority, the
`DEGRADED` cap is re-applied after ML blending, and activation requires
beating the incumbent's AUC. *Recommended:* require significance across
two consecutive training runs, or raise `AUC_CONFIDENCE_Z`, once real
labels exist to tune against.

### R-2 — Kill switch still has no durable store
*Component:* `yonixalpha_core/kill_switch.py` · *Impact:* AOF narrows the
loss window to ~1s, but the failure direction is still open. *Recommended:*
write kill-switch state to Postgres as the system of record and keep
Redis as a read cache; on Redis miss, fail **closed** (treat as engaged)
rather than open.

### R-3 — Access tokens are not revocable before expiry
*Component:* `apps/api/app/api/deps.py` · *Impact:* ≤15 min window after
logout. *Recommended:* a Redis `jti` denylist on logout if the window
ever becomes unacceptable.

### R-4 — Candidate rows are not keyboard accessible
*Component:* `apps/web/app/dashboard/candidates/page.tsx` · *Impact:* rows
navigate via `onClick` with no `<a href>`, `tabIndex` or `role`, so they
cannot be reached by keyboard and cannot be opened in a new tab.
*Recommended:* wrap the first cell in a `next/link` anchor.

### R-5 — `next`/`postcss` advisories remain unpatched
*Component:* `apps/web` · *Impact:* build-time only; PostCSS processes
first-party CSS, so neither advisory's attack surface (untrusted CSS)
exists here. Unchanged from Phase 10; fix requires a major Next.js bump.

### R-6 — Execution layer does not exist
*Component:* `execution_router` → provider · *Impact:* **this is what
blocks live trading.** No Jupiter or bonding-curve executor was ever
built, and `place_order_idempotent()` has no caller. *Recommended:* treat
as the next phase, and keep C-2's fail-closed limits in place until
position sizing exists.

---

## End-to-end paper-trading test (19/19 passed)

The full pipeline driven through the real services, real database and
real Redis, in paper mode with both trading flags off:

```
[safety]   TRADING_ENABLED=false, LIVE_TRADING_ENABLED=false     ok
[1] discovery      token + candidate created in DISCOVERED        ok
[2] decision       strategy_signals written           decision=NO_TRADE
                   risk_events written (audit trail)  approved=False
                   ml_features written, label NULL (no look-ahead)
                   risk refused to trade, reasons:
                     ['TRADING_ENABLED is false', 'LIVE_TRADING_ENABLED is false']
[3] paper entry    position opened, candidate -> MANAGING         ok
[4] management     real manage loop closed exactly 1 position     ok
[5] pnl + label    exit at the 2.40 TP level, not the 2.50 print  ok
                   realized_pnl  = (2.40-2.00)*50 = 20            ok
                   realized_pct  = 20/100 = 0.2                   ok
                   candidate     -> CLOSED                        ok
                   ml_features.label backfilled = 1               ok
[6] restart        re-run after restart is a no-op (idempotent)   ok
                   exactly one position, no duplicate             ok
                   PnL unchanged after restart                    ok
```

Two behaviours worth calling out, both correct:

- The risk engine refused the trade for exactly the right reasons, and
  said so in the persisted audit trail rather than failing silently.
- The exit filled at the configured take-profit level (2.40), not the
  overshoot price that triggered it (2.50) — the conservative simulation
  assumption, correctly applied.

---

## Final verification run

```
307 Python tests passed across 11 projects   (was 281 pre-audit; +26 regression tests)
11/11 projects ruff-clean
Frontend: typecheck clean, lint clean, production build succeeded
Shell:    shellcheck clean
Migrations: 0001 -> 0008 from empty DB, alembic check clean, 0008 reversible
Secrets:  gitleaks clean across full history
```

---

## Final report

```
TOTAL COMPONENTS AUDITED : 21
TOTAL TESTS RUN          : 307 automated + 56 adversarial probe checks = 363
PASSED                   : 363
FAILED                   : 0 (at final run)
FIXED                    : 9 defects
REMAINING                : 6 issues (R-1 .. R-6), none blocking paper operation

CRITICAL ISSUES : 0 open  (2 found, 2 fixed)
HIGH ISSUES     : 0 open  (4 found, 4 fixed)
MEDIUM ISSUES   : 1 open  (R-2)          (3 found, 3 fixed/mitigated)
LOW ISSUES      : 5 open  (R-1, R-3, R-4, R-5, R-6 tracked above)

PRODUCTION READY : CONDITIONAL
LIVE TRADING     : DISABLED
```

**Conditional on what.** Deploying today for **observation and paper
trading** is reasonable: the data pipeline, dashboard, auth, risk engine
and state handling are sound and tested.

**Live trading must not be enabled**, and cannot meaningfully be — R-6
means there is no execution path at all. Before it could be: build the
executor, implement position sizing and realized-PnL tracking so C-2's
limits can enforce instead of block, move the kill switch to durable
storage (R-2), and set a real `PAPER_TRADING_PER_LEG_COST_BPS` so paper
results and ML labels reflect net rather than gross outcomes.

The most valuable thing this audit found is not any single defect. It is
that three of the four capital-protection limits an operator was
instructed to configure could not fire, while appearing to. That class of
failure — a safety control that is present, documented, and inert — is
the one most likely to be trusted precisely when it matters.
