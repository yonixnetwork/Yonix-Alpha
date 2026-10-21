# Live control, momentum pipeline and PnL accounting: audit (2026-10-21)

Audit first: this records what the code does, what was changed, and what still needs server evidence. Labels: **VERIFIED (code)** means read in the code and covered by tests; **NEEDS SERVER DATA** means the answer depends on the production database, and the command that answers it is given.

## 1. Live Trading control

### 1.1 The path (VERIFIED, code)

| Step | Where | What decides |
|---|---|---|
| Button | `apps/web/app/dashboard/settings/page.tsx`, Execution: MANUAL / PAPER / LIVE | The LIVE button is **disabled** when `env.live_permitted` is false, from `GET /api/control/modes`. |
| API | `PUT /api/control/modes/global` (`apps/api/app/api/routes/control.py`) | Refuses LIVE with 409 unless `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=true` and `PAPER_TRADING=false`, read from the API container's environment. Also refuses when a strategy in AUTO/MANUAL lacks its live configuration (wallet key). |
| State | `platform_settings` (global mode), `strategy_configs` (per-strategy mode) | Persisted and audited. |
| Gate | `safety/gate._live_target` | A decision goes LIVE only with global LIVE, the strategy AUTO/MANUAL, the locks open and the live worker ready. |
| Worker | `services/paper-trading/app/live_worker.py` | Reads the same locks from **its own** container environment. It publishes `ready` / `disabled: environment locks closed` to Redis. |

### 1.2 Why activation fails (VERIFIED, code; server state NEEDS SERVER DATA)

- The switch cannot activate while any `.env` lock is closed. This is the safety policy working, not a defect. The last known server configuration was `LIVE_TRADING_ENABLED=false` and `PAPER_TRADING=true`. With that configuration the LIVE button is disabled and the API refuses it.
- **The defect:** the dashboard gave no reason. The button was greyed out with only a generic note, and a refused attempt left no record.
- **A second failure mode, now detected:** `.env` is read when a container starts. Editing `.env` and then running `docker compose restart` keeps the old values. The API and the order worker can then disagree. The new status reports this as `STALE_DEPLOYMENT`.

### 1.3 What changed

- `yonixalpha_core/live_activation.py` and `GET /api/control/live-activation` return one of four states:

  | State | Meaning |
  |---|---|
  | `LOCKED` | The switch is refused. |
  | `READY_TO_ACTIVATE` | Every check passes; the global mode is not LIVE yet. |
  | `ACTIVE_NOT_READY` | The switch is accepted, but live orders are still blocked. |
  | `ACTIVE` | Live orders are possible; this says nothing about whether a trade is due. |

  Each blocker comes with a category and an action:

  | Category | Meaning |
  |---|---|
  | `LOCKED_BY_SAFETY_POLICY` | `.env` locks, kill switch, or an entry switch is closed. |
  | `WALLET_NOT_CONFIGURED` | `WALLET_PRIVATE_KEY` is missing. |
  | `SIGNING_UNAVAILABLE` | The key is present but unusable, or the worker has no signer. |
  | `PROVIDER_DISCONNECTED` | No RPC, worker not reporting, or wallet sync stale. |
  | `STRATEGY_DISABLED` | No strategy is in AUTO or MANUAL. |
  | `CONFIGURATION_ERROR` | A strategy that would trade live lacks its configuration. |
  | `INSUFFICIENT_BALANCE` | The wallet is not synced, or is at or below the reserve. |
  | `STALE_DEPLOYMENT` | The API and the worker read different locks. |

  No secret is returned.
- A refused switch returns its blockers in the 409 response and is audited as `global_mode.refused`.
- Settings and Live Execution show this panel. The LIVE button's tooltip says why it is disabled.
- Nothing opens a lock or bypasses the risk engine. The locks stay server-only.

**Server check (read-only):** `python -m yonixalpha_core.tools.live_check` (existing), or the panel on Settings.

## 2. Momentum: why no trades

### 2.1 Pipeline and evidence (VERIFIED, code)

`yonixalpha_core/momentum_pipeline.py` (API `GET /api/strategies/solana_momentum/pipeline`, tool `tools.momentum_pipeline`) counts each stage from recorded data:

