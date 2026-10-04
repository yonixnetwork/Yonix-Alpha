# Test matrix (master §79, §80)

Every scenario §79 names, the tests that cover it and what they prove. The
last column says what a test here cannot prove. Paths:
- `core:` means `packages/core-py/tests/`;
- `api:` means `apps/api/tests/`;
- `svc:<name>` means `services/<name>/tests/`.

The tests run against:
- a real Postgres and Redis (the same schema as production);
- fake RPC nodes / transports serving recorded or SDK-built chain data.

They prove the system's logic. They do not prove real-chain behaviour: no
test signs or sends a transaction. Solana live execution has its own
on-server tools (`verify_live`, `exec_dryrun`) and production history
(tracker section 3, M2). BSC and Robinhood are paper only; EVM live
execution is off and locked.

Full suite on 2026-10-04: core 851, api 159, data-solana 10, data-evm 13,
copy-engine 11, engine-solana-discovery 18, engine-solana-migration 11,
engine-solana-momentum 11, decision-engine 58, ml 43, paper-trading 80, all
passing. CI runs the same suites, plus lint, type check, dependency audit and
the secret scan, on every pull request.

## §79 scenarios

| Scenario | Tests | What they prove | Not proven here |
|---|---|---|---|
| Solana buy | `svc:paper-trading/test_live_worker.py::test_buy_request_is_bounded_and_opens_from_the_actual_fill`, `::test_unfilled_buy_never_opens_a_position`, `::test_migrated_live_entry_routes_to_pumpswap`; `core:test_pump_tx_sdk_parity.py::test_bonding_curve_buy_matches_the_official_sdk`, `::test_pumpswap_buy_matches_the_official_sdk`; `core:test_live_exec.py::test_confirmed_buy_reports_the_actual_fill_and_persists_signature_before_sending` | buy bounded by the guard; position opened from the actual fill only; transaction bytes equal to the official Pump SDKs; migrated tokens routed to PumpSwap | a fresh live buy on mainnet: production history only (live trades since 2026-09) |
| Solana sell | `core:test_pump_tx_sdk_parity.py::test_bonding_curve_sell_matches_the_official_sdk`, `::test_pumpswap_sell_matches_the_official_sdk`; `core:test_live_exec.py::test_guard_sell_bounds`; `svc:paper-trading/test_live_worker.py::test_curve_position_keeps_managing_through_migration_and_sells_on_pumpswap` | sell transactions equal to the SDKs; guard bounds; a curve position sells on PumpSwap after migration | as above |
| Solana automatic sell | `svc:paper-trading/test_exit_parity.py::test_automatic_and_manual_exits_sell_the_same_way_and_close_once`; `svc:paper-trading/test_live_worker.py::test_stop_loss_becomes_a_sell_and_pnl_comes_from_the_sell_fill`, `::test_gate_manage_turns_a_crash_into_a_live_sell_order`, `::test_failed_exit_is_retried_with_more_slippage` | stop loss and crash exits create one SELL; PnL from the fill; failed exits retried with wider slippage | production: `tools.exit_diagnosis` (M2: 72 automatic sells, 97.2 % confirmed, median 3.1 s) |
| Solana manual sell | same parity test; `api:test_config_control.py::test_manual_buy_and_sell_endpoints` | the dashboard exit takes exactly the automatic path (same route, slippage, limits) | as above (M2: 3 manual sells, 100 %) |
| BSC buy | `svc:data-evm/test_worker.py::test_pipeline_discovery_safety_entry_exit` (Four.meme), `::test_manual_buy_runs_every_entry_check_but_replaces_the_signal`; `core:test_evm_safety.py::test_safety_passes_only_with_a_sellable_round_trip_and_clean_contract` | discovery → safety → evidence gate → paper entry at the executable quote; no second entry; manual buy runs every check | a real BSC transaction (EVM live locked) |
| BSC sell | same pipeline test (partial copy sell, stop loss at the sell quote, PnL in BNB, §41 exit checkpoints) | exit at the executable sell quote | as above |
| Robinhood buy | `svc:data-evm/test_worker.py::test_robinhood_pons_pipeline_discovery_safety_entry_exit` (Pons V2, new in M23); `core:test_evm_chains.py::test_pons_v2_buy_simulation_fallback_and_sell_formula` | the same pipeline on a Pons V2 curve: coordination data missing → NO_TRADE; with the chain's answers → paper entry; buy simulated through eth_call | a real Robinhood transaction (EVM live locked) |
| Robinhood sell | same test (stop loss when the curve's quote side collapses, PnL in ETH) | exit at the executable sell quote | as above |
| Copy buy | `svc:copy-engine/test_copy_engine.py::test_solana_copy_needs_gate_approval_and_mirrors_partial_and_full_exits`, `::test_evm_copy_is_gated_idempotent_and_mirrors_partial_sells`, `::test_a_target_buying_into_a_bundled_launch_is_not_copied` | a target buy passes our gate or is not copied; bundled launches refused | copying a real target live (copy is paper) |
| Copy sell | same tests; `::test_sell_only_target_queues_exits_on_our_own_paper_positions`, `::test_sell_only_never_guesses_an_unobserved_holding` | full exit mirrored; SELL ONLY touches only our paper positions | — |
| Partial copy sell | `svc:copy-engine/test_copy_engine.py::test_evm_copy_is_gated_idempotent_and_mirrors_partial_sells`; the partial step in `svc:data-evm/test_worker.py::test_pipeline_discovery_safety_entry_exit`; `core:test_copy_outcomes.py::test_link_fields_of_a_partly_exited_copy` | a 50 % target sell halves our position; link fields kept | — |
| Migration | `core:test_execution_venues.py::test_2_token_that_migrates_before_signing_is_rebuilt_for_pumpswap`, `::test_2b_migration_in_progress_is_not_traded`; `core:test_observation.py::test_migration_during_observation_switches_to_pool_rules`; `core:test_evm_chains.py::test_flap_graduation_event_from_a_real_bsc_log_is_a_migration`, `::test_odyssey_curve_buy_solves_budget_then_follows_migration_to_v3` | migration detected on every chain; no trade mid-migration; the venue switches | — |
| Fresh token | `svc:paper-trading/test_observation_scenarios.py::test_good_fresh_token_with_no_dex_pool_is_observed_entered_and_exited_on_deterioration`, `::test_bad_fresh_token_is_not_traded_and_its_monitoring_expires`; `svc:engine-solana-discovery/test_funnel.py::test_token_inside_its_window_is_fresh_observing`; `core:test_evm_observation.py::test_snapshots_and_expiry_without_an_entry` | observed before any decision; expiry EXPIRED_NO_ENTRY kept | — |
| Migrated token | `core:test_execution_venues.py::test_3_migrated_token_trades_on_the_canonical_pumpswap_pool`; `core:test_migrated_curve_price.py::test_migrated_curve_has_no_price_not_a_zero_price`; `svc:paper-trading/test_observation_scenarios.py::test_fresh_candidate_whose_token_migrates_is_handed_over_not_rejected` | canonical pool; no stale curve price; handed over, not dropped | — |
| Momentum | `svc:engine-solana-discovery/test_funnel.py::test_momentum_promotes_established_accelerating_tokens_only`; `core:test_strategies.py::test_momentum_requires_every_axis_not_just_volume`; `svc:decision-engine/test_gate_eval.py::test_momentum_candidate_uses_momentum_strategy` | momentum needs every axis, not just volume | — |
| Paper | `core:test_pump_pipeline.py::test_paper_entry_partial_tp_and_trailing_exit`; `core:test_safety_gate.py::test_healthy_input_executes_in_paper_with_full_auto_plan`; both data-evm pipeline tests; `core:test_safety_store.py::test_modes_default_to_paper_and_changes_are_audited` | paper simulates fees, partial take profit and trailing; paper is the default | — |
| ML | `core:test_ml_governance.py` (contribution 0 % by default, raised only with a PASS, one step a week; LIVE locked); `svc:decision-engine/test_ml_champion.py::test_champion_scores_and_explains_but_cannot_bypass_gate`; no look-ahead: `core:test_evm_ml_samples.py::test_labels_come_only_from_the_hour_after_the_decision`, `core:test_scanner_features.py::test_blocks_stamped_after_the_decision_are_dropped`, `core:test_deployer_intel.py::test_features_never_use_launches_resolved_or_created_after_the_decision` | ML cannot bypass the gate; no future data in features; governance gates | model quality on real data: frozen windows fill after deploy |
| RPC failure | `core:test_execution_venues.py::test_8_rpc_failure_is_a_controlled_rpc_unavailable`; `core:test_evm_chains.py::test_rpc_fails_over_on_429_checks_chain_id_and_never_leaks_urls`, `::test_unavailable_rpc_is_a_failed_quote_never_a_price`; `core:test_runtime_config.py::test_reloader_swaps_endpoints_into_a_running_manager_and_fails_over`; `svc:data-solana/test_rpc_manager.py` | failover; an outage is explicit, never a price or a pass | — |
| WSS failure | `svc:data-solana/test_ws_client.py::test_reconnects_and_resubscribes_after_forced_disconnect`; `core:test_evm_streams.py::test_feed_client_resumes_and_falls_back_to_the_delayed_feed`, `::test_backlog_reorg_and_resume_handling`, `::test_a_settings_or_redis_failure_never_stops_the_streams`; `core:test_pump_pipeline.py::test_dead_stream_makes_flow_stale` | reconnect and resubscribe; resume by sequence; a dead stream makes data stale, never fresh | — |
| Provider failure | `core:test_safety_gate.py::test_provider_unavailable_is_no_trade`, `::test_sell_route_unavailable_is_no_trade`; `core:test_evm_safety.py::test_goplus_flags_fail_and_a_clean_or_missing_answer_changes_nothing`; `core:test_enrichment.py::test_provider_errors_are_classified`; the coordination NO_TRADE step in the Robinhood pipeline test | provider failure → NO_TRADE where the data is critical; optional providers never pass a token | — |
| Stale data | `core:test_safety_gate.py::test_stale_critical_data_is_no_trade`; `core:test_risk.py::test_stale_data_rejects`; `svc:paper-trading/test_failure_isolation.py::test_stale_price_is_not_used_to_exit_a_position`; `core:test_position_pnl.py::test_a_stale_mark_still_computes_but_says_stale` | stale data never enters or exits, and is labelled stale on screen | — |
| Insufficient gas | `core:test_chains.py::test_gate_refuses_an_entry_the_wallet_cannot_pay_gas_for`; `svc:data-evm/test_worker.py::test_an_entry_needs_gas_for_both_swaps_and_the_gas_reserve`; `svc:paper-trading/test_live_worker.py::test_entry_larger_than_wallet_minus_reserve_is_refused` | INSUFFICIENT_GAS → NO_TRADE before anything is signed | — |
| Slippage | `core:test_risk.py::test_max_slippage_enforced`, `::test_max_price_impact_enforced`; `core:test_creator_and_migrated_liquidity.py::test_migrated_slippage_limit_blocks`; `svc:paper-trading/test_exit_parity.py::test_other_sell_failures_still_widen_slippage_and_keep_the_route` | limits enforced; exit retries widen within limits | — |
| Duplicate order | `svc:paper-trading/test_live_worker.py::test_processing_an_order_twice_buys_once`, `::test_a_second_entry_for_the_same_mint_is_refused`, `::test_second_buy_failure_rejects_and_never_duplicates`; `core:test_execution_venues.py::test_11_a_buy_that_looked_timed_out_but_confirmed_is_never_rebought`; the parity test ("close once") | one order, one fill, never a second buy or sell | — |
| Restart | `svc:paper-trading/test_live_worker.py::test_restart_with_a_signed_order_resolves_it_without_rebuying`; `svc:data-evm/test_worker.py::test_restart_restores_announced_contracts`; the re-scan in the BSC pipeline test; `api:test_system.py::test_status_reports_running_after_restart` | a restart neither loses nor repeats work | continuity on the server: `docs/ACCEPTANCE_24x7.md` |
| Reconciliation | `svc:paper-trading/test_live_worker.py::test_worker_reconciles_before_processing_and_then_reports_ready`, `::test_reconcile_flags_missing_tokens_and_records_unknown_holdings_once`; `api:test_live_api.py::test_orders_and_reconciliation_are_listed` | the wallet is the truth: missing tokens need review, never an invented exit | the same on the server after a restart (acceptance procedure, step 6) |

## §80 automatic sell regression

`services/paper-trading/tests/test_exit_parity.py`:
- `test_automatic_and_manual_exits_sell_the_same_way_and_close_once` runs a
  stop-loss exit and a dashboard exit under identical market conditions.
  Both must:
  - create one full SELL with the same route, slippage and limits;
  - close the position from the fill;
  - update PnL;
  - never need a second sell.
  If the automatic one fails, the test fails.
- `test_a_curve_sell_rejected_as_curve_complete_moves_the_next_sell_to_pumpswap`
  covers the production failure M2 traced.
- `test_other_sell_failures_still_widen_slippage_and_keep_the_route`.

"Transaction is confirmed" is proven from the confirmed fill in these tests;
on the chain it is measured by `tools.exit_diagnosis` (tracker section 3).

## §81 24/7 acceptance

An operator procedure on the server, with `tools.acceptance_247` as the
measurement: `docs/ACCEPTANCE_24x7.md`. NOT VERIFIED until it has run there.
