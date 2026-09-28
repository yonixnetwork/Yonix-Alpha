# Solana memecoin phase: report

Date: 2026-09-28.

Code status: implemented and tested locally, with CI and a real-browser
check.

**Production measurements are PENDING.** They require the read-only
commands in `docs/DIAGNOSTICS_COMMANDS.md`, run on the server after this
deploy. No latency, price or loss figure below is claimed until that output
exists.

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

**Latency (average, median, worst).** PENDING. Run `trade_report`; it also
measures orders placed before this deploy, from their recorded stages.

**Structural delays visible in code** (sizes to be measured):
- a candidate is re-evaluated at most every 30 s, from a 15 s loop;
- data assembly is sequential, now timed per source in
  `inputs_snapshot.timings_ms`;
- the order worker polls every 1 s;
- orders are processed one at a time, so a SELL can queue behind a BUY's
  confirmation (up to 75 s);
- exits are evaluated every 15 s.

No optimization was made on these, as asked, because nothing has been
measured yet.

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

**Measured values still PENDING** (run `rpc_check --capabilities` and
`tx_fixture --version 1`):
- per-provider 429 and 403 frequency;
- which versions each provider serves;
- method support.

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

**Results for recent trades:** PENDING (`trade_report`).

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
- **Why recent losing trades were entered:** PENDING. `loss_report` shows,
  for every loss, the entry features, the warnings present at entry, the
  exit check and the class.

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

## Remaining issues

- **All production measurements** (latency, price causes, loss causes,
  provider matrix): PENDING the diagnostics output.
- **Version-1 transaction fixture:** it will come from real mainnet data
  (`tx_fixture`). Until then, v1 handling is covered by request-level tests
  only, and the parsers fail closed.
- **Structural delays** (30 s re-evaluation, 1 s poll, serial order worker,
  15 s exit loop) are identified but not optimized, pending measurement.
- **Futures and other hidden backends still run** (they are only hidden in
  the UI). They can be disabled later with a compose profile.
- **Manual BUY override of preference filters** (earlier request): still
  not implemented. It is blocked by the permission settings.