1. **Stream**: from scanner_health.
2. **Scan**: tokens active in the last 2 minutes. New per-hour Redis counters record no curve address, curve done, and too young.
3. **Prefilter**: failed or passed.
4. **Candidate**: created, slot budget full, already promoted, or WATCH row.
5. **Gate decisions** and their refusal codes.
6. **Positions.**
7. **Refused or failed live orders.**

It returns the brief's A-H verdicts. A disconnected or stale stream is a `SYSTEM` finding, never "no opportunity". When nothing was recorded it says `NO_EVIDENCE`, not "no opportunity".

The Momentum page shows the verdict, the stage counts, and the latest momentum candidates with the stage each reached and its final blocker. You can search them, and each links to the token page.

### 2.2 What the code already shows (VERIFIED, code)

- **Mode OFF** creates no momentum candidate. With `monitor_when_off`, prefilter passes are recorded as WATCH rows only (verdict G).
- **Mode PAPER/AUTO**: momentum candidates share the slot budget with fresh-token candidates. With the budget full, a prefilter pass is dropped. This is now counted as `momentum_budget_full` (verdict D, `SYSTEM`).
- The 2026-10-20 replay found no momentum gate decision after 2026-10-11. That matches momentum being OFF since then.

### 2.3 Which case applies now: NEEDS SERVER DATA

Run after the deploy. The prefilter counters start at the deploy, so allow an hour or more:

```
python -m yonixalpha_core.tools.momentum_pipeline --hours 6
```

## 3. Late entries: NEEDS SERVER DATA before any change

The existing `tools.entry_timing` measures detection, promotion, the first gate evaluation, gate wait, queue, quote, signing and landing per candidate. It also measures the price change between detection, first signal and entry, and whether the token was already decelerating. No entry timing is changed until it has been read. A comparison of earliest valid entry, current, later and no-trade-if-extended entries will be built on the opportunity ledger only if those numbers show a delay worth testing.

## 4. PnL accounting and token-account rent (VERIFIED, code)

| Item | How it is booked |
|---|---|
| Swap / program fees, network and priority fee | Consumed. Part of the cost; itemised per order from the confirmed transaction (`execution_analysis`, `plan.fill.costs_sol`). |
| Token-account rent | A **deposit**, not a cost. It is paid at the first buy of a token and returned when the empty account is closed. Auto-reclaim after a full exit is on by default. The refund is credited to that position's proceeds, and the PnL is re-booked with an audit event (`pnl_rebooked`). |
| Cost basis | Everything the wallet paid, including the deposit, until it is returned. This is why small LIVE buys looked like they "never went up" against the cost basis. Highs and lows are market prices from the fill (`market_reference`). |
| Paper | Models the same items as estimates: buy and sell network fees, plus the close transaction or the unreclaimed rent. |
| LIVE account cash | The synced wallet balance (on-chain truth). Deposits made outside the bot are not in the ledger. |

**Correction to `MOMENTUM_STRATEGY_RESEARCH.md` §2.3 (confirmed by §4b):** the rent was reclaimed and is already in the booked result. The LIVE momentum loss is about 20 % consumed costs and about 80 % market: the entries rarely went up.

The new read-only `tools.pnl_reconcile` lists, per LIVE position:

- consumed costs;
- deposit;
- reclaimed and still-locked rent;
- booked and economic result;
- MFE against both the market price and the cost basis.

It also checks the paper account identity (starting + realized - open cost = cash). No historical row is changed: an existing, audited re-booking already applies when rent is reclaimed.

## 4b. Server evidence (2026-10-21 20:07 UTC, read-only tools)

**Live switch (`tools.live_check`).**

| Item | State |
|---|---|
| `TRADING_ENABLED` | true |
| `LIVE_TRADING_ENABLED` | **false** |
| `PAPER_TRADING` | **true** |
| Wallet key | configured |
| Global mode | PAPER |
| Strategies | fresh, migration and momentum all AUTO |
| Order worker | `disabled: environment locks closed` (correct for these locks) |

- **Cause:** the `.env` safety policy. This is not a code defect; the remaining defect was the silent UI, fixed in PR #92.
- **Still needed for LIVE, by explicit decision only:**
  - `LIVE_TRADING_ENABLED=true` and `PAPER_TRADING=false`, then recreate the containers;
  - global mode LIVE.
