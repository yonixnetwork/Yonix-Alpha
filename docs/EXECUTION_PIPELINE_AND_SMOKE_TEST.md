# Execution pipeline, EXIT_SIGNAL_AT_ENTRY and the live smoke test

This document covers four things:

- what each pipeline status means, so a positive analysis is never read as a buy;
- the new entry protection, `EXIT_SIGNAL_AT_ENTRY`;
- the controlled `LIVE_EXECUTION_SMOKE_TEST`;
- the LIVE/PAPER wallet view, and the observation follow-ups (T+5m … T+60m).

Nothing here changes the global mode. Normal trading stays in PAPER until you switch it.

## 1. What "PROMOTE" means, and the status ladder

**Audit result.** In the code, `PROMOTE` is an outcome of the fresh-token observation window
(`solana/observation.py`). It means the token had enough activity and was not deteriorating, and it was handed to
the safety gate as a candidate.

`PROMOTE` is **not** any of the following:

- an ML recommendation;
- a BUY signal (the strategy signal is evaluated later, inside the gate);
- a risk approval;
- an execution approval.

Each stage is now a separate fact, counted from the database (`execution_funnel.pipeline`). A token's stage is the
furthest one it reached.

| Stage | Meaning | Source |
|---|---|---|
| `OBSERVED` | The funnel (or a momentum/migration prefilter) saw the token | `token_observations`, candidates |
| `ANALYSIS_POSITIVE` | Activity looked positive: trend INCREASING/STABLE, or a prefilter passed | observation trend |
| `PROMOTE` | Handed to the safety gate ("deserves further consideration") | `trading_candidates` |
| `BUY_SIGNAL` | The strategy signal qualified in at least one gate evaluation | `risk_assessments` |
| `RISK_APPROVED` | BUY signal, and no risk-side blocker remains (see below) | findings by category |
| `EXECUTION_APPROVED` | Gate decision EXECUTE / REDUCE_SIZE: permitted to submit the buy | `risk_assessments.executable` |
| `BUY_SUBMITTED` | LIVE: transaction signed and sent. PAPER: simulated entry | `execution_orders.submitted_at` / position |
| `BUY_CONFIRMED` | LIVE: confirmed on chain with the wallet balance change. PAPER: simulated fill | order `CONFIRMED` + fill |
| `POSITION_OPEN` → `SELL_SUBMITTED` → `SELL_CONFIRMED` → `POSITION_CLOSED` | | positions, SELL orders |

**Risk-side blockers** are findings in these categories: token, holders, flow, market, liquidity, data, creator.
`RISK_ABOVE_AUTO_CEILING` and `AUTO_NO_APPROVAL` also count as risk-side, because HIGH findings drive them.

**Execution-side blockers** are sizing, costs, account limits, routes and mode.

**Where to see it:**

- `python -m yonixalpha_core.tools.execution_funnel --hours 1` prints:
  - `PIPELINE` (tokens per stage);
  - `BLOCKED` (by group: EXIT_SIGNAL_AT_ENTRY, liquidity, sellability, stale/missing data, route, sizing/account,
    mode, risk);
  - `PROMOTED BUT NEVER ASSESSED`;
  - `FINAL BLOCKER` counts;
  - the latest tokens, each with its furthest stage and exact final blocker.
- `--mint <MINT>` starts with `PIPELINE: furthest stage …; final blocker …`.
- The dashboard's execution funnel (Solana → Observing) shows the same ladder and a per-token table.

### Why did PROMOTE not become a buy? (production, 2026-09-27)

In the 30-minute window 01:55–02:25:

- 74 tokens were PROMOTED (discovery candidates created);
- only **13** fresh tokens got a gate evaluation;
- **62** discovery candidates were rejected.

From the code, exactly one path rejects a pump-stream candidate without writing an assessment:
`strategy <engine> is OFF` (`gate_eval.py`). Every other rejection writes the assessment first. The other paths are
the gate's REJECT, the time limit, an entry failure, and an operator decline on the Decisions page.

This is **UNVERIFIED** for that window. The funnel now prints each such candidate's own recorded reason under
`PROMOTED BUT NEVER ASSESSED`, so the next run answers it from the database instead of from inference.

For the tokens that were assessed, the final blockers were the ones listed in `PUMPFUN_EXECUTION_RESEARCH.md` §5:

- volatility over the maximum stop;
- no volatility data yet;
- duplicate name;
- the one-open-position limit;
- RPC data failures.

The fee bug that blocked every trade was fixed separately (§4.4).

### Fresh tokens and the DEX pool

**Verified in code and tests: fresh (pre-migration) tokens are not gated on a DEX pool.**

