# YONIXALPHA - Risk Control, SOL-Based Settings and Helius Optimization (2026-10-13)

## Scope and status

- Solana-only. BSC, Robinhood and copy trading were not touched and stay disabled.
- LIVE auto-entry stays disabled: no environment lock, mode or switch was changed.
- No trade was placed. Nothing is deployed until the operator deploys.
- Every number below marked VERIFIED comes from server output pasted by the operator in this session (see `YONIXALPHA_HISTORICAL_PROFIT_AND_REGRESSION_VERIFICATION.md`, not committed) or from this repository and its tests.

## 1. Root causes: confirmed and corrected

| Finding | Verified by | Correction |
|---|---|---|
| GLOBAL save `save_for_all_engines` removed per-engine overrides of the changed keys (2026-10-09 23:29: GLOBAL v11, solana_fresh v10 and solana_migration v5 in one transaction). Migration lost its 0.01 SOL size cap but kept its 0.02 SOL daily limit | code (`store.save_for_all_engines`), identical row timestamps (T12a) | Engine values are **kept** unless the operator ticks "also remove the engines' own values" (`remove_engine_values`, explicit). The response names the engines that keep their own value. Regression test `test_global_save_cannot_remove_the_migration_fixed_sol_size_cap` |
| Saving one field could reset the others to code defaults (a partial payload was parsed against defaults) | code (`settings_from_dict` fills defaults) | `save_settings` merges the payload over the scope's current effective values. Test `test_saving_one_field_keeps_every_other_field` |
| Sizing was always a percentage of a shared ~9.62 SOL paper book. A single migration stop (0.0968 SOL) exceeded the 0.02 SOL daily limit | T12a (`max_loss` 0.0962 = 1% of 9.62; gate message "daily realized PnL -0.0968 hit max_daily_loss 0.02") | New `FIXED_SOL` sizing and an absolute `MAX_LOSS_PER_TRADE_SOL` (section 4). Equity never changes a fixed size |
| The daily loss limit was per engine but counted the whole shared account, so momentum's losses blocked migration (Oct 10 and 11) | code (`account_state`), F11 / T12b | Two explicit levels: **account** limit (GLOBAL `max_daily_loss_quote` = MAX_DAILY_LOSS_SOL, every strategy counted) and **strategy** limit (an engine's own value counts only that engine's realized losses) |
| Token data (≥6 RPC calls) was fetched before the daily-limit refusal. 12,550 of 12,782 migration evaluations on Oct 12 were such refusals | code order (`gate_eval`), T12a | Account-level blockers run **before** any token data is fetched (section 6) |
| Momentum lost in LIVE throughout (33 trades, PF 0.05) | R10 | `solana_momentum` excluded from LIVE entries in code (`gate.LIVE_SUSPENDED_ENGINES`). PAPER is unaffected |
| Fresh-strategy profit came from tokens that had already migrated | R10 stage table | Every plan, and so every position, records `token_category` (FRESH / NEAR_MIGRATION / MIGRATED / MOMENTUM), the token's own state at the decision |
| Two strategies could hold the same token (exposure cap, not a block) | code | `TOKEN_ALREADY_HELD`: one open position per token per account, across strategies |

## 2. Changed files

