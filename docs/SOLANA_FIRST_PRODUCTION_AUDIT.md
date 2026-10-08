# YONIXALPHA — Solana-first production audit (2026-10-08)

Every claim is marked:

- **VERIFIED**: shown by a test in this repository, by a command whose output is quoted, or by a server measurement the operator pasted.
- **NOT VERIFIED**: not demonstrated yet. The reason is given.
- **BLOCKED**: it cannot be demonstrated without something this work does not have, such as a funded live wallet or authorization.

Server figures come only from the operator's pasted output, with the time given. Nothing here is estimated.

## 1. Summary

Production now runs in `SYSTEM_PROFILE=SOLANA_ONLY`. The profile controls the backend, not just the dashboard:

- The BSC / Robinhood worker (`data-evm`) is not started.
- With `COPY_TRADING_ENABLED=false`, `copy-engine` is not started either.
- The ML service skips the BSC / Robinhood cycle and their frozen validation sets.
- The API refuses BSC / Robinhood orders with 409 CHAIN_DISABLED.
- System Health shows these parts as `DISABLED — SOLANA_ONLY MODE`, not as failures.

Nothing was deleted. Code, adapters, tables, history and settings are all kept. The change is reversed in `.env` and by running `scripts/deploy.sh`.

Solana trading logic, Pump.fun execution, PumpSwap execution, the transaction guard, risk limits and position sizes were **not changed**. A new read-only report compares Solana PAPER and LIVE results.

## 2. Before (measured)