- The gate has no `DEX_POOL_NOT_FOUND` code.
- For `solana_fresh` and `solana_momentum` before migration, the curve is the venue (`BONDING_CURVE_MARKET`, LOW,
  informational).
- Execution data comes from the curve model, and the route is `pump`.
- `EXECUTION_UNAVAILABLE` on a fresh token means the curve could not be simulated: the mint or curve RPC failed, or
  the fee is unknown.
- Migrated tokens use the PumpSwap pool and the `pump-amm` route.
- A position bought on the curve switches to `pump-amm` when the token migrates. That fix is unchanged.

**Momentum is a selection category, not a route.**

- The momentum engine only takes tokens still on the curve (`curve.complete` is skipped), so their route is `pump`.
- A momentum token that migrates is handed to the migration engine (pool rules, `pump-amm`), exactly like a fresh one.

## 2. EXIT_SIGNAL_AT_ENTRY

The case that prompted it: `3eSai…pump`, bought at 02:22:23. The exit logic reduced it at 02:22:38 with "volume
collapsed to 8% of the previous 120s window; sellers outnumber buyers 2:1". Its exit windows overlapped almost
exactly with the flow the entry was decided on.

**How it works:**

- **Assembler.** `solana.assembler.entry_exit_check` runs the **existing** `exit_intel.solana_exit_decision` on the
  same pre-entry trades an open position would be judged on:
  - the curve's stream trades for fresh/momentum;
  - the PumpSwap pool flow for migrated tokens.
- **Settings.** It uses `ExitConfig.from_settings`. The rules are not duplicated and the thresholds are unchanged.
- **Entry state.** Entry liquidity is set to current liquidity. There is no holder change or price high yet. So only
  the flow rules can fire: sell pressure, seller dominance, volume collapse, creator selling.
- **Gate.** `REDUCE`, `EXIT` or `EXIT_NOW` becomes the finding `EXIT_SIGNAL_AT_ENTRY` (TRADING, HIGH, action
  **WAIT**).
- **Result.** No position is opened, and the candidate stays under analysis and is re-evaluated every 30 s with
  fresh data.
- **Where it shows.** The verdict and its metrics are stored in the assessment's `inputs_snapshot.entry_exit_check`.
  The funnel counts it as its own blocker group.

**Tests:**

- `packages/core-py/tests/test_exit_signal_at_entry.py`: the gate unit tests, plus the assembler replaying the
  production shape through `testing.pump.seed_fading_flow`;
- `services/decision-engine/tests/test_gate_eval.py::test_exit_signal_at_entry_waits_and_opens_nothing`.

## 3. LIVE_EXECUTION_SMOKE_TEST

This is an execution verification tool, not a strategy. It proves this chain with a tiny amount of real SOL:

wallet → buy → transaction confirmation → position → live price → PnL → exit decision → sell → confirmation →
closed.

### Switches (all required; the defaults are off)

1. **Server `.env`** (not editable from the dashboard):
   - `LIVE_SMOKE_TEST_ENABLED=true`;
   - `LIVE_SMOKE_TEST_MAX_SOL=<amount>` (there is **no default**: empty means it cannot be armed);
   - `LIVE_SMOKE_TEST_MAX_TRADES` (default 1; this counts every smoke-test buy ever made).
