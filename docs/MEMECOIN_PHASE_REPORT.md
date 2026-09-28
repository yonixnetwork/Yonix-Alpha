# Solana memecoin phase: report

Date: 2026-09-28.

Code status: implemented and tested locally, with CI and a real-browser
check.

**Production measurements:** taken on 2026-09-28 after deploying
`a15b8e4`, with the read-only commands in `docs/DIAGNOSTICS_COMMANDS.md`.
They cover the last 30 LIVE orders (10 confirmed, 20 guard refusals from
before the FAdo9 fix) and 5 losing LIVE trades. They are summarized in the
next section; the sections after it describe the code.

## Production measurements (2026-09-28)

### RPC capability matrix (`rpc_check --capabilities`)

| Endpoint | Result | Methods | Versions returned | Classification |
|---|---|---|---|---|
| Helius (`SOLANA_RPC_URL`) | CONNECTED, getSlot 85 ms | all 7 probed methods SUPPORTED (13–93 ms) | version 1 (3 of 3) | OK |
| Alchemy (dashboard) | CONNECTED, 75 ms | all 7 SUPPORTED (14–54 ms) | version 1 (2), version 0 (1) | OK |
| Ankr (dashboard) | UNAVAILABLE | every method, **getSlot included**, answers -32601 "does not exist/is not available" | none | **PROVIDER LIMITATION or configuration**: the configured URL serves no Solana method at all. The manager routes around it. Fix the URL or disable it (System → RPC & Data Providers). |
| Chainstack (dashboard) | AUTHENTICATION FAILED | HTTP 403 on every method | none | **PROVIDER LIMITATION** (key or plan). Replace the key or disable it. |

The version fix is **verified in production**: `getTransaction` declaring
version 1 now returns version-1 transactions from Helius and Alchemy,
where before every endpoint failed. `tx_fixture` captured a real mainnet
version-1 transaction (`4DRJBiQ5…`). All four running services load the
same configuration (revision 23, SYNCED).

### Latency (`trade_report`, 10 confirmed orders)

| Step | Average | Median | Worst |
|---|---|---|---|
| decision → submit (buys, n = 5) | 1553 ms | 1505 ms | 1948 ms |
| decision → confirm (buys, n = 5) | 5541 ms | 6407 ms | 9126 ms |
| queue wait (order → worker pickup) | 1258 ms | 1326 ms | 2046 ms |
| build | 27 ms | 23 ms | 42 ms |
| guard + migration re-check | 32 ms | 26 ms | 73 ms |
| simulation | 19 ms | 18 ms | 27 ms |
| submission | 66 ms | 30 ms | 154 ms |
| submit → confirm | 2702 ms | 1276 ms | 7866 ms |

- **Queue wait is 82% of decision → submit.** Our pipeline from pickup to
  submission takes about 150 ms. Cause (**APPLICATION BUG**, fixed): the
  Telegram notification was sent **before** the order was committed, both
  for BUYs and for every LIVE SELL (stop-losses included). On top of that
  came the 1 s poll and a 30 s wallet reconcile that ran before the orders.
  - Now the order is committed first and the notification is sent after.
  - The worker picks up a new order within 0.2 s.
  - The reconcile runs after the orders.
  - The new figure is to be measured.
- **Confirmation:** 7 of 10 landed in 1.1–2.4 s; 3 of 10 took 4.8–7.9 s,
  which fits a missed first broadcast followed by the 3 s rebroadcast. These
  orders predate slot recording, so inclusion delay cannot yet be told apart
  from polling. Orders from now on record `slots_to_land`. No change to the
  priority fee was made.

### "Bought at a higher price" (`trade_report`, 5 confirmed buys)

| Token | decision → build | build → landing | price impact | costs on top of the trade | total vs decision |
|---|---|---|---|---|---|
| SNAPD | +0.11% | 0.00% | +0.01% | **+70.79%** | +71.00% |
| VSTR | 0.00% | −1.28% | 0.00% | **+51.87%** | +49.92% |
| $WCAT | 0.00% | +0.16% | 0.00% | **+110.14%** | +110.47% |
| DREW | 0.00% | 0.00% | +0.01% | **+33.15%** | +33.16% |
| LOOONG | 0.00% | +0.12% | +0.01% | **+37.95%** | +38.12% |