| File | Why |
|---|---|
| `packages/core-py/yonixalpha_core/safety/settings.py` | `trade_size_mode`, `fixed_trade_size_sol`, `max_loss_per_trade_sol`, `fixed_size_policy`; `sizing_errors` validation (conflicts, negatives, sub-lamport precision, size outside min/max position size) |
| `.../safety/planning.py` | Max loss from SOL setting or equity %; `_fixed_size` (fixed amount, every cap and the loss limit checked; REJECT or reported REDUCE); `planned_loss_at_stop`, `token_category` on the plan |
| `.../safety/gate.py` | `account_blockers` (token-independent hard checks); account vs strategy daily limits; `INSUFFICIENT_BALANCE`; `TOKEN_ALREADY_HELD`; `STRATEGY_NOT_LIVE_ELIGIBLE`; `token_category` |
| `.../safety/models.py` | `AccountState`: strategy daily PnL, account daily limit, account name |
| `.../safety/store.py` | Partial saves merge; GLOBAL saves keep engine values unless explicitly removed; strategy-own daily PnL |
| `.../safety/pipeline.py` | Loads the account limit from GLOBAL and the strategy's own PnL for every decision |
| `.../solana/rpc.py` | Coalescing of identical concurrent reads (never sends); aggregated usage counters; flush at most once a minute |
| `.../solana/assembler.py` | Short reuse of mint and holder reads that keeps the original observation time, always below `max_data_age_seconds` |
| `.../rpc_usage.py` (new) | Hourly usage hashes, entry-blocked counters |
| `.../risk_summary.py` (new) | Dashboard risk state computed by the gate's own functions |
| `.../tools/rpc_usage.py` (new) | Read-only usage report (call counts, not credits) |
| `.../runtime_watch.py` | Labels each service's RPC counters |
| `services/decision-engine/app/gate_eval.py` | Account pre-check before token data; 60 s recheck while blocked; reason stored on the candidate |
| `apps/api/app/api/routes/control.py`, `schemas/control.py` | `GET /api/control/risk-summary`; saves return the authoritative `effective` settings; `remove_engine_values` |
| `apps/web/components/RiskSummary.tsx` (new), `app/dashboard/page.tsx`, `risk/page.tsx`, `risk-settings/page.tsx` | Risk panel (dashboard + Risk pages); "Trade size & loss limits (SOL)" section; explicit removal checkbox; save message shows the stored values |
| Tests | `test_sol_sizing.py`, `test_rpc_usage.py` (new); `test_safety_store.py`, `test_safety_gate.py` (updated); decision-engine `test_sol_risk_settings.py` (new), `test_gate_eval.py`; paper-trading `test_gate_manage.py`; api `test_config_control.py` |

## 3. Database migration

**None needed.** The new settings are keys of the existing versioned `risk_settings.settings` JSON (append-only versions, audited). `token_category` and `planned_loss_at_stop` are keys of the existing `paper_positions.plan` JSON. Nothing is deleted or rewritten, and older rows read with defaults:

- `trade_size_mode` RISK_PCT (the previous behaviour);
- `max_loss_per_trade_sol` 0 (not set).

Rollback needs no schema step.

## 4. The SOL settings and their persistence path

| Setting (dashboard key) | Meaning | Default |
|---|---|---|
| TRADE_SIZE_MODE (`trade_size_mode`) | `FIXED_SOL` or `RISK_PCT` | RISK_PCT (unchanged behaviour until you choose FIXED_SOL) |
| FIXED_TRADE_SIZE_SOL (`fixed_trade_size_sol`) | SOL per entry. Up to 9 decimals. Must be within min/max position size | 0 (required in FIXED_SOL) |
| MAX_LOSS_PER_TRADE_SOL (`max_loss_per_trade_sol`) | Planned loss at the stop, fees included. Required in FIXED_SOL; in RISK_PCT it caps equity × risk % | 0 (not set) |
| policy (`fixed_size_policy`) | REJECT: refuse with the reason. REDUCE: trade the largest size that fits, every reduction reported. Never enlarged | REJECT |
| MAX_DAILY_LOSS_SOL (`max_daily_loss_quote`) | On GLOBAL: the account's realized-loss limit since 00:00 UTC, all strategies. On a strategy: its own losses only | 0.5 (GLOBAL), unchanged |

Persistence path, end to end:

