# Pump.fun execution: research, buy-path audit, findings

Date: 2026-09-26. This audit follows the path from discovery to sell in the source code. Every statement about
YonixAlpha below points to code. Every external statement says whether it was verified, and from where.

## 1. Sources and access from the build environment

| Source | Reached? | Used for |
|---|---|---|
| pump-fun/pump-public-docs (official, `raw.githubusercontent.com`) | **Yes**, read 2026-09-26 | bonding curve, buy/sell bounds, `complete`, `migrate`, PumpSwap |
| PumpPortal docs (pumpportal.fun) | No (egress blocked). The trading-local integration was built from them earlier (`solana/pumpportal.py`) | unsigned transaction builder |
| helius-sdk 3.2.0 (npm, official) | Yes (package types) | `getProgramAccountsV2` shape |
| Helius, solana.com, GMGN, Axiom docs | **No** (egress blocked) | — |

Nothing here is taken from private or undocumented APIs, and no proprietary code was copied.

## 2. Research table

| Resource | Capability | Publicly documented? | Relevant? | Implementation option | Security / risk | Decision |
|---|---|---|---|---|---|---|
| Pump program (official docs) | Coins trade on a bonding curve from launch, without seeded liquidity | Yes (PUMP_PROGRAM_README) | Yes: fresh tokens are bought on the curve, not in a pool | Already used: curve model from `BondingCurve` reserves; buy/sell simulated exactly | — | KEEP |
| Pump program | `buy(amount, max_sol_cost)` / `sell(amount, min_sol_output)` | Yes | Yes: hard bounds on every trade | Enforced by `txguard.GuardExpectation` (`max_sol_in_lamports`, `min_sol_out_lamports`) before signing | Unbounded trades refused | KEEP |
| Pump program | `complete = true` when the curve's real tokens reach 0. `migrate` (permissionless) moves liquidity to PumpSwap | Yes | Yes: a completed curve cannot be traded | Positions switch to PumpSwap on migration (**fixed in this change**, §4.1) | Selling a completed curve cannot fill | FIXED |
| PumpSwap (official docs) | Canonical pool PDA per mint; constant-product AMM | Yes (PUMP_SWAP_README) | Yes: migrated tokens and post-migration exits | Already used: `solana/pumpswap.py` verifies owner, mints and reserves | — | KEEP |
| PumpPortal Local Transaction API | Returns an unsigned buy/sell transaction for `pool = pump / pump-amm / auto`; the caller signs | Yes (docs not reachable now) | Yes: the live provider | Already used: every transaction is decoded and checked by `txguard` (allowed programs, amounts, fee transfers) before signing | Key never leaves the server | KEEP |
| PumpPortal Lightning API | Custodial: PumpPortal holds the wallet key | Yes | No | — | Third party holds the funds | REJECT (earlier audit) |
| PumpPortal data WebSocket | New tokens, trades, migrations | Yes | Cross-check only | Already used (`solana/pumpportal_ws.py`) | — | KEEP |
| Solana RPC `logsSubscribe` (Helius) | Real-time pump.fun events | Yes | Primary discovery stream | Already used (`data-solana` → `pump_stream`) | — | KEEP |
| Priority fees and slippage | Compute-unit price and slippage bounds per trade | Yes (Solana, PumpPortal) | Yes | Already configurable: Live settings `entry_slippage_pct`, `priority_fee_sol`, and a `max_priority_fee_sol` cap enforced by the guard | Fee capped | KEEP |
| Transaction confirmation | Signature status, then the transaction's balance changes | Yes (Solana RPC) | Yes | Already used: a fill counts only when the wallet's SOL and token balances actually changed (`live_exec`) | No invented fills | KEEP |
| Helius `getProgramAccountsV2` | Paginated program-account scan | Yes (SDK types) | Tried for creator history | Measured live: it scans the whole program, not only matching accounts | Cost and latency | REJECT (creator count) |
| GMGN / Axiom / FOMO terminals | Commonly advertised: new-pair feeds, one-click and auto buy/sell, TP/SL, trailing, wallet tracking, PnL, MEV-protected submission | **NOT VERIFIED** (docs unreachable from this environment) | Feature comparison only | Nothing copied. YonixAlpha has feeds, auto buy/sell, TP1–3 / stop / trailing, exit intelligence, live PnL. It has no wallet copy-trading and no MEV-protected relay | Private APIs not used | NOT VERIFIED |
| `solana_pumpswap_migration_bot` | PumpSwap migration sniping | Repo reviewed earlier | Reference | — | Hidden 0.5% referral fee to a third party | REJECT (earlier audit) |
| MemeBot (private) | Pump.fun sniper using PumpPortal Lightning | Repo reviewed earlier | Filter ideas | Filters adopted; custodial execution replaced by Local + guard | Custodial | PARTIALLY USE |