- **Warning:** the live wallet holds 0.103 SOL with a 0.05 SOL reserve.
- **Corrected tool output:** `live_check` also printed "20 executable decisions went to PAPER after the switch to LIVE". That line refers to an Oct 15 switch, and the global mode is PAPER now. The line now appears only while the global mode is LIVE.
- **Risk to note:** momentum is AUTO, not PAPER, so it would trade live the moment the locks and the global mode allow it. Setting it to PAPER keeps it paper-only whatever the global mode.

**Momentum (`tools.momentum_pipeline --hours 1`).** Verdict **D: a system cause, not "no opportunity"**.

| Stage | Count |
|---|---|
| Stream | CONNECTED |
| Token checks | 8500 |
| Reached the prefilter | 1419 (1315 failed, **104 passed**) |
| Candidates | **0**: all 104 dropped as `budget_full` |
| Gate decisions | 0 |

Two causes:

1. **Candidate slots.** Fresh and momentum share 20 of the 25 slots, and fresh candidates (up to 30 min each at the gate) held all of them.
   - Fix: momentum keeps a reserve of 3 slots while it is not OFF (`funnel.momentum_reserve`, 12 % of the budget). Fresh candidates cannot take the unused part. Fresh keeps its floor of 5 when migrations hold their 20.
   - A pass dropped for lack of a slot is now still recorded as a WATCH observation, so learning does not depend on slots.
2. **Metadata coverage.** 3731 of 8500 checks (44 %) found no curve address. Launch metadata expired 6 h after the launch even while the token kept trading, so established tokens were invisible.
   - Fix: every trade now refreshes the 6 h expiry, so an active token keeps its metadata. Tokens whose metadata already expired stay invisible.

These fixes let momentum be evaluated. They do not change its entry rules, which the 2026-10-20 research found have no edge.

**Late entries (`tools.entry_timing --hours 168`).**
- 40 entered tokens; 16 entered 50 %+ above the first-detection price.
- For migrated tokens much of that rise is by design: detection is the launch and the entry comes after migration.
- The gate waited a median 1588 s before the approving evaluation (n=6). The waits were mostly data (`AUTO_SL_NO_VOLATILITY` 1446: the stop cannot be set until volatility is measurable) and safety.
- Whether waiting costs money is measured by `tools.entry_compare` (new): earliest signal vs the gate entry for the same token, and the skip-when-extended rule chosen on the first 60 % and tested on the last 40 %.
- No timing rule is changed before that output is read.

**PnL and rent (`tools.pnl_reconcile --engine solana_momentum`).**

| Item | SOL |
|---|---|
| LIVE momentum positions | 44 (9 failed with no fill, 35 filled) |
| Spent | 0.169 |
| Consumed costs | 0.0044 (2.6 %) |
| Rent deposits | 0.0454; all reclaimed except 0.0015 (MCASH) |
| Booked | −0.0212 = economic −0.0212 |

- **The rent was not a loss.** It came back and is already in the booked result.
- About 0.0044 SOL of the loss is consumed costs; the rest is the market.
- Against the market fill price, about a third of the trades never rose at all (MFE 0.0 %), and the median MFE was about 2.5 %. The −40 % "MFE" in the earlier report was the cost basis (deposit + fees), not the market.
- **PAPER momentum:** booked −0.4924 SOL, of which modelled fees were 0.3277 SOL. Costs were two thirds of the paper loss.
- **Paper books:** the `solana` paper account reconciles exactly (difference 0). The archived `hyperliquid` account shows −15 (archived engine, not traded).

## 5. Not built yet

These follow the server evidence above:

- the ML Training Lab: observation queue, manual labels, idea-to-experiment form and dataset export;
- the Signal Center with immutable predictions;
- the entry-approach comparison.

What already exists and will be reused:

- the opportunity ledger: decision-time snapshots, outcomes T+5 s to T+60 m, executable returns;
- ML governance: no auto-activation, frozen validation sets, champion/challenger;
- the ML Review page;
- WATCH observation.

## 6. Safety

- Live trading stays locked by the server `.env`.
- Nothing in this change can open a lock, change a mode on its own, raise a size or bypass the gate.
- Momentum stays OFF unless you set it; observation never authorises a trade.
