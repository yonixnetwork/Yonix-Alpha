# Working execution path: architecture map

LIVE BUY and SELL are confirmed working in production (deploy 29cffd9 and
later). This document records that path exactly as the code runs it. It is
**protected**: later changes are made *around* this path, not to the
transaction construction inside it. Every network request between the BUY
decision and submission is listed here.

## 1. BUY: signal → position

| # | Hop | Where | Network requests | Timing source |
|---|---|---|---|---|
| 1 | **Signal**: launch stream → observation funnel → candidate | `engine-solana-discovery/app/funnel.py`, every `FUNNEL_INTERVAL_SECONDS = 10` | none (Redis stream data) | `trading_candidates.created_at` |
| 2 | **Decision**: the gate evaluates the candidate | `decision-engine/app/gate_eval.py::evaluate_with_gate`, loop every `EVAL_INTERVAL_SECONDS = 15`, each candidate at most every `REEVALUATE_EVERY_SECONDS = 30` | see the assembler rows below | `risk_assessments.evaluated_at` |
| 2a | Data assembly (fresh) | `solana/assembler.py::assemble_fresh` | `getAccountInfo` (mint), `getAccountInfo` (curve), `getTokenLargestAccounts` + `getMultipleAccounts` (holders), funding links (`getSignaturesForAddress`/`getTransaction` per early buyer, cached), creator history. All run **sequentially**. | new: `inputs_snapshot.timings_ms` |
| 2b | Data assembly (migrated) | `assemble_migrated` | the above plus pool trades (`getSignaturesForAddress` + `getTransaction`), pool state, SOL/USD, a Jupiter quote | same |
| 3 | **Approval**: `assess()` → executable LIVE plan | `safety/gate.py`, `safety/planning.py` | none | same as 2 |
| 4 | **Order**: pending position + `execution_orders` BUY row (`status=PENDING`, limits: max SOL in, max fee transfer, max priority fee), committed; the "LIVE BUY submitted" notification (Telegram) is sent **after** the commit | `live_trading.enter_live`, `gate_eval.py` | none (DB) | `execution_orders.created_at` |
| 5 | **Pickup**: the order worker picks up PENDING orders | `paper-trading/app/live_worker.py`: loop every `POLL_SECONDS = 1.0`, and between loops a new PENDING order is picked up within `PENDING_CHECK_SECONDS = 0.2`; the 30 s wallet reconcile runs after the orders, not before; orders are processed **one at a time** | DB only | first stage `at` |
| 6 | **Venue** (from on-chain state) | `solana/venue.py::resolve` | 1 × `getMultipleAccounts` [mint, curve PDA, canonical pool] | stage `VENUE_RESOLVED` |
| 7 | **Route / quote**: native builder computes the output from the reserves just read (Jupiter is used only for non-Pump venues) | `solana/tx_builders.py::NativePumpBuilder` | `getAccountInfo` Global / GlobalConfig only when the 60 s cache is stale | stage `TRANSACTION_BUILT` |
| 8 | **Transaction build**: compute budget + ATA + Pump `buy` (18 accounts) / PumpSwap WSOL wrap + `buy` (26 accounts) | `pump_tx.py` | 1 × `getLatestBlockhash` (fetched right before compiling) | `TRANSACTION_BUILT` |
| 9 | **Transaction guard** | `solana/txguard.py::inspect` | none | `TRANSACTION_GUARD_PASSED` |
| 10 | **Migration race re-check** | `live_exec.execute` | 1 × `getMultipleAccounts` | (inside 9→11) |
| 11 | **Sign**; the signature is persisted before sending | `live_exec.execute`, `on_signed` | DB write | `TRANSACTION_SIGNED` |
| 12 | **Simulate** (`sigVerify: true`) | `live_exec.execute` | 1 × `simulateTransaction` | `SIMULATED` |
| 13 | **Submit** (`skipPreflight`, `maxRetries: 0`); the same signed bytes are rebroadcast every 3 s | `_send_and_confirm` | `sendTransaction` | `TRANSACTION_SUBMITTED` |
| 14 | **Confirmation**: poll every 1 s | `lookup` | `getSignatureStatuses`, then `getTransaction` (jsonParsed, version 0: our own transactions are v0) | `TRANSACTION_CONFIRMED` |
| 15 | **Position**: the fill comes from the wallet's pre/post balances; the position opens. Entry price = cost basis (all the wallet paid, for PnL); high, low and last price start at the **market** price of the fill (our trade event's price) | `live_trading.apply_outcome`, `market_fill_price` | none | `FILL_VERIFIED`, `execution_orders.confirmed_at`, `plan.fill` |

**RPC requests between the BUY decision and submission** (order pickup →
submit):
- `getMultipleAccounts`
- (`getAccountInfo` × 0–2 when the cache is stale)
- `getLatestBlockhash`
- `getMultipleAccounts`
- `simulateTransaction`
- `sendTransaction`

