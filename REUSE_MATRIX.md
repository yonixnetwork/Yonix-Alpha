# YonixAlpha — Reuse Matrix (Phase 0)

Legend for **Reusable?**: `Pattern` = reuse the design/algorithm, rewrite the code · `Port` =
adapt existing code with moderate changes · `No` = do not reuse, build fresh · `Replace` = a
better alternative (usually an official SDK) should be used instead.

| Repository | Component | Reusable? | Reason | Changes Required | Security Risk | License | Target Module |
|---|---|---|---|---|---|---|---|
| hyperliquid-grid-trading-bot | `src/risk.py` circuit breaker (drawdown % + range-break %) | Pattern | Clean, minimal threshold-based kill-switch shape | Generalize from single-asset/range to portfolio-level checks | None found | Unclear (README claims MIT, no LICENSE file) | `services/decision/risk_engine` |
| hyperliquid-grid-trading-bot | `src/state.py` weighted-avg P&L accounting | Port | Correct Decimal-based avg-entry/realized/unrealized math, flip-through-zero handled | Key by symbol instead of single asset | None found | Unclear | `services/decision` position tracking |
| hyperliquid-grid-trading-bot | `bot.py::_flatten()` cancel-then-market-close sequence | Pattern | Good emergency de-risk ordering with failure alerting | Generalize across execution adapters | None found | Unclear | `services/decision/risk_engine` kill switch |
| hyperliquid-grid-trading-bot | `src/config.py` YAML+dotenv config/secrets split with fail-fast validation | Pattern | Good separation of strategy params vs secrets | Replicate shape per engine config | None found | Unclear | `packages/core-py/config` |
| hyperliquid-grid-trading-bot | REST-polling order-diff reconciliation loop shape | Pattern | Useful as a *supplementary* reconciliation even under WS | Rebuild as periodic REST reconciliation against WS state | None found | Unclear | `services/engine-binance-futures` |
| hyperliquid-grid-trading-bot | `src/hyperliquid_client.py` (Hyperliquid SDK wrapper) | No | Exchange-specific, no WS, single-asset only | Full rewrite for Binance | None found | Unclear | — |
| hyperliquid-grid-trading-bot | `src/control_api.py` bearer-token FastAPI control plane | Pattern | Reasonable ops-API skeleton | Add real auth (not static token compared with `!=`), input validation | Low (non-constant-time token compare; mitigated by loopback bind) | Unclear | `apps/api/system` admin endpoints |
| confluence-matrix-forex | `position_state.py` durable idempotent position-state machine | Pattern | Exact class of bug (state not surviving restart) YonixAlpha must avoid; atomic JSON write + prune-to-live-set | Swap MT5 ticket → exchange-agnostic position ID; back with DB/Redis not file | None found | Unclear (README claims MIT, no LICENSE file) | `services/decision/state_machine` |
| confluence-matrix-forex | `executor.py::calc_lot_size` fixed-%-risk sizing formula | Pattern | Canonical `risk_amount / (stop_distance × value_per_unit)` shape, asset-agnostic | Parameterize behind `InstrumentSpec` interface (tick value/size, volume step) | None found | Unclear | `services/decision/risk_engine` position sizing |
| confluence-matrix-forex | `executor.py::partial_close_and_move_be` + guard-before-act sequencing | Pattern | Generic two-step reduce+tighten-stop skeleton | Fix known bug: don't mark "done" if SL-move step fails after successful partial close | None found | Unclear | `services/decision` exit engine |
| confluence-matrix-forex | `backtest.py::compute_metrics` R-multiple-based metrics | Pattern | Asset-agnostic PnL-in-risk-units backtest reporting | Reuse formula shape in shared backtest framework | None found | Unclear | Backtesting framework (Phase 34) |
| confluence-matrix-forex | `analysis.py::compute_score` weighted multi-factor confluence scoring | Pattern (scaffold only) | Scoring/gating/funnel-diagnostics architecture is reusable; the specific factors (RSI/ATR/forex structure) are not | Replace all factors with Solana/Binance-specific features; keep weighted-sum + funnel-diagnostic scaffold | None found | Unclear | `services/decision/signal_engine` |
| confluence-matrix-forex | `connection.py`, MT5-specific order dicts | No | Tied to MetaTrader5 IPC API | N/A — not portable | None found | Unclear | — |
| solana-token-scanner | `scanner.py` WebSocket connect/subscribe/reconnect loop | Pattern (minimal) | Clean minimal skeleton: certifi SSL context, subscribe-by-JSON, layered exception handling with backoff | Add message-type routing, schema validation, queue/backpressure, persistence | None found | None (no LICENSE, no claim) | `services/engine-solana-discovery` ingestion |
| solana-token-scanner | PumpPortal `subscribeNewToken` payload field mapping | Pattern | Documents free-tier pump.fun creation-event fields (mint, bondingCurveKey, creator, initialBuy, vSolInBondingCurve) | Use as schema reference only | None found | None | `services/engine-solana-discovery` schema |
| solana-token-scanner | `display.py` | No | Terminal cosmetics only | N/A | None found | None | — |
| solana-token-scanner | Migration/graduation detection | No — does not exist | Repo never subscribes to migration events | Build entirely new (Engine B) | N/A | N/A | `services/engine-solana-migration` |
| solana-token-scanner | Existing-token momentum/volume-quality analytics | No — does not exist | No volume/holder/wallet analytics of any kind present | Build entirely new (Engine C) | N/A | N/A | `services/engine-solana-momentum` |
| meta-muse-crossover-strategy | `bot.py::get_position()` REST-truth reconciliation | Pattern | Always re-derive position state from exchange rather than trusting cache | Generalize beyond single hardcoded symbol | None found | Unclear (README claims none explicitly; no LICENSE) | `services/engine-binance-futures` |
| meta-muse-crossover-strategy | `bot.py::has_open_protection()` protective-order re-verification per tick | Pattern | Defensive re-placement of missing SL/TP orders after restart/crash | Extend with client-order-ID idempotency (currently absent) | None found | Unclear | `services/engine-binance-futures` position manager |
| meta-muse-crossover-strategy | `calculate_quantity`/`round_price` via ccxt precision helpers | Pattern (concept only) | Exchange-precision-aware rounding is the right idea | Reimplement using Binance official SDK's `exchangeInfo`, not ccxt | None found | Unclear | `services/engine-binance-futures` order sizing |
| meta-muse-crossover-strategy | `_protective_order_params()` Binance STOP_MARKET/TAKE_PROFIT_MARKET semantics doc | Pattern (reference) | Useful documentation of `closePosition=True` semantics | Reference only when wiring official SDK order params | None found | Unclear | `services/engine-binance-futures` |
| meta-muse-crossover-strategy | ccxt-based execution/connectivity layer as a whole | Replace | ccxt is a generalized abstraction; misses Binance-specific WS user-data stream, funding history, liquidation fields, batch orders | Use Binance's official `binance-futures-connector-python` directly | None found | Unclear | `services/execution/binance_futures_executor` |
| meta-muse-crossover-strategy | `control_api.py` | Pattern (minimal) | Bearer-token status/close/config blueprint with secret-key redaction-by-regex | Needs real RBAC/auth, bind to loopback not `0.0.0.0` | Low (non-constant-time compare; binds `0.0.0.0`) | Unclear | `apps/api/system` |
| meta-muse-crossover-strategy | 9/21 EMA inverse-divergence strategy logic | No | Unbacktested, unproven edge, forex/crypto-agnostic signal not validated | Do not port as-is; if used, must be independently backtested first | N/A | Unclear | — |
| solana-sniper-jupiter-swap-api | `swap.py::get_quote()` Jupiter `/swap/v1/quote` call | Port | Correct, current Jupiter v1 quote flow with slippage-bps and decimal normalization | Convert to async, structured errors, remove prints | None found | Unclear (README claims none; no LICENSE) | `services/execution/jupiter_executor` |
| solana-sniper-jupiter-swap-api | `swap.py::build_swap_transaction()` | Port | Modern priority-fee pattern: `dynamicComputeUnitLimit`, `dynamicSlippage`, `priorityLevelWithMaxLamports` | Parameterize, add config for `skip_preflight` (default False in prod) | None found | Unclear | `services/execution/jupiter_executor` |
| solana-sniper-jupiter-swap-api | `swap.py::sign_and_send()` | Port | Correct solders `VersionedTransaction` signing pattern | Add blockhash-expiry-aware retry, idempotent submitted-txid tracking | None found | Unclear | `services/execution/jupiter_executor` |
| solana-sniper-jupiter-swap-api | `swap.py::get_mint_decimals()` | Port | Small correct SPL mint-decimals lookup via raw JSON-RPC | Add caching layer | None found | Unclear | `services/execution/jupiter_executor` utils |
| solana-sniper-jupiter-swap-api | Bonding-curve execution capability | No — does not exist | Assumes Jupiter routes everything; fails generically on pre-migration tokens | Build a new `bonding_curve_executor` with direct pump.fun-equivalent program interaction | N/A | N/A | `services/execution/bonding_curve_executor` |
| solana-sniper-jupiter-swap-api | Private-key loading pattern (env-var, base58, never logged) | Pattern | Verified clean across full git history; no exfiltration vector | Add KMS/vault option for production, keep env-var as local-dev fallback | None found (verified clean) | Unclear | `packages/core-py/wallet` |