| | 2026-10-07 (before PR #56/#57) | 2026-10-08 20:46 (after PR #58) |
|---|---|---|
| Load 1 / 5 / 15 min (2 vCPU) | 9.82 / 7.95 / 7.30 | 2.98 / 2.44 / 2.25 |
| RAM available | 345 MB | 513 MB |
| Swap used | 1 298 MB | 1 500 MB |
| CPU: postgres / data-evm / paper-trading / discovery / copy-engine | 37.6 / 27.4 / 3.2 / 10.2 / 60.7 % | 54 / 21 / 19 / 11 / 4.6 % |
| Solana pipeline | — | 323 launches/h, 343 decisions/h, median decision 382 ms |
| EVM trade feed | — | 17–21 s behind |

Source: operator output (docker stats, `db_health`). **VERIFIED** (as measured).

At 20:46, data-evm was the largest worker CPU cost after Postgres, at 21 % of one CPU. Its BSC log scans also drive much of Postgres's write load, because `evm_trades` is 3.7 GB.

## 3. Worker lifecycle audit

| Starter | What it starts | Finding |
|---|---|---|
| `infra/docker/docker-compose.yml` + `docker-compose.prod.yml` | All workers, `restart: unless-stopped` | data-evm and copy-engine had no profile and always ran. Now they are behind profiles `evm` and `copy`. **VERIFIED** (`docker compose config --services` without profiles lists 11 services, without data-evm or copy-engine; with `COMPOSE_PROFILES=evm,copy` both are listed) |
| `scripts/deploy.sh` | Builds the services in `config --services`, then `up -d --remove-orphans` | `--remove-orphans` does **not** stop a service whose profile is off, because it is still in the file. deploy.sh now stops and removes those containers explicitly (`rm -s -f`). **NOT VERIFIED on the server**: no Docker daemon in the build environment; checked at the first deploy (section 21) |
| `scripts/install-env-updater.sh` | systemd path unit that applies dashboard `.env` requests (`up -d`) | Now passes the same `COMPOSE_PROFILES`, so a disabled worker stays off. It starts no worker of its own. **VERIFIED** (code; `test_env_updates` passes) |
| `scripts/backup-db.sh` | Cron, documented, database dump only | Starts no worker. **VERIFIED** (code) |
| Legacy `engine-solana-momentum` / `engine-solana-migration` | Profile `legacy` only | Not started. **VERIFIED** (compose config) |
| Cron / systemd / supervisor on the server itself | Unknown | **NOT VERIFIED**: needs the server check in section 21 (`crontab -l`, `systemctl list-units`, `docker ps`) |

Duplicate workers: each worker runs as one container and no second starter exists in the repository. **VERIFIED for the repository. NOT VERIFIED for the server** until the section 21 output is seen.

## 4. System profile

`yonixalpha_core.system_profile`, settings in `config.py`, `.env.example`:

| Setting | Production | Effect |
|---|---|---|
| `SYSTEM_PROFILE` | `SOLANA_ONLY` | Only Solana runs. `MULTI_CHAIN` lets the `CHAIN_*` flags decide (unset = on) |
| `CHAIN_SOLANA_ENABLED` | `true` | The Solana stack holds the live positions and is never stopped by the profile. `false` is reported as not applied |
| `CHAIN_BSC_ENABLED`, `CHAIN_ROBINHOOD_ENABLED` | `false` | Ignored in SOLANA_ONLY |
| `COPY_TRADING_ENABLED` | `false` | copy-engine is not started. The copy status reads SUSPENDED and cannot be resumed from the dashboard (409 COPY_DISABLED) |
| `ML_DATASET_SCOPE`, `ML_MODEL_SCOPE` | `SOLANA` | No BSC / Robinhood training or validation |

`scripts/compose-profiles.sh` applies the same rules to `.env` on the host, without Python. A test runs it on six env files and compares the result with `compose_profiles()`. **VERIFIED** (`test_system_profile.py`, 12 tests).

## 5. What SOLANA_ONLY stops, and what it keeps

| Stopped | How | Evidence |
|---|---|---|
| BSC / Robinhood discovery, log scans, safety, paper entries, streams (data-evm) | Not started. Started by hand, it idles with a "disabled" heartbeat | **VERIFIED** (compose config; `test_a_disabled_worker_started_anyway_only_heartbeats_disabled`) |
| Copy trading on every chain (copy-engine) | Not started, or idles if started | **VERIFIED** (same; `test_copy_policy_follows_the_operating_mode` shows COPY_TRADING_ENABLED=false gives SUSPENDED / PROTECT_ONLY even with ACTIVE stored) |
| BSC / Robinhood copy watching, if copy trading is switched on under SOLANA_ONLY | `evm_chains()` is empty | **VERIFIED** (`test_solana_only_profile_never_watches_or_manages_evm_copies`) |
| EVM ML cycle (samples, labels, shadow models, wallet ML) | Step `evm_wallet_ml` SKIPPED with the reason | **VERIFIED** (`test_solana_only_profile_skips_the_evm_ml_cycle_and_its_frozen_sets`) |
| EVM frozen validation sets | Only `solana_opportunity` and `solana_candidate` are frozen and scored | **VERIFIED** (same test) |
| Manual BSC / Robinhood BUY, SELL of an EVM position, new EVM copy targets | 409 CHAIN_DISABLED with the reason | **VERIFIED** (`test_manual_evm_buy...`, `test_sell_of_an_evm_position_is_refused...`, `test_copy_targets_profiles_and_events`) |

Kept, unchanged:

- Solana discovery, the gate, paper and live execution, position management, exits, the risk engine, migration handling, reconciliation, Solana ML training and inference.
- Every table and row. The BSC / Robinhood code and adapters.

**Open EVM paper positions are frozen.** They are not managed while data-evm is off: no stop loss, no exit. They are paper only. EVM live execution is locked (task #178), so no funds are at risk. Their manual SELL is refused with the reason, so nothing waits for a worker that is not running. They resume management when the chain is switched back on.

## 6. System Health

- Health state `DISABLED`, which is not counted in the overall state, like NOT CONFIGURED.
- data-evm reads `DISABLED — SOLANA_ONLY MODE: BSC and Robinhood are not scanned or traded`.
- copy-engine reads `DISABLED: COPY_TRADING_ENABLED=false`.
- If a disabled worker is still running from an old container, it reads DEGRADED: "running although the system profile switches it off: run scripts/deploy.sh".
- Server resources shows the profile, and lists disabled workers as DISABLED.
- BSC, Robinhood, EVM Markets and Copy Trading pages show a banner explaining the switch-off.

**VERIFIED** (`test_health_states_from_evidence`, `test_copy_trading_disabled_by_the_profile_is_never_resumed`; web typecheck, lint and build).

## 7. Solana focus

Discovery uses event streams:

- The Pump program log stream (`pump_stream`) and PumpPortal WebSocket, with a stream-vs-logs coverage check.
- Gate decisions are made on shared market data in Redis.
- No full table scan sits on the decision path.

**VERIFIED** (code and existing tests; server: 323 launches/h and median decision 382 ms at 20:46).

Stages traded: FRESH (bonding curve), NEAR_MIGRATION (curve progress at least 0.70 at the decision), MIGRATED (PumpSwap canonical pool) and MOMENTUM. They are reported separately in section 14.

## 8. Migration handling (event-driven)

| Requirement | Implementation | Status |
|---|---|---|
| Migration detected from the stream while a curve position is open | The curve state in Redis turns `complete` with a pool. The position switches PRE_MIGRATION to POST_MIGRATION, is priced from the pool, and sells on PumpSwap | **VERIFIED** (`test_curve_position_keeps_managing_through_migration_and_sells_on_pumpswap`) |
| A curve sell rejected because the curve completed (Pump error 6005) | Not counted as an exit failure, so slippage does not widen. The position moves to the canonical PumpSwap pool, and the next sell is a PumpSwap sell | **VERIFIED** (new test `test_curve_complete_sell_rejection_moves_the_exit_to_pumpswap`) |
| Refresh before every exit | Every live order resolves the venue right before building: curve, PumpSwap pool, Jupiter route, or MIGRATION_IN_PROGRESS. It resolves again after building; if the venue changed, it rebuilds; if it is still unstable, it is not sent (VENUE_UNSTABLE) | **VERIFIED** (code `solana/live_exec.py`; `test_execution_venues.py`) |
| Token account and liquidity at sell | The sell amount comes from the position's held amount; the pool is read on resolve; the guard checks the wallet's token delta | **VERIFIED** (existing tests `test_live_worker.py`, `test_pumpswap_sell_check.py`) |
| On a real chain | — | **BLOCKED**: needs an authorized live trade through a migration. A 2026-09-28 production record (comment in `live_trading.curve_complete_rejection`) shows a 6005 sell followed by a confirmed PumpSwap sell; that predates this change and was not re-run |

Error 6005 = BondingCurveComplete comes from the earlier research record (`docs/PUMPFUN_EXECUTION_RESEARCH.md`) and that production record. A quick search on 2026-10-08 confirmed the program addresses and the PumpSwap migration path, but not the error table itself. **NOT VERIFIED externally in this change.**

## 9. Execution path

No change to the order path, the PumpPortal / native builders, the transaction guard, slippage limits, priority fees or sizing. **VERIFIED** (`git diff --stat origin/main`: `live_trading.py`, `solana/`, `safety/` and `execution/` are not modified; the new 6005 test exercises the existing code).

PAPER is the default and LIVE needs both environment locks. **VERIFIED** (unchanged tests).

## 10. Resource priorities

| Priority | Work | In SOLANA_ONLY |
|---|---|---|
| 1, never paused | Positions, exits, risk, execution, migration, Solana discovery, safety, reconciliation | Running |
| 2 | Signals, ML inference, paper trading, dashboard | Running |
| 3 | ML training (waits only while CRITICAL), ML Review, analytics | Running, bounded |
| Off | data-evm, copy-engine, EVM ML | Not started |

Training skipped under pressure is recorded as `SKIPPED - RESOURCE PRESSURE ...` (PR #57/#58). **VERIFIED** (`test_low_resource_schedule.py`).

## 11. ML (Solana scope)

- **Scope.** Solana training reads only Solana engines' samples (`ENGINES_FOR_MODEL`). With ML_MODEL_SCOPE=SOLANA, no BSC / Robinhood model is trained or validated. **VERIFIED** (code; section 5 test).
- **Training data.** The opportunity ledger covers traded, rejected and missed outcomes, with lifecycle, executable returns and counterfactuals (task #161). **VERIFIED** (existing tests).
- **Validation.** Chronological and frozen-window validation on data the model never saw. Feature ablation A–F. Activity-matched validation (tasks #167, #210). **VERIFIED** (`test_validation.py`, `test_ablation.py`).
- **Governance.** Stages are OBSERVATION_ONLY, SHADOW, PAPER_CONTRIBUTOR and LIVE_CONTRIBUTOR (locked). Contribution starts at 0 %, and nothing is promoted automatically (`ml.governance`). The prompt's names map as follows:
  - LEARNING / VALIDATING = SHADOW plus frozen validation;
  - PAPER_VALIDATED = PAPER_CONTRIBUTOR;
  - PRODUCTION_CONTRIBUTOR = LIVE_CONTRIBUTOR, which is locked;
  - RETIRED = an inactive model version, kept and never overwritten.

  **VERIFIED** (`test_ml_governance.py`). Renaming the stages was not done, to keep stored rows valid.
- **ML cannot override safety.** ML scores enter only after the hard-block checks. **VERIFIED** (existing gate tests).
- **Predictive value.** **NOT VERIFIED**: no model has passed the frozen-set PASS rule on server data in the evidence available here.

## 12. Expected executable return and realistic paper

- Executable returns, not market-cap returns, are recorded per opportunity (task #161).
- Paper fills simulate fees, the network fee, account rent and failure rates (`paper_execution`, task #154).

**VERIFIED** (existing tests). Paper-vs-live parity is tracked by `tools/parity_report` (PAPER_VS_LIVE_PARITY_AUDIT.md). **VERIFIED** (tool runs on the server, output pasted 2026-10-08).

## 13. Copy trading

Disabled by `COPY_TRADING_ENABLED=false`, and copy-engine is not started. Targets, events, positions, profiles and settings are kept. Turning it back on still requires the resource resume check (PR #57). "A target wallet buying a token is not permission to buy it" is unchanged: every copy buy passes the gate. **VERIFIED** (tests in section 5).

## 14. Solana performance report (new)

`GET /api/analytics/solana-performance?days=1..90` (dashboard: Trading, then Solana Performance). The command-line version is `python -m yonixalpha_core.solana_performance --days N`. The report shows:

- Decisions by target: signals, qualified, rejected.
- Entries, exits and open positions by mode.
- Closed trades, PAPER and LIVE separately. For each: win rate, average and median win / loss, profit factor, net PnL, fees, max drawdown, average MFE / MAE and median hold.
- Those trade figures split by stage (FRESH / NEAR_MIGRATION / MIGRATED / MOMENTUM), by hold time (<10s, 10–30s, 30s–1m, 1–5m, 5–15m, 15–60m, >60m), by entry quality (CLEAN / DETERIORATING / UNKNOWN) and by exit reason.
- Missed winners by the rule that rejected them.
- False positives by loss classification.
- Exit timing classes.
- LIVE execution per side and route: median decision-to-submit, decision-to-confirm, slippage vs expected, and all-in vs decision.

The report is SQL aggregation over a bounded window, with one grouped pass over the closed positions. Groups under 20 trades are flagged anecdotal. Paper slippage is reported as not recorded per trade, rather than estimated. **VERIFIED** (`test_solana_performance_api.py`: seeded rows with hand-computed answers, including profit factor 0.75 and max drawdown 0.03 SOL). Server run time: **NOT VERIFIED** (section 21).

## 15. Paper vs live parity

Same gate, same sizing, same exits. Differences are recorded with their reasons (`parity_report`: block codes per target). **VERIFIED** (2026-10-08 output). LIVE sample: **NOT VERIFIED — funded live wallet required**. No LIVE trade was authorized in this work.

## 16. Execution telemetry per stage

The executor records each stage: VENUE_RESOLVED, TRANSACTION_BUILT, guard, signed, submitted, confirmed. `execution_analysis` derives latencies and price components per LIVE order (task #149). They are now aggregated per side and route in the report. **VERIFIED** (code and existing tests). Real latencies: **BLOCKED** — they need LIVE orders.

## 17. Automatic sell architecture

Unchanged: stop loss, trailing, take profit, exit signals, migration switch and kill switch. Manual SELL of Solana positions works as before; only EVM positions are refused while their chain is off. **VERIFIED** (all existing exit tests pass).

## 18. Security

- No key handling changed.
- New endpoints require login: `/system/profile` returns 401 without a token.
- The secret scanner runs on each commit.
- No credential is in this change.

**VERIFIED** (test; gitleaks on staged files).

## 19. Research used

- Pump.fun bonding-curve program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` and PumpSwap AMM `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`. Graduation migrates into the PumpSwap AMM, which has replaced Raydium since March 2025. Checked 2026-10-08 against [Solana Tracker program guide](https://docs.solanatracker.io/guides/pumpfun-program.md), [Solana Tracker AMM guide](https://docs.solanatracker.io/guides/pumpfun-amm) and [The Block](https://www.theblock.co/post/347360).
- PumpSwap fee and creator-share context (2026): [MadeOnSol](https://madeonsol.com/blog/what-is-pumpswap). Third-party; fees are read on chain by the code, never from this.
- Earlier research, reused rather than repeated: `docs/PUMPFUN_EXECUTION_RESEARCH.md` (official Pump docs and IDL, SDKs, Jupiter) and `docs/INTELLIGENCE_AUDIT_2026.md` R1–R8 (Mayhem mode, instant bonds, executable vs market-cap returns, causal creator history, wallet reputation).

Research is not a runtime dependency: nothing is scraped, and every value the code uses is read on chain.

## 20. Tests run

Full suite before the PR: ruff, core, api, and every service, plus web typecheck, lint and build. The results are listed in the PR. New tests:

- `test_system_profile.py` (12)
- `test_solana_performance_api.py`
- `test_curve_complete_sell_rejection_moves_the_exit_to_pumpswap`
- `test_solana_only_profile_never_watches_or_manages_evm_copies`
- `test_solana_only_profile_skips_the_evm_ml_cycle_and_its_frozen_sets`
- `test_sell_of_an_evm_position_is_refused_while_its_chain_is_off`
- `test_copy_trading_disabled_by_the_profile_is_never_resumed`

Five existing tests now set `SYSTEM_PROFILE=MULTI_CHAIN` or `COPY_TRADING_ENABLED=true` for the part that exercises those features, and each also asserts the new SOLANA_ONLY behaviour. No test was removed or weakened.

## 21. Deploy and measure

1. In `.env`, add or confirm: `SYSTEM_PROFILE=SOLANA_ONLY`, `COPY_TRADING_ENABLED=false`, `CHAIN_BSC_ENABLED=false` and `CHAIN_ROBINHOOD_ENABLED=false`. These are also the defaults when unset.
2. `scripts/deploy.sh` should print "optional workers on: none" and "Stopping data-evm", then "Stopping copy-engine".
3. Check: `docker ps --format '{{.Names}}'` should list no data-evm or copy-engine. Also run `crontab -l; systemctl list-units --type=service --state=running | grep -i yonix`.
4. Measure: run docker stats, `db_health --skip-timings` (it now prints the profile line), `parity_report --days 1` and `python -m yonixalpha_core.solana_performance --days 1`.

Results after the deploy: **NOT VERIFIED** until the operator's output is seen. To compare: load, RAM available, swap, Postgres CPU and the Solana decision median, against section 2.

### First deploy (2026-10-08 22:06, operator output)

- `db_health` showed the profile correctly: `system profile SOLANA_ONLY: chains solana; copy trading off; ML scope SOLANA; compose profiles none`, with data-evm and copy-engine listed as DISABLED. **VERIFIED**.
- But data-evm (40.6 % CPU, 128 MB) and copy-engine were **still running**, and deploy.sh printed neither "System profile" nor "Stopping". Cause: deploy.sh updates itself with `git merge` and bash keeps reading the file it opened, so the previous version's steps ran. Reproduced locally with a two-commit repository. **VERIFIED**.
- Fix: after pulling, deploy.sh re-runs the pulled version of itself (`DEPLOY_PULLED`). Also verified locally. The next deploy (the old script still runs once, but its steps already include the stop) stops both workers.
- Solana pipeline at 22:06: 149 launches and 159 decisions in the last hour, median decision 514 ms, load 3.37 with data-evm still at 40 %. Not yet comparable: measure again after the workers stop.
- ML steps all read SKIPPED. That is the hourly schedule after the restart (each step last ran 21:44). `db_health` now prints the reason and the last OK time.

## 22. Not done / open

- Server confirmation of the disabled workers and the resource saving: pending the section 21 output.
- LIVE results, live migration exit and live latencies: **BLOCKED** (funded, authorized live trading required).
- 7wmm sell failure root cause: still **NOT VERIFIED**. The proposals (second-RPC re-simulation, retry backoff) await operator approval.
- Manual BUY override (task #138): **BLOCKED** (unchanged).
- EVM live (task #178): off and locked (unchanged).
- ML stage names: mapped (section 11), not renamed.
