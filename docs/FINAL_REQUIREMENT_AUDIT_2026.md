# Final requirement audit (master §82)

The master prompt (sections 0–84) re-read in full on 2026-10-04, after M23,
and every section checked against the code on `main`. No section is skipped.

Columns are the ones §82 asks for:

| Column | Content |
|---|---|
| REQUIREMENT | the section's demand |
| STATUS | DONE / DONE (paper) / PARTIAL / NOT VERIFIED / RULE (a working rule, followed throughout) |
| FILES | where it lives |
| TEST | the tests that prove it |
| RESULT | the test result on 2026-10-04 |
| EVIDENCE | production evidence or the tracker section |
| REMAINING ISSUE | what is not done or not proven |

Paths:
- `core/` means `packages/core-py/yonixalpha_core/`;
- tests are in `packages/core-py/tests/` unless they start with `api:`
  (`apps/api/tests/`) or `svc:<service>` (`services/<service>/tests/`);
- "§n" in EVIDENCE means section n of `docs/MASTER_UPGRADE_2026.md` (the
  tracker).

Test result: the whole suite passes. That is core 851, api 159, data-solana
10, data-evm 13, copy-engine 11, engine-solana-discovery 18,
engine-solana-migration 11, engine-solana-momentum 11, decision-engine 58,
ml 43, paper-trading 80, with ruff, tsc and next lint clean. RESULT "PASS"
below means that.

Two facts hold for every row:
- LIVE VERIFIED applies only to Solana (Pump.fun / PumpSwap): live trades
  since September 2026, measured on the server (§5).
- BSC and Robinhood are paper only. EVM live execution is off and locked
  (operator decision), so no BSC or Robinhood row can be LIVE VERIFIED.

