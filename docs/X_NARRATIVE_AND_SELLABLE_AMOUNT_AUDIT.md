# X Narrative Intelligence + Sellable-Amount Protection — Audit (2026-10-10)

Audit of the code as merged on `main` at c50bfd7, before any change in this
upgrade. Every row cites the file and function it was read from. Status:
IMPLEMENTED_AND_VERIFIED (code + a test exercising it), PARTIAL, MISSING,
BROKEN, NOT_VERIFIED (exists but no test or production evidence was found).

## A. Token discovery and identity

| Component | Where | Status | Notes |
|---|---|---|---|
| Pump.fun launch discovery | `solana/pump_stream.ingest_logs`, `services/engine-solana-discovery/app/main.py` (logsSubscribe on the pump program) | IMPLEMENTED_AND_VERIFIED | `tests/test_pump_pipeline.py`; on the server 44 creates/min measured 2026-10-09 (stream_check) |
| Missed-launch repair | `solana/stream_guard.gap_fill` / `stream_problem` | IMPLEMENTED_AND_VERIFIED | `tests/test_stream_guard.py`; server: 10 gap-filled/min, 88 % delivered by the stream (2026-10-09) |
| Token identity (name, symbol, uri, creator) | CreateEvent fields in `pump_stream.ingest_logs` (`meta_key`), `db/models.Token.metadata_uri` | IMPLEMENTED_AND_VERIFIED | identity = mint; name/symbol come from the on-chain CreateEvent |
| Metadata JSON / project social links | — | MISSING | the `uri` is stored, its JSON (twitter/website) is never fetched; `market_data.parse_dexscreener_pairs` drops DexScreener `info.socials` |
| Social / X data of any kind | — | MISSING | only `enrichment.py` (Nansen / MadeOnSol wallet labels, KOL twitter handle as a provider-reported field) |
| PumpPortal new-token feed | `solana/pumpportal_ws` | IMPLEMENTED_AND_VERIFIED | cross-check only, never a data source for trading |

## B. Bonding curve, migration, routes

| Component | Where | Status | Notes |
|---|---|---|---|
| Curve monitoring | `pump_stream` curve keys, `assembler.assemble_fresh` | IMPLEMENTED_AND_VERIFIED | |
| Migration detection | MIGRATION_EVENT (`pumpfun._EVENTS`), `pump_stream.MIGRATED`, `pumpportal_ws.MIGRATED` | IMPLEMENTED_AND_VERIFIED | |
| Sell route chosen at sell time | `live_exec._resolve` -> `venue.resolve(side="sell")` | IMPLEMENTED_AND_VERIFIED | a position is not bound to its entry route |
| Curve-complete rejection -> PumpSwap | `live_trading.curve_complete_rejection`, `switch_to_pumpswap` | IMPLEMENTED_AND_VERIFIED | `services/paper-trading/tests/test_exit_parity.py` |
| Jupiter fallback route for sells | `live_exec._resolve` (NO_EXECUTABLE_ROUTE -> Jupiter quote with raw amount) | IMPLEMENTED_AND_VERIFIED | |

## C. Exits

| Component | Where | Status | Notes |
|---|---|---|---|
| TP / SL / trailing decision | `paper_engine.manage_step` | IMPLEMENTED_AND_VERIFIED | TP size = `initial_quantity x exit_fraction`, capped at remaining; defaults 0.4 / 0.3 / 0.3 (`safety/settings.tp_exit_fractions`, validated to sum <= 1) |
| Same decision for LIVE | `live_trading.manage_live_position` reuses `manage_step` | IMPLEMENTED_AND_VERIFIED | |
| Raw token units for sells | `live_trading.request_live_exit` (`ROUND_DOWN`, capped at remaining raw) | IMPLEMENTED_AND_VERIFIED | never oversells the position's own quantity |
| Remaining tokens after a sell | `live_trading.apply_outcome` (sold = -token_change_raw from the fill) | IMPLEMENTED_AND_VERIFIED | from the confirmed transaction, not the request |
| Dust threshold | `live_trading.DUST_RAW = 1`, `paper_engine.DUST = 1e-9` | PARTIAL | a remainder of a few raw units, or one worth less than a sell fee, stays open; nothing checks a remainder's value |
| Partial exit worth less than its fee | — | MISSING | a TP worth less than the 0.000105 SOL sell fee is sent (paper charges it as a loss, `paper_engine._position_fixed_fees`) |
| Whole-schedule planning | — | MISSING | each level is computed alone |
| Concurrent exit protection | `PaperPosition.pending_order_id`; `request_live_exit` returns None while an order is pending | IMPLEMENTED_AND_VERIFIED | `test_exit_parity` (no duplicate sell) |
| Wallet balance read before a sell | `live_trading.reconcile` (on-chain balance clamps `remaining_quantity`, never assumes more than held) | PARTIAL | periodic reconciliation, not per sell; the transaction itself fails safely if the balance is lower (simulation) |
| Quote / min-out per sell | `request_live_exit` (`min_sol_out_lamports` from the curve/pool model at exit slippage) | IMPLEMENTED_AND_VERIFIED | |
| Exit retries with escalating slippage | `exit_slippage_step_pct`, `max_exit_slippage_pct`, `exit_failures` | IMPLEMENTED_AND_VERIFIED | |
| Token-2022 support | `rent_reclaim` (Token-2022 accounts), `pump_tx` (create_v2) | IMPLEMENTED_AND_VERIFIED | |
| Transfer-fee extension | `token_safety` reads `transferFeeConfig` -> `transfer_fee_bps`; tax gate refuses above the limit | IMPLEMENTED_AND_VERIFIED | a token with a transfer fee is not entered when it exceeds the configured tax; paper exits apply the fee (`exit_fill(... transfer_fee_bps)`) |
| Exit UI (raw balance, planned next exit, dust state) | — | MISSING | the trade page shows initial / remaining quantity and TP hits only |

## D. Decision, risk, ML

| Component | Where | Status | Notes |
|---|---|---|---|
| Signal + safety gate | `safety/gate.assess`, `services/decision-engine/app/gate_eval.evaluate_with_gate` | IMPLEMENTED_AND_VERIFIED | |
| Signal strength (on-chain score) | `StrategySignal.strength` | IMPLEMENTED_AND_VERIFIED | used as ONCHAIN_SCORE x 100, read only |
| ML features at decision time | `safety/pipeline.record_ml_sample`, `ml/gate_features` | IMPLEMENTED_AND_VERIFIED | outcome labels written at close (`paper_engine.close_position`) |
| Social ML features | — | MISSING | |

## E. Infrastructure

| Component | Where | Status |
|---|---|---|
| Secrets in .env as SecretStr, never returned | `config.Settings`, e.g. `NANSEN_API_KEY` | IMPLEMENTED_AND_VERIFIED |
| Non-secret settings in `platform_settings`, audited | e.g. `paper_execution`, `enrichment` | IMPLEMENTED_AND_VERIFIED |
| Daily call budget pattern | `enrichment.calls_today / spend` (Redis) | IMPLEMENTED_AND_VERIFIED |
| Resource pressure level | `resources.level` | IMPLEMENTED_AND_VERIFIED |
| Telegram for errors | `notify.alert_error` (throttled) | IMPLEMENTED_AND_VERIFIED |

## What the audit decided

1. Sellable-amount protection is a real gap (C: dust, uneconomic partials, no
   schedule). It is added as a pure planner (`exit_plan`) applied to paper
   exits and, for LIVE, recorded only until the operator switches it on.
2. X data does not exist anywhere. It is added as an optional, budgeted,
   shadow-only enrichment that never touches the gate.