1. Risk Settings page.
2. `PUT /api/control/settings/{scope}` or `POST /settings-all`.
3. `store.save_settings` (validate, clamp, merge, append a version, audit `risk_settings.updated` with a diff).
4. The response returns `effective`, read back after the commit. The page shows it and reloads from GET.
5. Every engine reads `store.load_settings(engine)` (GLOBAL plus the engine's own values) on each evaluation.
6. `pipeline.load_controls` → `assess` → `plan_trade`.
7. The paper or live position stores `plan.position_size` (value and method), `max_loss`, `planned_loss_at_stop` and `token_category`.

## 5. Proof that the engine consumes the saved values (tests, all run)

- **Fixed size used in a paper entry:** `test_saved_fixed_sol_amount_is_what_the_next_paper_entry_uses`. Saved 0.03 SOL; the gate plan is 0.03 and the paper position's entry cost is 0.03 + the LIVE buy fee, on a ~10 SOL book.
- **Equity never changes the size:** `test_fixed_sol_size_is_used_exactly_and_never_scaled_by_equity` (equity 1 / 10 / 100 → 0.05).
- **Refusals with reasons:**
  - `test_fixed_size_above_the_loss_limit_is_refused_with_the_reason` (TRADE_SIZE_EXCEEDS_RISK);
  - `..._above_the_pool_cap_is_refused_never_reduced`;
  - `test_insufficient_balance_is_a_precise_refusal_never_a_smaller_trade`;
  - `test_soft_warning_reduction_needs_policy_reduce`;
  - `test_a_conflicting_manual_size_is_refused_not_silently_chosen`.
- **Daily limits:**
  - `test_account_daily_limit_counts_every_strategy_and_fetches_no_token_data`;
  - `test_strategy_own_daily_limit_counts_only_its_own_losses`;
  - `test_strategy_own_daily_limit_blocks_that_strategy`;
  - `test_manual_buy_cannot_bypass_the_daily_limit`.
- **Exits continue:** `test_open_positions_keep_their_stops_after_the_daily_limit_blocks_new_entries`.
- **API round trip:** `test_sol_sizing_saved_from_the_dashboard_is_returned_and_used_by_the_risk_summary`. Save → response → reload → risk summary all agree; a conflicting value gets 422 and nothing changes.
- **Lamports:** `test_sol_to_lamports_is_exact`. LIVE already uses Decimal and `ROUND_DOWN` (`live_trading.enter_live`).

## 6. Helius request metrics

**Order of operations now:**
1. stale expiry
2. pacing
3. migration hand-off
4. strategy OFF
5. account state (DB)
6. **account blockers**: kill switch, operator controls, gas, max open positions, account and strategy daily limits, loss cooldown, FIXED_SOL balance
7. token data (RPC)
8. full assessment

A token-specific check stays after the data it needs.

**Before vs after:**

| | Before | After | Label |
|---|---|---|---|
| RPC calls of one refused-by-account evaluation | one full assembly. In the test harness one fresh evaluation made **11 calls** with cold caches (2 getAccountInfo, 6 getSignaturesForAddress, 1 getProgramAccounts, 1 getTokenLargestAccounts, 1 getMultipleAccounts) | **0** (`rpc.calls == []` in the tests) | MEASURED (tests) |
| Re-check interval while the account is blocked | 30 s (events down to 10 s) | 60 s | code |
| Production share of evaluations that were account refusals | Oct 12: 12,550 of 12,782 migration evaluations (98%) | not yet measured | VERIFIED before / pending after |
| Identical concurrent reads | each sent | one request, copies to each caller (`coalesced` counter) | MEASURED (tests) |
| Mint / holder reads between evaluations of a token | every evaluation | reused up to 45 s / 15 s, never longer than `max_data_age_seconds - 10`, original observation time kept (stale data still refused by the gate) | code + tests |

**Counters:** `python -m yonixalpha_core.tools.rpc_usage` reports the following per service, endpoint (by name, never URL) and method:
- sent / ok / failed / rejected / 429 / failover retries / coalesced / shed;
- average latency;
- critical / normal / background split;
- cache hit / miss;
- entries refused before token data.

These are call counts, not credits. Production "after" numbers exist once this version runs; compare them with the Helius dashboard.

**Not changed (by design):** WebSocket subscriptions, provider routing, transaction routes, venue probe, funding and creator checks (already cached), paid services.

## 7. Strategy and token-category corrections

- `post_migration_flow v1` is unchanged (no code evidence for a change); it stays in PAPER.
- Momentum cannot enter LIVE while `LIVE_SUSPENDED_ENGINES` lists it. PAPER evaluation and history are kept.
- Every new position records `token_category`. A fresh-engine trade on an already-migrated token is recorded as MIGRATED, never as proof of bonding-curve profitability.
- One open position per token per account across strategies (`TOKEN_ALREADY_HELD`).
- Every trade already records:
  - strategy and version (`strategy`, `feature_version`);
  - risk decision (`assessment_id`);
  - entry plan and quote (`plan`);
  - exit reason;
  - net realized PnL (`realized_pnl` = proceeds after every fee − entry cost including fees).

**Paper realism (audited, unchanged here):**
- Paper already charges swap fees and impact on the curve or pool model, the slippage allowance, and the LIVE fixed network, priority and rent costs (#53).
- It also applies the measured LIVE entry/exit drift (#62), partial sells and the sellable-remainder protection (#66).
- Remaining gaps are listed in section 12.

## 8. Test results (run in this session)

Full suite `allsuites.sh`: ruff, core-py, api, and all nine services. Results are pasted in the PR description from the actual run; any failure is listed there, not hidden.

## 9. Build

- `tsc --noEmit`: clean.
- `next lint`: no warnings or errors.
- The production web build runs in CI (`web` job).

## 10. Deployment plan

1. Merge after CI is green. Then deploy on the server: `cd /opt/yonixalpha && scripts/deploy.sh`.
2. Check: `python -m yonixalpha_core.tools.rpc_usage --hours 1` (counters appear) and the dashboard Risk limits panel.
3. Choose your values on Risk Settings → GLOBAL → "Trade size & loss limits (SOL)":
   - `trade_size_mode` FIXED_SOL;
   - `fixed_trade_size_sol`;
   - `max_loss_per_trade_sol`;
   - `max_daily_loss_quote`.

   Save, and confirm that the saved-values message and the panel show them.
4. Engines with their own values (e.g. migration's 0.02 SOL daily limit) keep them. The panel lists them; use "Use GLOBAL" only if you want them gone.
5. LIVE stays off. Enabling it is a separate, explicit decision.

## 11. Rollback

- Code: redeploy the previous main commit (`git checkout 883eb6a` on the server, then `scripts/deploy.sh`).
- Settings: the new keys are ignored by the old code, and the old sizing applies again.
- No schema to roll back. Saved settings versions remain in history.

## 12. Remaining risks and unverified items

- **Helius:** credits per method and the billing period are UNVERIFIED. Pricing pages were unreachable from this environment.
- **Production effect:** the after-metrics need the deploy. Coalescing and caching gains depend on the real evaluation timing.
- **FIXED_SOL with policy REJECT refuses every trade with a soft-warning size reduction.** On Oct 11, HIGH_VOLATILITY (REDUCE_SIZE) appeared in about 23k fresh evaluations. Choose REDUCE if smaller trades are acceptable.
- **The migration engine's own 0.02 SOL daily limit is still in its settings.** It now counts only migration's own losses. Momentum and fresh losses count against the GLOBAL account limit only, which is a deliberate change from the shared-PnL behaviour.
- **Paper realism:**
  - quote freshness at fill is not separately simulated beyond the entry delay;
  - failed paper transactions are not simulated;
  - the drift charge is 0 while there are no recent LIVE fills to measure.
- **33 open or stuck LIVE positions (Oct 10)** still need reconciliation, separately.
- **Strategy evidence:** no strategy has a proven edge. Migration without Oct 6 was net negative.