- **The market, latency, slippage, stale data and price impact are NOT the
  cause.** Each moved the price by at most 1.3%.
- The whole difference is **what the wallet paid on top of the trade**:
  program fees, the network + priority fee, and SOL deposited into new
  accounts.
- From the recorded numbers, this extra is about **0.0016 SOL fixed per buy
  plus about 1.25% of the trade**. On buys of 0.0015–0.005 SOL, that is
  33–110%.
- **Itemized by `cost_report` (2026-09-28, every transaction fully
  explained, residual 0):**

  | Per buy | SOL | Kind |
  |---|---|---|
  | token account for the bought token (170 bytes) | 0.00151384 | **rent deposit**, returned only when the account is closed |
  | network fee (base 0.000005 + priority 0.0001) | 0.000105 | cost |
  | program fees (protocol + creator) | 1.25% of the trade | cost |
  | first buy only (SNAPD): one program account (137 bytes, Pump's per-user volume account) | 0.0013462 | one-time deposit |

- **Sells** pay 1.25% program fees plus the 0.000105 network fee and
  create nothing. **Nothing closes the token account after the sell**, so
  its rent stays locked. Today 5 empty token accounts hold **0.0075692
  SOL** (about 9% of the wallet).
- The token-account rent is **90% of the fixed cost per buy**. Real round
  trip costs, excluding the refundable deposit: about 2.5% of the size plus
  0.00021 SOL.
- Classification: before, this showed only as "FEES" (with a misleading
  "within the slippage limit"). It is now split into program fees, network
  fee and new-account deposits. When a deposit dominates it is classified as
  ACCOUNT_RENT.

### Why the 5 losing LIVE trades lost (`loss_report`, 72 h)

| Token | Exit | After | MAE | Explained by the costs alone | Market move | Cause |
|---|---|---|---|---|---|---|
| SNAPD | exit intelligence EXIT_NOW | 5 s | −41.44% | −41.45% | none | **APPLICATION BUG** |
| $WCAT | exit intelligence EXIT_NOW | 19 s | −52.41% | −52.41% | none | **APPLICATION BUG** |
| VSTR | exit intelligence EXIT_NOW | 47 s | −37.50% | −34.15% | about −5% | **APPLICATION BUG** (the 35% emergency needed the cost markup) |
| LOOONG | stop-loss | 52 s | −36.95% | −27.51% | about −13% | the market fell to the stop |
| DREW | stop-loss | 142 s | −34.19% | −24.90% | about −12% | the market fell to the stop |

- **APPLICATION BUG (fixed):** a LIVE position's high, low and last price
  started at the **cost basis** (all the wallet paid, per token) instead of
  a market price.
  - Exit intelligence's emergency rule "price 35% below the high since
    entry" therefore fired as soon as the costs were above about 54% of the
    trade.
  - SNAPD and $WCAT were sold with the market **unchanged**. Their MAE
    equals the cost markup to the hundredth of a percent.
  - Now the marks start at the fill's market price (our own trade event).
    The cost basis stays the entry price, for PnL.
  - Positions filled before this deploy use their planned entry price as
    the reference.
- **The same bug made every loss read "never went up (MFE 0.00%)"**, and
  therefore SIGNAL_FAILURE. That was an artifact. For these 5 trades the
  real high is unknown, and it is now reported as unknown, not 0%.
- **RISK_MODEL_FAILURE is accurate for all 5.**
  - Each loss was 2.2–3.8× the planned maximum (0.00074–0.00083 SOL).
  - The plan's maximum loss does not include entry costs.
  - At the current size, **the fixed cost of one buy (about 0.0016 SOL) is
    about twice the whole per-trade risk budget**.
  - This is not changed in code; see "Decision needed" below.
- LOOONG and DREW were genuine stop-losses (MARKET moved against the entry
  by about 12–15% within 1–2 minutes).

### Volatility (`loss_report`)

- **1358 of 5000 assessments** in 72 h were blocked by
  AUTO_SL_NO_VOLATILITY. Of these, **1354** had "fewer than 3 returns even
  at 10-second resolution": tokens with too few trades to measure.
- 0 were LOW_CONFIDENCE. Most of the 72 h window predates this deploy, so
  re-measure after 24 h.

### Guard refusals

20 BUY_REFUSED_BY_TRANSACTION_GUARD (FAdo9…), all on 2026-09-27, before
the builder fix. There have been none since.

### Decisions taken (operator, 2026-09-28) and implemented

At the measured trade size, costs were 33–110% of each buy. The operator
chose:

1. **Return the rent deposit.**
   - **After every full exit**, the token's now-empty account is closed and
     its rent (0.00151384 SOL) returns to the wallet. This is the setting
     `auto_reclaim_rent`, default on.
   - **A dashboard button** ("Return rent to wallet", with confirmation)
     closes all empty token accounts. That covers the 5 accounts holding
     0.0075692 SOL today.
   - It is a separate transaction with its own guard (see
     `EXECUTION_PATH_MAP.md` §2b). It never touches a token with an open or
     pending position, and never an account that holds tokens.
   - The refund is booked back to the trade it came from.
2. **Count fixed costs in the risk plan (LIVE only).**
   - A LIVE round trip has a fixed cost: buy and sell network + priority
     fees, plus the reclaim fee. With auto-reclaim off, the unreclaimed
     deposit is counted instead.
   - With the current settings that is **0.000225 SOL** (0.00172384 SOL with
     auto-reclaim off).
   - Loss at the stop = size × loss fraction + fixed cost, and it must stay
     within the maximum loss. The size is reduced to fit.
   - Two cases refuse the trade:
     - FIXED_COSTS_EXCEED_RISK: the fixed cost alone reaches the budget;
     - STOP_INSIDE_COSTS: proportional plus fixed costs reach the stop
       distance.
   - Breakeven includes the fixed cost. Paper sizing is unchanged.
   - **Effect on a wallet of about 0.08 SOL** (from the tests): with a 10%
     stop, proportional costs of about 5.5% plus a fixed cost of about 5.8%
     at that size leave no room, so the trade is refused. With a 16% stop
     (as on the measured trades) it trades, somewhat smaller.
3. **Trade size:** unchanged; that remains the operator's decision.

## Execution (protected path, unchanged)

The working BUY and SELL paths are documented hop by hop in
`docs/EXECUTION_PATH_MAP.md`. Transaction construction, the guard, signing,
simulation, sending and confirmation are unchanged. What was added is
measurement around them:

- every RPC call the executor makes is timed;
- a `TRANSACTION_SEEN` stage is recorded;
- the blockhash slot and the compute-unit limit are recorded;
- the program's own trade event is decoded from the confirmed logs.

**Only changes that affect execution behavior:**
- **RPC priority.** Executor calls are "critical": they are never dropped
  behind background work. Before this change, a background request could
  trigger a 429 cooldown that also blocked the executor.
- **Compute-unit limits** are now dashboard settings. The defaults (200k
  curve, 350k PumpSwap) are exactly the previous constants. The allowed
  range is 120k–400k for the curve and 180k–600k for PumpSwap.
  Measured use was about 96k and 142k.

**Latency (average, median, worst):** measured; see "Production
measurements" above.

**Structural delays visible in code** (sizes to be measured):
- a candidate is re-evaluated at most every 30 s, from a 15 s loop;
- data assembly is sequential, now timed per source in
  `inputs_snapshot.timings_ms`;
- the order worker polls every 1 s;
- orders are processed one at a time, so a SELL can queue behind a BUY's
  confirmation (up to 75 s);
- exits are evaluated every 15 s.

After measurement, only the order pickup delay was fixed (it was 82% of
decision → submit). The others remain as they were.

**Priority fees**
- The total priority fee is `priority_fee_sol`, default 0.0001 SOL,
  capped by `max_priority_fee_sol`.
- There is no Jito tip and no provider-specific fee API.
- `trade_report` groups confirmation speed and slots-to-land by fee and CU
  setting, so the effect of a fee change can be measured before anyone
  raises it.

## RPC

| Finding | Cause | Classification |
|---|---|---|
| `getTransaction` failed on every endpoint with "Transaction version (1) is not supported by the requesting client" | Every call declared `maxSupportedTransactionVersion: 0`. The node reports that version-1 transactions exist and that this client did not accept them. | **APPLICATION BUG** (fixed: every call now declares 1). |
| The -32015 version error counted as an endpoint failure and triggered failover and an alert | Error classification | **APPLICATION BUG** (fixed: it is now a request-level answer; no failover, no health penalty, no alert). |
| Ankr: "the method getTransaction does not exist/is not available" | Ankr does not offer this method on this plan | **PROVIDER LIMITATION**. The manager now remembers it for 6 h and routes the method elsewhere. It shows on the capability matrix. |
| Chainstack 403 (earlier) | Key or plan | **PROVIDER LIMITATION**. Shown as AUTHENTICATION FAILED. |
| HTTP 429 | Free-plan limits | Largely a **PROVIDER LIMITATION**. The application previously made it worse: background lookups shared the executor's endpoint cooldown, and Retry-After was ignored. Now there are per-endpoint background caps, a budget share, cooldowns shared across services through Redis, Retry-After, and shedding (dropped, not queued). |

**How fetched transactions are read (version handling)**
- No fetched transaction is decoded as binary. The node renders it as JSON.
- Pool trade history reads only log messages, which are the same in every
  version.
- Funding lookups read parsed instructions and fail safe to "unknown".
- Fill parsing reads only our own transactions (v0) and refuses any other
  version.

**Measured values:** see "Production measurements" above. Still pending:
per-provider 429 frequency under load. The learned-traffic section was
empty right after the restart.

## Price execution

**The analysis**
- Each confirmed BUY is decomposed: decision price → spot at build → spot
  just before our trade (from the program's trade event) → trade price →
  all-in price.
- Each step is shown as a percentage.
- One cause is named with its evidence: MARKET_MOVED, DATA_STALENESS,
  CURVE_MOVEMENT, PRIORITY_FEE_DELAY, RPC_LATENCY, PRICE_IMPACT, FEES,
  WITHIN_EXPECTED or UNKNOWN.
- A cause is named only when its step is at least 2%.
- RPC_LATENCY is used only when submission itself was slow while the curve
  moved.

**Results for recent trades:** see "Production measurements" above. The
cause was costs on top of the trade price, never the market.

## Decision engine

- **Volatility.** The "requires volatility data" block had two causes.
  - First, a token with fewer than three 10-second returns has no measurable
    volatility.
  - Second (an **APPLICATION BUG**, fixed), for migrated tokens a failed
    pool trade-history lookup, for example from the v1 `getTransaction`
    error, wiped out the pool's price and liquidity too.
  - Now volatility is AVAILABLE (unchanged measurements), LOW_CONFIDENCE
    (at least 6 trades, measured trade to trade; needs approval) or
    UNAVAILABLE (no number, with the reason).
  - The automatic-stop rule itself is unchanged.
- **Deterioration.** Two or more independent, multi-window signs mean an
  automatic entry waits, and a manual BUY asks for confirmation:
  - sellers accelerating;
  - buyers stalling;
  - volume collapsing against its own baseline;
  - a reversal from the local high;
  - a spike without broad buying.

  One sign alone is only reported, so a lull is not treated as a collapse.
  EXIT_SIGNAL_AT_ENTRY is unchanged.
- **Why recent losing trades were entered:** see "Production measurements"
  above.

## ML

- **Recorded now:**
  - every observation rejection or expiry;
  - every gate rejection or expiry;
  - every entry.
- **What each record holds:**
  - the decision snapshot, with market cap at discovery and at decision;
  - the price at T+5s, 10s, 30s, 60s, 5m, 15m and 30m;
  - the peak and the drawdown;
  - whether the token migrated.
- **Traded:** PnL, market cap at entry and exit, MFE/MAE, exit reason,
  entry execution quality.
- **Losses:** a LOSS_ANALYSIS class with flags.
- **ML Review compares:**
  - winners against losers;
  - traded tokens against tokens that were rejected and later went up.
- None of this is read by the gate. Model changes still go only through
  training → validation → challenger → shadow → controlled promotion.

Recording starts with this deploy; earlier trades are covered by
`loss_report`.

## UI

- **Hidden from navigation** (routes and backends untouched):
  - Binance, Bybit and Hyperliquid;
  - Meta Muse and Confluence Matrix;
  - Gold/BTC and grid;
  - External Bots;
  - the futures ML model group.
- **New or redesigned:**
  - the dashboard: live wallet, today LIVE and PAPER, market, system, open
    positions with SELL;
  - the token terminal: header with price, market cap (labelled as FDV on
    Pump.fun), liquidity, state, age and risk; charts; 1m/5m flow; live
    activity from real trade events; BUY/SELL; decision; external links;
  - Token Explorer, Open Positions and Trades;
  - Trade Details: latency bars and the price chain;
  - the RPC capability matrix;
  - the funnel, now DISCOVERED … TRANSACTION BUILT → SIGNED → SUBMITTED →
    CONFIRMED → POSITION OPEN;
  - the manual BUY preview now shows market cap and liquidity.
- **Real-browser check** at 1440 and 400 px: no console errors and no
  horizontal overflow. It found and fixed two bugs: a 500 on the dashboard
  and query-string 422s.

## Tests (local, all passing)

| Suite | Result |
|---|---|
| core-py | 574 passed |
| paper-trading | 69 passed |
| decision-engine | 74 passed |
| engine-solana-discovery | 16 passed |
| engine-solana-migration | 11 passed |
| engine-solana-momentum | 11 passed |
| data-solana | 10 passed |
| data-binance | 14 passed |
| engine-binance-futures | 48 passed |
| execution-futures | 9 passed |
| ml | 23 passed |
| mt5-bridge | 7 passed |
| api | 127 passed |
| web | tsc, lint, build pass |
| ruff | clean |
| migrations | 0016 and 0017 apply on a fresh database |

**New test files:**
- `test_rpc_versions_capabilities`
- `test_execution_analysis`
- `test_entry_quality`
- `test_opportunities`
- `test_token_market`

**Existing tests extended:**
- `test_execution_venues`: RPC trace and CU limit
- `test_execution_funnel`: new stages
- `test_funnel`: NUL name

One scenario assertion changed on purpose: a 12-second-old token with 15
trades is now LOW_CONFIDENCE (not traded, needs approval) instead of
NO_TRADE because volatility was unavailable.

## Follow-up after the production measurements

Fixed (APPLICATION BUG):
- LIVE position marks started at the cost basis, so exit intelligence sold
  on a phantom crash, and MFE/MAE were wrong;
- the LIVE BUY and SELL were committed only after the Telegram call;
- the order worker picked up orders only once per second, after the wallet
  reconcile.

Added (read-only):
- each confirmed order's cost breakdown is now recorded, and the
  misleading "within the slippage limit" text is gone;
- `cost_report` itemizes past orders and the SOL held in empty token
  accounts;
- `loss_report` rebuilds diagnostics for old orders and shows the exit
  intelligence reason.

Tests:
- 3 cost breakdown tests and a `cost_report` test;
- 2 excursion tests and a loss-classification test;
- a fill-marks test;
- a phantom-crash test, which fails on the old code;
- a worker wake test;
- a commit-before-notify test, which fails on the old code.

## Remaining issues

- **Rent reclaim: verified on chain (2026-09-28).** The operator reclaimed
  the 5 empty accounts from the dashboard and the SOL returned to the
  wallet. The automatic close after a full exit has not run on chain yet;
  the next live exit is its first real run. Cost-aware sizing is UNVERIFIED
  on chain until the next live entry.
- **Crash between signing and confirming a reclaim:** reconciliation
  resolves the order, but without the list of closed accounts the refund
  is not booked back to the trades. The SOL is in the wallet either way.
- **Dust:** an account that still holds even 1 raw token after an exit is
  skipped (it cannot be closed). Nothing is burned.
- **Latency after the pickup fix:** to be measured (`trade_report`).
- **Confirmation tail (3 of 10 above 4.8 s):** `slots_to_land` is now
  recorded; to be measured before any priority-fee change.
- **Version-1 transaction fixture:**
  - v1 is served and read in production;
  - the captured transaction is not yet in the test suite, because only its
    first 4000 characters were printed;
  - the parsers still fail closed.
- **Ankr and Chainstack** remain enabled but serve nothing. Disabling them
  is an operator setting.
- **Structural delays** (30 s re-evaluation, serial order worker, 15 s exit
  loop) are unchanged.
- **Futures and other hidden backends still run** (they are only hidden in
  the UI). They can be disabled later with a compose profile.
- **Manual BUY override of preference filters** (earlier request): still
  not implemented. It is blocked by the permission settings.