## 3. The buy path, traced in code

For a fresh Pump.fun token:

| # | Question | Where, in code | Answer |
|---|---|---|---|
| 1–2 | Discovered? Stored? | `data-solana` `logsSubscribe` → `pump_stream.ingest_logs` (Redis meta, curve, trades) | Yes. Verified live: `verify_live` showed 27,292 events in 60 s |
| 3 | Observed? | `engine-solana-discovery/funnel.observe_fresh` → `token_observations` | Yes: T0 / T+5 / T+10 checkpoints, outcome and reasons stored |
| 4 | Activity increasing? | `solana/observation.evaluate` → trend | This is **activity**, not a signal (§6) |
| 5 | BUY signal? | `strategies/solana.fresh_launch_signal`: buy/sell volume ratio, trade acceleration, positive price change over 5 min | Stored on every assessment |
| 6–7 | EXECUTE? Which gate blocked? | `safety/gate.assess`, persisted every ≤30 s per candidate (`risk_assessments`) with every finding | Now counted per code by the execution funnel (§5) |
| 8 | Who receives EXECUTE? | `decision-engine/gate_eval.evaluate_with_gate`: PAPER target → `paper_engine.open_position`; LIVE target → `live_trading.enter_live` (pending position + BUY order) | Yes |
| 9–13 | Build, sign, submit, get signature, confirm? | `paper-trading/live_worker` → `live_trading.process_order` → `SolanaLiveExecutor.execute`: PumpPortal build → `txguard` → sign → simulate → send → confirm → read fill from balance changes | Implemented and tested at the provider boundary. **Never run against a real wallet** (§7) |
| 14–16 | Database, position manager, dashboard? | `apply_outcome` → position `open`, candidate ENTERED → MANAGING; `gate_manage` manages it; the Live and Paper pages list it | Yes |
| 17 | Can it sell? | `gate_manage` → `manage_live_position` → `request_live_exit` → the same worker | Yes before migration. **After migration it could not** (fixed, §4.1) |

## 4. Defects found and fixed

### 4.1 A live curve position was unsellable after migration

A LIVE position bought on the curve kept `execution_route = "pump"`. After migration, every exit (stop, take-profit,
emergency) asked PumpPortal for a bonding-curve sell of a completed curve, which cannot fill.

**Fix** (`paper-trading/gate_manage._note_migration`): when an open curve position is first priced from the PumpSwap
pool, it:

- moves to market state POST_MIGRATION (`lifecycle = MIGRATED`, `pool` = canonical pool);
- routes live exits to `pump-amm`;
- records a `position_migrated` timeline event.

The position is **not** closed because of migration; management continues.

Test: `test_curve_position_keeps_managing_through_migration_and_sells_on_pumpswap` (Scenario B).

### 4.2 The live plan ignored the SOL reserve

The gate sized a LIVE entry against the whole wallet. `enter_live` then refused any size above wallet minus
`min_sol_reserve`, so the result was `live_entry_refused` and the token was REJECTED.

**Fix** (`safety/pipeline.load_controls`): live sizing uses wallet minus reserve as the available balance, so the size
is reduced rather than refused. Risk (max loss) is still measured on the whole wallet.

Test: `test_live_sizing_respects_the_sol_reserve`.

### 4.3 A failed buy ended the token forever

Any failed live buy rejected the candidate.

**Fix** (`live_trading.apply_outcome`, state machine ENTRY_PENDING → ANALYZING):

- A buy that failed or expired **without delivering tokens** returns the token to the gate once
  (`MAX_ENTRY_ATTEMPTS = 2`). The gate re-checks everything with fresh data before any new buy; there is no blind
  resend.
- The second failure rejects the token.
- A duplicate is impossible: one live position per mint (`enter_live`).
- Each failure is recorded with its stage: `BUY_TRANSACTION_BUILD_FAILED`, `BUY_REFUSED_BY_TRANSACTION_GUARD`,
  `BUY_SIMULATION_FAILED`, `BUY_SUBMISSION_FAILED`, `BUY_FAILED_ON_CHAIN`, `BUY_CONFIRMATION_TIMEOUT` (and the SELL_
  equivalents).