That is 5 requests, or 7 when the cache is stale. All go through the
paper-trading service's `RpcManager`.

## 2. SELL: exit → closed position

| # | Hop | Where | Network |
|---|---|---|---|
| 1 | Mark price and exit rules (stop, trailing, take-profits, exit intelligence) | `paper-trading/app/gate_manage.py` → `live_trading.manage_live_position`, main loop every `LOOP_INTERVAL_SECONDS = 15` | price source (stream curve / pool over RPC) |
| 2 | SELL order (`min_sol_out` from the expected proceeds and the exit slippage, which widens after each failed attempt) | `live_trading.request_live_exit` | DB |
| 3–14 | Same worker and executor as BUY steps 5–14, with the sell instructions | as above | as above |
| 15 | Proceeds from the fill; the position closes below the dust threshold. With `auto_reclaim_rent` on (default), a RENT order is queued for that token | `apply_outcome`, `request_rent_reclaim` | none |

## 2b. RENT: closing the empty token account (rent deposit back to the wallet)

Every buy opens the wallet's token account for the bought token (rent
0.00151384 SOL, measured). Closing it once it holds nothing returns the
deposit. This is a separate transaction; the BUY and SELL paths above are
unchanged.

| # | Hop | Where | Network |
|---|---|---|---|
| 1 | RENT order: after a full exit (one token), or from the dashboard button (all empty accounts, with confirmation; refused unless the worker is ready) | `live_trading.request_rent_reclaim`, `POST /api/live/reclaim-rent` | DB |
| 2 | Worker picks it up like any order; tokens with an open, pending or needs-review position are excluded | `live_trading.process_order` | DB |
| 3 | Accounts read from chain: only ours, zero tokens, initialized, no other close authority, no withheld transfer fees; nothing to close → SKIPPED, nothing sent | `solana/rent_reclaim.scan` | `getTokenAccountsByOwner` × 2 |
| 4 | Build: compute budget + one CloseAccount per account (at most 8), destination and authority our wallet; priority fee 0.00001 SOL | `rent_reclaim.build` | `getLatestBlockhash` |
| 5 | Dedicated guard: only compute-budget and CloseAccount instructions, exactly the expected accounts, each closed to our wallet by our wallet, no lookup tables, priority fee bounded | `txguard.inspect_close_accounts` | none |
| 6 | Sign → simulate → send → confirm (same code as trades) | `SolanaLiveExecutor.close_token_accounts` | as BUY 11–14 |
| 7 | The refund (rent − its share of the network fee) is booked to the closed trade it belongs to (once): realized PnL, ML label and outcome record are re-booked; a notification reports it, a failure is reported too | `live_trading._apply_reclaim`, `paper_engine.rebook_realized` | none |

## 3. Structural delays visible in the code (to be confirmed by measurement)

These come from reading the code. Their real size is measured by the timing
added in this phase (`execution_orders.timing`), not assumed.

1. **Decision cadence**: a candidate is evaluated at most every 30 s, from
   a loop that wakes every 15 s.
2. **Sequential data assembly**: each RPC lookup in 2a/2b waits for the
   previous one.
3. **Order pickup**: measured on 2026-09-28 at 1.26 s average (median
   1.33 s, worst 2.05 s, n = 10). It had three parts: the Telegram
   notification was sent before the order was committed (BUY and SELL);
   a 1 s poll; the 30 s wallet reconcile ran before the orders. All three
   were fixed (see the phase report); the new figure is to be measured.
4. **One order at a time**: while a BUY waits up to 75 s for confirmation,
   a SELL queued behind it waits too.
5. **Exit checks every 15 s**: stops and trailing exits are evaluated on the
   paper-trading loop, which also runs outcome tracking and follow-ups in
   the same cycle.
6. **Shared RPC manager**: the executor shares one `RpcManager` with that
   service's background follow-ups and outcome tracking. Before this phase,
   a 429 caused by background traffic also cooled the endpoint down for
   execution.

## 4. Priority fees (as built)

- `ComputeBudget.SetComputeUnitLimit`: 200,000 (curve) or 350,000
  (PumpSwap).
- `SetComputeUnitPrice` = `priority_fee_sol` (dashboard, default 0.0001 SOL)
  spread over that limit:
  `micro_lamports = lamports × 1e6 / units`.
- The total priority fee equals `priority_fee_sol`. The guard enforces
  `max_priority_fee_sol`.
- No Jito tip and no provider-specific fee API is used.
- Mainnet dry run (2026-09-28): the curve buy consumed 96,127 CU and the
  PumpSwap buy 141,716 CU.

Validators rank by price per compute unit, and the limit is about twice the
consumption. The same total fee therefore buys a lower per-CU price than a
tighter limit would. This is recorded for the latency analysis; the limits
are not changed without measurements from real trades.