2. **The three environment locks open** (`TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `PAPER_TRADING=false`), a valid
   wallet, and the live order worker reporting ready.
3. **Arming one run** on the Live page:
   - choose the category (FRESH / MIGRATED / MOMENTUM), max SOL (≤ the server cap) and expiry (5–120 min);
   - enter the **admin password** again (5 wrong attempts lock arming for 15 minutes);
   - type **`SPEND REAL SOL`**.
   Arming, refusals and cancellations are written to the audit log.

The global mode is never changed, and a deploy never arms anything.

### What happens while a run is armed

1. The decision engine evaluates candidates as usual. For the first candidate of the run's category **with a BUY
   signal**, it runs the **full safety gate again** against the **live wallet**:
   - balance minus `min_sol_reserve`;
   - `max_position_size` capped at the run's max SOL;
   - every other risk limit unchanged.
2. **If not approved**, the attempt is recorded with its stage and codes, and nothing is bought. The stages are:
   - `SAFETY_REJECTED`;
   - `RISK_REJECTED` (e.g. a wallet too small for the 0.01 SOL minimum);
   - `MARKET_DATA_UNAVAILABLE`;
   - `EXECUTION_ROUTE_UNAVAILABLE`.
3. **If the run expires first**, it ends as `NO_TEST_EXECUTION_CANDIDATE`, with a count per stage.
4. **If approved**, `live_trading.enter_live` queues the BUY, and the existing order worker executes it. Its steps
   are: PumpPortal trade-local build → transaction guard → local signing → simulation → send → confirmation → fill
   from the wallet's balance change.
5. **Position management is the existing system:** stop, take-profits, trailing stop, exit intelligence.
6. **Test-close** (Live page) sets the position's `exit_requested` flag, the same operator exit every position has.
   The manager then sells through the normal exit path, with expected output, slippage limit and guard.

### What the Live page shows for a run

The status is always derived from the real `execution_orders` and `paper_positions` rows (`live_smoke.run_view`),
never stored as a claim:

- `ORDER_SUBMITTED` means signed and sent;
- `TRANSACTION_CONFIRMED` means the signature is confirmed;
- `ACTUALLY_FILLED` means the wallet balance changed the right way.

**For each order:**

- requested amount and slippage limit;
- signature;
- submitted and confirmed times;
- actual tokens and SOL;
- network fee;
- fill price and slippage against the plan.

**Failure stages:**

| Stage | Cause |
|---|---|
| `QUOTE_FAILED` | Transaction build/quote failed |
| `SIGNING_FAILED` | The request was not for the configured wallet |
| `TRANSACTION_REJECTED` | Refused by the guard, failed simulation, or failed on chain |
| `SUBMISSION_FAILED` | The transaction was not sent |
| `TRANSACTION_UNCONFIRMED` | Not confirmed before the blockhash expired |
| `FILL_UNVERIFIED` | Confirmed, but the wallet did not change as expected |
| `POSITION_CREATION_FAILED` | The position could not be created |
| `SELL_FAILED: …` | A sell failed at one of the stages above |
| `NO_TEST_EXECUTION_CANDIDATE` | No approved candidate before the run expired |

**Status: IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.**

- No real transaction has been sent by this code.
- The provider boundary is covered by tests with scripted outcomes; the full cycle is
  `services/paper-trading/tests/test_live_worker.py::test_smoke_test_cycle_buy_fill_pnl_test_close_sell_closed`.
- A real run needs your explicit authorization.

**With the current wallet** (0.083 SOL, reserve 0.05, so 0.033 available), the 0.01 SOL minimum is reachable. Risk
per trade still applies: if 1–2 % of equity and the stop give a size under 0.01, the run records `RISK_REJECTED`.
It is never resized to force a buy.

## 4. Wallets and live positions

`GET /api/live/wallets` (Live page) shows LIVE and PAPER side by side, never mixed.

**LIVE.** This comes from the chain, via the order worker's wallet sync every reconcile. It shows:

- the shortened public address only;
- SOL balance with its time and a STALE flag;
- reserved SOL (`min_sol_reserve` plus pending buys) and available SOL;
- token holdings.

Each holding is valued from one of these sources (`solana/valuation.py`):

- the bonding curve in the stream, with the time of its last trade;
- the PumpSwap pool reserves over RPC;
- wrapped SOL at 1:1.

USD is shown only when the Jupiter-quoted SOL/USD is cached. A holding with no reliable price shows
`VALUATION UNAVAILABLE` and the reason. These are marginal prices, not executable quotes. The total is marked
incomplete when anything is not valued.

**PAPER.** Starting balance, available balance, open-position value, unrealized and realized PnL, and equity.

**Live positions.** Each row shows:

- entry → current price, with `LIVE` / `STALE` / `UNAVAILABLE` and the mark's age;
- quantity;
- cost → current value;
- unrealized PnL and %: remaining quantity × latest price − the remaining share of the actual SOL spent, from the
  fill (fees included);
- realized PnL;
- stop, take-profits (hit ones ticked) and trailing stop;
- lifecycle/route/provider;
- the entry signature.

The page refreshes every 5 s and on events.

## 5. Observation follow-ups (T+5m … T+60m)

`solana/followups.py` runs in paper-trading's loop, next to the existing rejected-outcome tracker. For every
observed fresh token, traded or not, it stores the following in `token_observations.followups`:

- `T+5m`, `T+10m`, `T+30m` and `T+60m` after launch: a decimals-free price (the same unit as the observation's
  T0 / T+half / T+window checkpoints), liquidity, source, the change against the decision, and a `late` flag;
- `migration` when it happens;
- a `final` summary at T+60m.

Before migration it uses the stream curve; after migration, the PumpSwap pool (at most 5 pool lookups per tick).
An unpriced snapshot records why it could not be priced.

This is observation data only. The ML promotion process is unchanged, and nothing trains on it automatically.

## 6. Security

- Private keys never leave the server. The APIs return the shortened public address, balances, positions, PnL and
  signatures only.
- Signing stays in the order worker.
- The smoke-test switches are server-only. The dashboard can only arm a run the server already allows, and only with
  the password and the typed phrase.