Tests: Scenario D (`test_unfilled_buy_never_opens_a_position`, `test_second_buy_failure_rejects_and_never_duplicates`).

## 5. Why zero buys? How the question is now answered with data

The decision logic was **not** changed. Instead, every stage is now counted from the database:

- **Command on the server:** `python -m yonixalpha_core.tools.execution_funnel --hours 24`, or `--mint <MINT>` for
  one token, evaluation by evaluation.
- **API:** `/api/control/execution-funnel` and `/api/control/execution-funnel/token/{mint}`.
- **Dashboard:** Fresh Observation → **Execution funnel**, and per token ACTIVITY / SIGNAL / RISK / EXECUTION /
  POSITION.

It reports:

- the counts observed → candidate → assessed → **BUY signal** → **executable** → position → live order;
- the finding codes that blocked tokens that *had* a BUY signal;
- the HIGH findings behind "needs approval" decisions (AUTO turns those into NO_TRADE);
- near misses (exactly one blocking code);
- execution failures, order errors and latencies;
- the modes and locks in force, and a plain-language diagnosis.

Causes proven by the code, pending your numbers:

1. **Global mode PAPER.** No live buy is ever sent; executable decisions open *paper* positions.
2. **Strategy mode PAPER or MANUAL plus any HIGH finding.** Overall risk above `max_risk_level_for_auto` (MODERATE)
   turns the decision into REQUIRE_MANUAL_APPROVAL. It waits for approval (PAPER/MANUAL) or becomes NO_TRADE (AUTO).
   HIGH findings include `HIGH_VOLATILITY` (≥10% per minute: common on young tokens), `TOP1_HIGH`, `TOP10_HIGH`, and
   the manipulation indicators.
3. **`VOLATILITY_EXCEEDS_MAX_STOP`.** The automatic stop (2 × volatility) is wider than `max_stop_pct` (30%) →
   NO_TRADE.
4. **Wallet size for LIVE.** With 0.083 SOL and `risk_per_trade_pct` 0.01, even the calmest test token gets
   `SIZE_BELOW_MINIMUM` (measured with the real gate; §7).

These are safety rules working as designed. None was loosened. The funnel shows which ones actually stop your tokens,
so any change is your informed decision.

## 6. Activity is not a signal

On the dashboard:

- **ACTIVITY** (INCREASING / STABLE / DETERIORATING) is what the stream saw.
- **SIGNAL** (BUY / none) is the strategy.
- **RISK** (APPROVED / decision) is the gate.
- **EXECUTION** (READY / BLOCKED — code / BUY_SUBMITTED / BUY_CONFIRMED / BUY_FAILED …) is the order.
- **POSITION** (NOT ENTERED / PAPER OPEN / LIVE OPEN · MIGRATED …) is the result.

## 7. Live verification status

| Item | Status |
|---|---|
| Build → guard → sign → simulate → send → confirm → fill, BUY and SELL | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION (provider boundary tested; no real transaction has been sent) |
| Real wallet SOL balance | VERIFIED (read from chain by the order worker; Live page) |
| Token holdings valued in SOL | NOT COMPLETE (counted, not valued) |
| Live position PnL | PARTIALLY VERIFIED (from actual fills and marks; no real fill has happened yet) |
| Trading 0.083 SOL at 1–2% risk | BLOCKED by arithmetic: 2% of 0.083 SOL is 0.0017 SOL of allowed loss. Stop plus costs usually push the size below the 0.01 SOL minimum |

## 8. ML and observation data

| Item | Status |
|---|---|
| Every observed token stored with checkpoints T0 / T+5 / T+10, trend, reasons, metrics | VERIFIED (`token_observations`, 3 days) |
| Every gate evaluation stored, including rejections, with its features and findings | VERIFIED (`risk_assessments`) |
| What happened after a rejection (price 15 min – 6 h later) | VERIFIED (`RiskAssessment.outcome`, fresh engine); shown for review |
| Snapshots at T+30 / T+60 and counterfactual "entered at T+10/20/30" labels | NOT COMPLETE |
| ML trained on non-traded tokens | NOT COMPLETE: training uses executed trades only (`record_ml_sample`); rejected tokens are review data, deliberately not labels, because their outcome ignores costs and exitability |
| Controlled promotion (challenger → validation → champion) | VERIFIED (existing ML service; never promoted automatically) |