## Summary by repo

| Repository | Overall Verdict |
|---|---|
| hyperliquid-grid-trading-bot | Functional reference for risk/position-accounting patterns; execution layer not portable (Hyperliquid-specific, no WS) |
| confluence-matrix-forex | Strongest generic risk/position-management pattern source of the five; no portable strategy logic |
| solana-token-scanner | Tutorial-grade; only a minimal WS-connect idiom is usable; Engines A/B/C must be built new |
| meta-muse-crossover-strategy | Execution layer should be replaced with Binance's official SDK; reuse only reconciliation patterns |
| solana-sniper-jupiter-swap-api | Solid Jupiter v1 quote/build/sign/send reference; confirms a bonding-curve-native executor must be built separately |

## Cross-repo conclusion

No reference repository provides: a production database layer, Binance/Solana WebSocket
infrastructure beyond a single read-only PumpPortal subscription, migration/graduation
detection, existing-token momentum analytics, a bonding-curve-native execution adapter, ML
pipeline, backtesting framework, web dashboard, or authentication system. These are all net-new
builds for Phases 1–8. The genuinely reusable material across all five repos amounts to a
handful of well-tested *patterns* (idempotent state machines, risk-sizing formulas,
REST-truth reconciliation, Jupiter v1 API call shapes) — no wholesale code merge is appropriate,
consistent with the project brief's explicit instruction not to blindly combine repositories.