| § | REQUIREMENT | STATUS | FILES | TEST | RESULT | EVIDENCE | REMAINING ISSUE |
|---|---|---|---|---|---|---|---|
| 0 | 24/7 server-side control center for Solana, BSC, Robinhood: fresh / migrated / momentum / launchpad / DEX tokens, smart wallets, copy, auto, manual, paper, ML, safety, sellability, positions, real-time PnL / balances / execution / latency / provider health | PARTIAL | `services/`, `apps/api/`, `apps/web/` | full suite | PASS | §1–34 | BSC / Robinhood trade on paper only (EVM live locked); see the rows below |
| 1 | Preserve working Solana / Pump.fun / PumpSwap execution; change only after diagnosis | DONE | `core/solana/live_exec.py`, `core/solana/pump_tx.py`, `core/solana/pumpswap.py`, `core/live_trading.py` | `test_pump_tx_sdk_parity.py`, `test_live_exec.py`, `svc:paper-trading/test_live_worker.py`, `svc:paper-trading/test_exit_parity.py` | PASS | since the audit baseline 84d6b86 one commit touched execution (803c8fd): curve-complete sells move to PumpSwap, made after `exit_diagnosis` traced the failure on the server (§5) | none |
| 2 | Research Jul–Sep 2026, newest first; no scraping, no browser automation as the engine | DONE for repositories and official documentation | `docs/MASTER_UPGRADE_2026.md`, `docs/MULTICHAIN_AUDIT_2026.md`, `docs/PUMPFUN_EXECUTION_RESEARCH.md`, `docs/SCANNER_INTELLIGENCE_2026.md` | — | — | §17–18, §22, §34; every integration is on-chain events, RPC, WebSocket, sequencer feed or official APIs | no claim is made about YouTube / X / Reddit sources; the recorded research is code and official documentation |
| 3 | Per-repository record (URL, commit, license, …); inspect code, never trust stars | DONE | `docs/MASTER_UPGRADE_2026.md` (§18) | — | — | every repository named in §7–12 recorded at a pinned commit | none |
| 4 | Only Solana, BSC, Robinhood in the active UI | DONE | `apps/web/app/dashboard/layout.tsx` | — | — | legacy futures / forex / grid removed to an archive branch (MC P5) | none |
| 5 | Launchpad Health: status, last launch / trade / migration, 7-day counts, verified flags | DONE | `core/chains/activity.py`, `core/chains/verification.py`, `core/tools/launchpad_verify.py`, `apps/web/app/dashboard/launchpads/page.tsx` | `test_launchpad_activity.py`, `test_venue_probe.py` | PASS | §1, §4, §19 | Solana venues' 7-day counts and volume show "not tracked" (a sample, not a full stream) |
| 6 | 7 days without activity → INACTIVE, adapter kept, reactivation | DONE | `core/chains/activity.py` | `test_launchpad_activity.py` | PASS | NOXA and The Odyssey inactive (operator confirmed), adapters kept (§25) | none |
| 7 | Solana launchpads beyond Pump.fun (LetsBONK, LaunchLab, Meteora DBC, Bags, Moonshot, Jupiter Studio, StonkFun) | PARTIAL | `core/solana/venue_probe.py`, `core/solana/dbc.py`, `core/solana/launchlab.py`, `core/tools/dbc_verify.py`, `core/tools/launchlab_verify.py` | `test_venue_probe.py`, `test_dbc.py`, `test_launchlab.py`, `test_venue_verify_tools.py` | PASS | activity monitored (§19), sites per platform config (§22), DBC / LaunchLab quotes equal to the official SDKs (§34) | LaunchLab constant-product quotes PASS against the chain (88/88 trades, tracker §37); DBC exact-in quotes PASS against the chain (68/68 swaps, after the twin-event fix, §37); partial fill / exact out not seen on the chain; LaunchLab Token-2022 pools refused (20 of 21 seen); Moonshot quotes not done; paper trading on these venues not wired; all OBSERVE ONLY |
| 8 | BSC: Four.meme, Flap verified; Genius.fun researched; nothing enabled automatically | PARTIAL | `core/chains/evm/fourmeme.py`, `core/chains/evm/flap.py`, `core/chains/registry.py`, `core/tools/fourmeme_modes.py` | `test_evm_chains.py`, `test_evm_safety.py`, `test_fourmeme_modes_tool.py`, `svc:data-evm/test_worker.py` | PASS | discovery and quotes verified on BSC (§1); X Mode detected by simulation; stock-quoted curves handled (§22–23); Genius.fun observe-only | Four.meme AntiSniperFeeMode / template layout untested after four server runs (tracker §36–37); the implementation's source is verified neither on Sourcify nor on Etherscan, so the layout cannot be read from an ABI (safety does not depend on it); buy / sell transactions not run (EVM live locked) |
| 9 | BSC mempool wallet copying, latency recorded | DONE (measurement) | `core/chains/evm/streams.py` | `test_evm_streams.py` | PASS | §17: pending-transaction stream, REFUSED / LIMITED become UPGRADE REQUIRED | needs a WSS endpoint whose plan serves full pending transactions; copy decisions stay on confirmed trades |
| 10 | Robinhood: Pons, NOXA, Odyssey (chain 4663, gas ETH) | DONE | `core/chains/evm/pons.py`, `core/chains/evm/odyssey.py` | `test_evm_chains.py`, `svc:data-evm/test_worker.py` | PASS | Pons V1 / V2 discovered and paper traded; NOXA / Odyssey inactive (§25) | Pons V4 (graduated) trading not implemented: graduated tokens are observe-only |
| 11 | Pons coordinated-launch checks → NO_TRADE / REDUCE_SIZE / MANUAL_APPROVAL | DONE (paper) | `core/launch_coordination.py`, `core/tools/coordination_check.py` | `test_launch_coordination.py`, `svc:data-evm/test_worker.py` | PASS | entrypoint selectors and the on-chain snipe-tax check VERIFIED on the server (§14); receipt exemptions read from real launches | Blockscout funding lookups NOT VERIFIED on the server since the User-Agent change |
| 12 | Robinhood reference repositories inspected | DONE | `docs/MASTER_UPGRADE_2026.md` (§18) | — | — | all seven plus the official Pons source | none |
| 13 | Sequencer feed (+ delayed fallback): latency, reconnects, gaps, duplicates | DONE | `core/chains/evm/streams.py` | `test_evm_streams.py` | PASS | §17; feed signatures and decoding checked against 143 real transactions (§18) | real-feed latency NOT VERIFIED from the build environment |
| 14 | Every token enters OBSERVATION on all chains before TRADE / WAIT / REJECT / EXPIRE | DONE | `core/chains/evm/observation.py`, `core/solana/observation.py` | `test_evm_observation.py`, `test_observation.py`, `svc:engine-solana-discovery/test_funnel.py` | PASS | §15, §33 | none |
| 15 | Observation state machine (DISCOVERED … ENTERED / EXPIRED / REJECTED) | DONE | `core/chains/evm/observation.py`, `core/solana/observation_states.py` | `test_evm_observation.py`, `test_solana_observation_states.py` | PASS | Solana in the §15 names since M21 (§33) | none |
| 16 | Snapshots T0–T+60, adaptive windows, recorded fields | DONE | `core/chains/evm/observation.py`, `core/solana/followups.py`, `core/solana/launch_features.py` | `test_evm_observation.py`, `test_observation_followups.py`, `test_launch_features.py` | PASS | §15 | fields an EVM chain cannot give (holders) say "not tracked", never 0 |
| 17 | observation_started_at / deadline / reason / expiry_reason; EXPIRED_NO_ENTRY kept for ML | DONE | `core/chains/evm/observation.py`, `core/solana/observation_states.py` | `test_evm_observation.py`, `test_solana_observation_states.py`, `api:test_observations_settings.py` | PASS | §15, §33 | the detailed Solana funnel report is pruned after 3 days; the outcome is kept in the opportunity ledger |
| 18 | Wallet windows 24H … 180D | PARTIAL | `core/wallet_pnl.py`, `core/wallet_profiles.py` | `test_wallet_pnl.py` | PASS | §6 | EVM: 14D+ are INSUFFICIENT DATA (14-day trade retention); Solana: launches' first 30 minutes only |
| 19 | Wallet performance model (trades, wins, PF, drawdown, holds, best / worst, entry mcap, slippage, copy delay …) | PARTIAL | `core/wallet_pnl.py`, `core/wallet_profiles.py` | `test_wallet_pnl.py`, `test_wallet_rebuild.py` | PASS | §6, §33 | average slippage / price impact / copy delay of a wallet: not measured (no wallet-level fills) |
| 20 | Show profit and loss separately | DONE | `apps/web/app/dashboard/smart-wallets/page.tsx`, `core/wallet_pnl.py` | `test_wallet_pnl.py` | PASS | §6 | none |
| 21 | Realistic wallet PnL: ledger, partial sells, fees, transfers; realized / unrealized / executable | PARTIAL | `core/wallet_pnl.py`, `core/wallet_intel.py` | `test_wallet_pnl.py`, `test_wallet_intel.py` | PASS | §6, §33 | fees listed, not subtracted per launchpad; gas not included; transfers / airdrops not in the ledger |
| 22 | Cost-basis engine (FIFO lots) | DONE | `core/wallet_pnl.py` | `test_wallet_pnl.py` | PASS | §6 | none |
| 23 | INSUFFICIENT DATA, never 0 % / $0 / 0 trades | DONE | `core/wallet_pnl.py`, `core/position_pnl.py`, `core/wallet_profiles.py` | `test_wallet_pnl.py`, `test_position_pnl.py`, `test_wallet_intel.py` | PASS | §6, §24, §33 | none |
| 24 | Nansen / MadeOnSol as enrichment, never the only source | DONE | `core/enrichment.py` | `test_enrichment.py`, `api:test_enrichment_api.py` | PASS | §25 | NOT VERIFIED against the real APIs (no keys); off by default |
| 25 | Wallet discovery: collect, score, validate, paper-follow; never auto-copy | DONE | `core/wallet_profiles.py`, `core/wallet_validation.py` | `test_wallet_validation.py`, `test_wallet_rebuild.py` | PASS | §12 | none |
| 26 | Wallet validation gates, consistency across periods | DONE | `core/wallet_validation.py` | `test_wallet_validation.py` | PASS | §12, §33 | Solana wallets need several covered launches (ledgers start after the M21 deploy) |
| 27 | Outlier test (with / without best and top 3) | DONE | `core/wallet_pnl.py` | `test_wallet_pnl.py` | PASS | §6 | none |
| 28 | Market regime test | PARTIAL | `core/market_regimes.py` | `test_market_regimes.py` | PASS | §12 | Solana: INSUFFICIENT DATA (no Solana regime series recorded) |
| 29 | Copy BUY ONLY / SELL ONLY / BUY + SELL | DONE (paper) | `core/copy_trading.py`, `services/copy-engine/app/main.py` | `svc:copy-engine/test_copy_engine.py` | PASS | §7 | copy is paper only |
| 30 | Copy buy: decode, venue, wallet quality, safety, liquidity, displacement, delay, size; never chase | DONE (paper) | `core/copy_trading.py` | `svc:copy-engine/test_copy_engine.py`, `test_wallets_copy.py` | PASS | §7, §9 | none |
| 31 | Copy sell 20 / 50 / 100 %, proportional, override, emergency | DONE (paper) | `core/copy_trading.py` | `svc:copy-engine/test_copy_engine.py`, `svc:data-evm/test_worker.py` | PASS | §7 | none |
| 32 | Copy position link fields | DONE (paper) | `core/copy_outcomes.py` | `test_copy_outcomes.py` | PASS | §9 | slippage measured only on live fills (none for copy) |
| 33 | Copy latency stages on the dashboard | PARTIAL | `core/copy_outcomes.py`, `apps/web/app/dashboard/copy/page.tsx` | `test_copy_outcomes.py` | PASS | §9 | build / sign / submit / landing / confirm exist only for live copies (none); the target's own submit time is not observable |
| 34 | Copy never overrides safety | DONE | `core/copy_trading.py`, `core/decision_states.py` | `svc:copy-engine/test_copy_engine.py`, `test_decision_states.py` | PASS | §28 | none |
| 35 | Paper copy for every selected wallet, with would-have-won / missed | DONE (paper) | `core/copy_outcomes.py` | `test_copy_outcomes.py`, `svc:copy-engine/test_copy_engine.py` | PASS | §9 | NOT VERIFIED in production volume |
| 36 | Smart-wallet ML features | DONE | `core/ml/wallet_labels.py` | `svc:ml/test_evm_ml.py` | PASS | §26 | shadow models only |
| 37 | Mistake labels (SUCCESSFUL / FAILED / LATE ENTRY, PREMATURE / LATE EXIT, MISSED_WINNER) | DONE | `core/ml/wallet_labels.py` | `svc:ml/test_evm_ml.py` | PASS | §26 | none |
| 38 | ML knowledge testing on frozen unseen sets | DONE | `core/ml/frozen.py`, `services/ml/app/validation.py` | `svc:ml/test_validation.py`, `test_ml_governance.py` | PASS | §31 | NOT VERIFIED on real data until frozen windows fill |
| 39 | Stages OBSERVATION_ONLY → SHADOW → PAPER → CONTROLLED LIVE; no promotion on confidence | DONE | `core/ml/governance.py` | `test_ml_governance.py`, `api:test_ml_governance.py` | PASS | §31 | LIVE contributor locked |
| 40 | ML dashboard fields; contribution 0 % → small steps with gates; no increase on a short winning run | DONE | `core/ml/governance.py`, `apps/web/components/MlGovernance.tsx` | `test_ml_governance.py`, `svc:decision-engine/test_evaluate.py` | PASS | §31 (behaviour change: Solana ML contribution 0 % by default since M19) | none |
| 41 | ML decision test: BUY / WAIT / REJECT / SELL / HOLD vs deterministic, risk, final | DONE | `core/ml/evm_samples.py`, `core/ml/exit_samples.py` | `test_evm_ml_samples.py`, `test_exit_samples.py`, `svc:ml/test_evm_ml.py` | PASS | §26, §30 | none |
| 42 | Champion / challenger; never overwrite historical models | DONE | `core/ml/registry.py`, `services/ml/app/train.py` | `test_ml_registry.py`, `svc:ml/test_train.py` | PASS | §31 | none |
| 43 | No look-ahead | DONE | `core/ml/evm_samples.py`, `core/opportunity_analysis.py`, `core/deployer_intel.py` | `test_evm_ml_samples.py`, `test_opportunity_analysis.py`, `test_scanner_features.py`, `test_deployer_intel.py` | PASS | §26 | none |
| 44 | Paper trading as training data | DONE | `core/paper_engine.py`, `core/opportunity_analysis.py`, `core/ml/evm_samples.py` | `test_opportunity_analysis.py`, `test_evm_ml_samples.py` | PASS | §26 | none |
| 45 | Manual BUY / SELL on all chains, correct venue, through safety | DONE (Solana live; EVM paper) | `core/manual_trade.py`, `core/chains/evm/manual.py` | `test_operator_request.py`, `api:test_config_control.py`, `api:test_explorer_wallets_api.py`, `svc:data-evm/test_worker.py` | PASS | §24 | EVM manual trades are paper (EVM live locked) |
| 46 | Instrument the automatic sell path stage by stage | DONE | `core/live_trading.py`, `core/tools/exit_diagnosis.py` | `test_exit_diagnosis.py` | PASS | §5 | none |
| 47 | Automatic vs manual sell comparison; change only on evidence | DONE | `core/tools/exit_diagnosis.py` | `test_exit_diagnosis.py`, `svc:paper-trading/test_exit_parity.py` | PASS | §5: automatic 72 sells, 97.2 % confirmed, median 3.1 s; manual 3 sells, 100 %, 10.1 s (waits for the loop tick) | none |
| 48 | Provider settings in the dashboard, not .env | DONE | `core/solana/rpc_registry.py`, `core/chains/evm/rpc_registry.py`, `apps/web/app/dashboard/rpc/page.tsx` | `test_evm_rpc_registry.py`, `api:test_provider_keys.py` | PASS | §11, §16 | none |
| 49 | Provider roles | DONE | `core/provider_roles.py` | `test_provider_roles.py` | PASS | §16 | none |
| 50 | Solana providers, plan requirements shown | DONE | `core/provider_roles.py`, `core/solana/rpc.py` | `test_provider_roles.py`, `test_rpc_versions_capabilities.py` | PASS | §16 | LaserStream / transactionSubscribe not integrated (plan-dependent) |
| 51 | BSC providers; public RPC never the sole production path | PARTIAL | `core/chains/evm/rpc.py`, `core/chains/evm/rpc_registry.py` | `test_evm_chains.py`, `test_evm_rpc_registry.py` | PASS | §1, §16 | depends on the operator adding a keyed BSC provider |
| 52 | Robinhood providers (HTTP, WSS, sequencer, archive) | PARTIAL | `core/chains/evm/rpc_registry.py`, `core/chains/evm/streams.py` | `test_evm_rpc_registry.py`, `test_evm_streams.py` | PASS | §16–17 | depends on the operator adding a keyed Robinhood provider |
| 53 | Plan health: UPGRADE REQUIRED with provider, plan, capability, limitation, recommendation | DONE | `core/provider_roles.py`, `apps/api/app/api/health_state.py` | `test_provider_roles.py`, `api:test_control_center.py` | PASS | §16 | none |
| 54 | Token explorer across chains with every field | DONE | `core/chains/evm/token_view.py`, `apps/web/app/dashboard/explorer/page.tsx` | `test_evm_token_view.py`, `api:test_explorer_wallets_api.py` | PASS | §24 | EVM holders "not tracked"; ML NOT_AVAILABLE on the EVM token page |
| 55 | Explorer action buttons, correct link per chain | DONE | `core/explorer_links.py` | `test_explorer_links.py` | PASS | §24 | Robinhood has no confirmed DEX page (shown unavailable) |
| 56 | Balance management per chain (total, available, reserved, gas reserve, trading) | DONE | `core/balances.py`, `core/chains/evm/wallet.py` | `test_balances.py` | PASS | §24 | EVM live balance NOT VERIFIED on the server |
| 57 | Insufficient gas → NO_TRADE before signing | DONE | `core/chains/evm/paper.py`, `core/safety/gate.py` | `test_chains.py`, `svc:data-evm/test_worker.py`, `svc:paper-trading/test_live_worker.py` | PASS | §24 | none |
| 58 | Unified wallet: separate Solana and EVM accounts, keys never exposed | DONE | `core/chains/evm/wallet.py`, `core/solana/wallet.py`, `core/redact.py` | `test_redact.py`, `api:test_control_center.py`, `api:test_security.py` | PASS | §24 | none |
| 59 | Every position shows PROFIT / LOSS with all fields | DONE | `core/position_pnl.py` | `test_position_pnl.py` | PASS | §24 | none |
| 60 | Colours and icons; NO EMOJIS | DONE | `apps/web/components/`, `core/position_pnl.py` | `test_no_emojis.py` (new in M23: scans the dashboard and every Python module, and fails on any emoji), `test_position_pnl.py` | PASS | scan: none in code | none |
| 61 | Market cap in $K / $M / $B | DONE | `apps/web/lib/format.ts` (`formatUsdCompact`), `core/chains/evm/token_view.py` | `test_evm_token_view.py` (the USD values) | PASS | §24 | the compact formatting itself has no unit test (the web app has no test runner; type-checked only) |
| 62 | Trading continues with the browser or phone offline | DONE in design, NOT VERIFIED on the server | `infra/docker/docker-compose.prod.yml`, `core/tools/acceptance_247.py` | `test_acceptance_247.py` | PASS | every engine is a server container with a restart policy; the dashboard only views | run `docs/ACCEPTANCE_24x7.md` on the server |
| 63 | Server workers (discovery, launchpad / wallet / copy monitors, market data, signal, ML, risk, execution, positions, exits, migration, outcomes, health, research) | DONE | `services/` | service suites | PASS | §1 | none |
| 64 | GitHub / infrastructure update monitor via API (no HTML) | DONE | `core/update_monitor.py` | `test_update_monitor.py` | PASS | §21, §25 | GitHub path NOT VERIFIED from the build environment (blocked) |
| 65 | Store and classify updates (INFO … ACTION_REQUIRED) | DONE | `core/update_monitor.py` | `test_update_monitor.py` | PASS | §21 | none |
| 66 | Telegram + System Health notification; never auto-deploy | DONE | `core/update_monitor.py`, `core/notify.py` | `test_update_monitor.py`, `test_notify.py` | PASS | §21 | none |
| 67 | RESEARCH → REVIEW → PAPER → VALIDATION → CONTROLLED RELEASE | DONE | `core/research.py` | `test_research.py`, `api:test_research_api.py` | PASS | §32 | none |
| 68 | Multiple detection methods (launches, wallets, safety) | DONE | `core/chains/evm/crosscheck.py`, `core/chains/evm/streams.py`, `core/chains/evm/safety.py` | `test_crosscheck.py`, `test_evm_streams.py`, `test_evm_safety.py` | PASS | §28, §32 | an indexed API as a further launch source not added (none verified without a key); stream-vs-logs numbers pending the server |
| 69 | Source priority: on-chain first; never scraping | RULE / DONE | `core/chains/evm/`, `core/solana/` | — | — | no HTML scraping or browser automation anywhere in the trading path | none |
| 70 | Fallback: secondary provider, then safe degraded; failure → NO_TRADE | DONE | `core/chains/evm/rpc.py`, `core/solana/rpc.py`, `core/safety/gate.py` | `test_evm_chains.py`, `test_runtime_config.py`, `test_safety_gate.py`, `svc:data-evm/test_worker.py` | PASS | §28 | none |
| 71 | Paper on all chains: buy, sell, partial, fees, gas, slippage, impact, latency, migration, copy, stop, TP, trailing, failures | DONE | `core/paper_engine.py`, `core/paper_execution.py`, `core/chains/evm/paper.py` | `test_pump_pipeline.py`, `svc:paper-trading/test_e2e_paper.py`, `svc:paper-trading/test_holders_and_failures.py`, `svc:data-evm/test_worker.py` | PASS | §26 | none |
| 72 | Paper feeds ML (would_buy / sell / wait / reject, theoretical and executable PnL, MFE / MAE) | DONE | `core/opportunity_analysis.py`, `core/ml/evm_samples.py` | `test_opportunity_analysis.py`, `test_evm_ml_samples.py` | PASS | §26 | none |
| 73 | Train on every kind of outcome, never winners only | DONE | `services/ml/app/dataset.py`, `core/ml/evm_samples.py` | `svc:ml/test_dataset.py`, `svc:ml/test_evm_ml.py` | PASS | §26–27 | none |
| 74 | Continuous learning through challenger / validation / shadow / paper; no overwrite per trade | DONE | `services/ml/app/train.py`, `core/ml/governance.py` | `svc:ml/test_train.py`, `test_ml_governance.py` | PASS | §29, §31 | none |
| 75 | ML knowledge dashboard | DONE | `apps/web/app/dashboard/ml/review/page.tsx` | `api:test_evm_ml_api.py`, `api:test_ml_governance.py` | PASS | §26, §31 | none |
| 76 | Safety hierarchy; ML, wallets and copy never override safety | DONE | `core/decision_states.py`, `core/safety/gate.py` | `test_decision_states.py`, `test_safety_gate.py`, `svc:decision-engine/test_ml_champion.py` | PASS | §28 | none |
| 77 | Decision states with full evidence | DONE | `core/decision_states.py`, `core/decision.py` | `test_decision_states.py`, `test_decision.py` | PASS | §28 | none |
| 78 | Never delete historical ML / trading data | DONE | `apps/api/migrations/versions/` | — | — | 40 migrations, none drops a table or a column on upgrade (checked 2026-10-04) | none |
| 79 | Test the listed scenarios | DONE | `docs/TEST_MATRIX_2026.md` | 84 named tests, all existing (checked by script) | PASS | Robinhood pipeline test added in M23 | real-chain behaviour of BSC / Robinhood (EVM live locked) |
| 80 | Automatic sell regression under identical conditions | DONE | `services/paper-trading/tests/test_exit_parity.py` | `svc:paper-trading/test_exit_parity.py` | PASS | §5 | none |
| 81 | 24/7 acceptance test | NOT VERIFIED | `docs/ACCEPTANCE_24x7.md`, `core/tools/acceptance_247.py` | `test_acceptance_247.py` | PASS | — | the operator runs the procedure on the server |
| 82 | Final requirement audit | DONE | this document | — | — | — | none |
| 83 | Final report | DONE | `docs/FINAL_REPORT_2026.md` | — | — | — | none |
| 84 | Final development rule (research, audit, test, measure, verify; no scraper, no demo; trust nothing blindly) | RULE | — | — | — | every phase recorded in the tracker with its evidence and its NOT VERIFIED items | none |

## Open items, all in one place

Items that need the server:
- `tools.dbc_verify` and `tools.launchlab_verify` (§7);
- `tools.fourmeme_modes` with the verified ABI (§8);
- `docs/ACCEPTANCE_24x7.md` (§62, §81);
- stream-vs-logs cross-check numbers (§68);
- Blockscout funding lookups (§11);
- GitHub path of the update monitor (§64);
- EVM live balances (§56).

Items that need an operator decision or a key:
- keyed BSC / Robinhood RPC providers (§51–52);
- Nansen / MadeOnSol keys (§24);
- EVM live execution, locked by decision (§0, §8, §45).

Not built:
- Moonshot quotes and paper trading on the other Solana launchpads (§7);
- Pons V4 trading after graduation (§10);
- wallet-level slippage / impact / copy delay (§19);
- fees and gas in the wallet ledger, transfers and airdrops (§21);
- 14-day-plus EVM wallet windows (§18);
- a Solana regime series (§28);
- an indexed launch API (§68);
- Helius LaserStream (§50).

Pending task: manual BUY overriding preference filters (task #138) stays
blocked by decision.
