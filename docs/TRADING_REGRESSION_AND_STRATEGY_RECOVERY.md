# Trading regression investigation and strategy recovery

Status (2026-10-10): **Audit done (section 1). Corrections C3, C4 and C5
implemented and tested; LIVE auto-entry stays OFF (section 10).** No entry
threshold of the existing pipeline, size, risk limit, slippage, priority fee
or exit rule was loosened. C3 only refuses trades. Nothing below is a
profitability claim. Sections marked PENDING need the server measurements
listed in section 9.

## 0. Protection applied first

| Action | How | Effect | Verified |
|---|---|---|---|
| Stop new LIVE auto-entries | Operator: Settings > Global mode > PAPER | The gate's `live_intent` requires global mode LIVE (`decision-engine/app/gate_eval.py`), so no new LIVE buy is created. | DONE by the operator 2026-10-10 |
| Keep protecting open LIVE positions | Nothing to change | The live worker (`paper-trading/app/live_worker.py`) and exit management check only the environment locks (`live_trading_permitted`), not the global mode. Stops, take-profits, trailing stops and sellable-amount checks keep running. | Code-traced |
| Do NOT use the kill switch or the .env locks for this | - | The kill switch cancels queued BUYs only, but closing the .env locks would also stop LIVE exits. | Code-traced |
| Paper and shadow keep running | Nothing to change | Paper trading and the entry-intelligence shadow pass are unaffected by the global mode. | Code-traced |

## 1. Root cause: evidence and confidence

Evidence: `regression_report --days 21` and three read-only queries run on
the production server on 2026-10-10 (211 closed Solana trades since
2026-09-19; the outputs are in the session record). Findings, in order of
effect:

**R1. LIVE has never been profitable in the 21-day window. This is not a
regression. Confidence: high.** Every day since 2026-09-28 is negative:
72 closed LIVE trades, win rate 11% (8 of 72), profit factor below 0.1,
net -0.048 SOL. Every stage loses (FRESH, NEAR_MIGRATION, MIGRATED, MOMENTUM).
The median LIVE size is 0.0036-0.0073 SOL, and the smallest band (<0.005
SOL, 46 trades) loses the most. Contributing causes, from the same data:
fixed network and priority fees of about 0.00022 SOL per round trip plus
the 1.25% curve fee each side (median fees 5-11% of size); entries
4-1000+ s after the signal (`entry_timing`, 2026-10-10); and a paper / LIVE
gap (R3).

**R2. The recent PAPER losses are the momentum strategy, not a code
change. Confidence: high for the location; medium for "why now".**

- Over 21 days, PAPER `solana_momentum` lost: 27 trades, profit factor
  0.60, -0.22 SOL. LIVE `solana_momentum` lost too: 33 trades, profit
  factor 0.05.
- PAPER `solana_migration` was profitable: 84 trades, profit factor 2.41,
  +0.10 SOL. So was PAPER `solana_fresh`: 28 trades, profit factor 1.63,
  +0.03 SOL.
- On 2026-10-10, 17 paper momentum trades lost -0.37 SOL. Most of that
  came in three hours (09:00, 12:00 and 14:00 UTC), on paper positions of
  about 0.2 SOL each. That is about 20 times the size of fresh and
  migrated paper positions (risk-based sizing on tighter momentum stops).
- The gate did not get looser. About 0.5% of momentum evaluations were
  approved on 2026-10-08 and on 2026-10-10 alike. The NUMBER of momentum
  evaluations rose about tenfold from 2026-10-09 13:00 UTC (about 80/h to
  500-1000/h), so the same rule produced more entries.

The rise in volume starts before #66, #69 and #70. It was not caused by
the event re-evaluation of #69: event-triggered momentum entries did
better (8 trades, 4 wins, -0.064 SOL) than timer-triggered ones (9 trades,
1 win, -0.305 SOL).

**R3. PAPER is still more optimistic than LIVE. Confidence: medium.**

- PAPER fresh trades won 64% of the time (28 trades); LIVE fresh trades
  won 18% (22 trades).
- No PAPER trade in the window was charged the measured LIVE price drift
  (#62): the share is 0% in every window. Either fewer than 20 LIVE buys
  carry a measurable drift, or their median was not adverse. Which one is
  PENDING (`paper_execution` effective rates).
- PAPER is charged the LIVE fixed costs since #53 (77-100% of PAPER trades
  after it), but at a 0.01-0.2 SOL size those costs are 2-7% of the
  position, against 5-11% for LIVE.

**Ruled out:**

- #53 and #62 (paper accounting): PAPER stayed positive after them.
- #66 (exit protection): no exit-protection events were recorded.
- #69 (event re-evaluation): its entries did better, as above.
- #70 (RPC fix): the volume rise came first.
- The 33 "open" LIVE positions are failed buys with 0 SOL in them, so no
  exposure is stuck.
- LIVE sell failures in the last 2 days are 278 retries of one position
  (program error 6053, the known pool-specific failure recorded in
  `YONIXALPHA_FULL_AUDIT_REPORT.md`), plus 23 retries of another (6004),
  2 positions with 3012, and 6 BlockhashNotFound.

**Measurement correction:** the first regression report compared LIVE
marks with the LIVE cost basis, which includes fees and new-account rent
(30-110% of a tiny buy), so LIVE MFE / MAE looked negative. The tool now
compares marks with the market price of the fill (`plan.fill.market_price`).

## 2. Before / after performance

| Group | Trades | Win rate | Net SOL | Profit factor |
|---|---|---|---|---|
| PAPER migration, 21 d | 84 | 39% | +0.103 | 2.41 |
| PAPER fresh, 21 d | 28 | 64% | +0.034 | 1.63 |
| PAPER momentum, 21 d | 27 | 37% | -0.220 | 0.60 |
| PAPER momentum, 2026-10-10 only | 17 | 29% | -0.369 | 0.13 |
| LIVE all, 21 d | 72 | 11% | -0.048 | below 0.1 |

All groups are from closed trades and realized PnL. Groups under 20
trades are anecdotal.

### Proposed corrections (smallest first)

| # | Correction | Kind | Status |
|---|---|---|---|
| C1 | Global mode PAPER: no new LIVE buys; exits keep running | Operator | DONE (operator, 2026-10-10) |
| C2 | Strategy `solana_momentum` to PAPER (or OFF), so it cannot trade LIVE when LIVE returns | Operator setting, no code | RECOMMENDED |
| C3 | Refuse a LIVE entry whose fixed round-trip costs exceed `max_fixed_cost_pct` (default 2%, ceiling 10%) of its size: NO_TRADE `FIXED_COSTS_TOO_HIGH`, never a size increase | Code (`safety/planning.py`, `safety/settings.py`) | DONE, tested |
| C4 | Paper buys on the pump curve land after the measured LIVE delay and are filled at the stream price of that moment (section 5.3) | Code (`paper_execution.py`, `gate_eval.py`, `gate_manage.py`) | DONE, tested |
| C5 | Strategy registry, category strategies and one-per-token routing, all in SHADOW; promotion manual, demotion automatic (section 5) | Code (`strategy_registry.py`, `entry_intel.py`, `entry_shadow.py`) | DONE, tested; SHADOW |

## 3. Changes identified in Git (trading, sizing, paper accounting)

Merge times are UTC. A merge is not a deploy: the operator deployed later.

| PR | Merged | Change | Affects |
|---|---|---|---|
| #53 | 2026-10-07 12:04 | Paper charged the LIVE round-trip fixed costs; the plan counts those costs for PAPER as for LIVE (stop and size) | PAPER results and which PAPER trades are taken (`STOP_INSIDE_COSTS`) |
| #54 | 2026-10-07 13:57 | Paper exit-failure rate no longer inflated by retries | PAPER exits |
| #62 | 2026-10-09 10:00 | Paper fills charged the measured median LIVE price drift (buy and sell) | PAPER results |
| #63 | 2026-10-09 11:42 | Fresh-token stream guard; alert on refused live buys | Detection |
| #66 | 2026-10-09 20:52 | Sellable-amount exit protection (PAPER by default): tiny partial take-profits deferred | PAPER partial exits |
| #69 | 2026-10-10 08:51 | Fast promotion pass, gate wake list, event re-evaluation (no rule change) | Decision timing and RPC load |
| #70 | 2026-10-10 10:04 | RPC 429 backoff overflow fixed | Data availability |

No PR in this window changed an entry threshold, a stop-loss / take-profit
default, a position-size rule or an ML threshold. That was checked by
diffing `safety/`, `strategies/`, `paper_engine.py`, `paper_execution.py`,
`exit_intel.py` and the decision and paper services. Production
configuration (risk settings in the database) is not in Git. Whether it
changed is PENDING (`config` history / audit log).

## 4. Research references

Six repositories and the documentation were reviewed in
`EARLY_ENTRY_RESEARCH.md` (2026-10-10). Pump public docs and IDL
verification against the current program: PENDING. Outbound access to
solana.com and the pump docs was blocked from the build container on
2026-10-10, so these could not be checked from there.

## 5. Strategy definitions

All new strategies start in SHADOW: they record signals, and the same
labeller measures each signal's executable return (after fees, impact, the
measured LIVE latency and fixed costs). None of them can create a LIVE order
in this release; in PAPER mode a routed signal becomes a PAPER-only gate
candidate and every safety, risk and sellability check still applies. The
existing pipeline stays the champion (`CURRENT_GATE_ENTRY`).

### 5.1 Registry (`yonixalpha_core/strategy_registry.py`)

| Code | Recorded as | Category | Signal (what makes it different) |
|---|---|---|---|
| F1 | EARLY_ACCELERATION | FRESH | net SOL inflow per 10 s accelerating, new buyers, no single-wallet push |
| F2 | EARLY_DEMAND_CONFIRMATION | FRESH | buyer breadth broadening and 30 s net buy pressure; ignores price and inflow acceleration |
| F3 | SELECTIVE_EARLY_BREAKOUT | FRESH | new high above the range before the last 10 s, after a real (>= 5%) pullback, backed by new buyers |
| M1 | MIGRATED_DELAYED_CONFIRMATION | MIGRATION | >= 120 s after migration, price held, pool SOL above the start |
| M2 | MIGRATED_CONTINUATION | MIGRATION | new pool high with rising pool SOL |
| M3 | MIGRATED_PULLBACK | MIGRATION | >= 10% pullback, +3% off the low, rising pool SOL |
| P1 | MOMENTUM_CONTINUATION | MOMENTUM | healthy continuation phase, 60 s inflow, buyers outgrowing sellers |
| P2 | BREAKOUT_RETEST | MOMENTUM | 5-20% below the high, a bounce off the retest low, inflow rising again; not a chase |
| P3 | MOMENTUM_RECOVERY | MOMENTUM | 25-70% below the high, sell volume falling below buys, new buyers, a confirmed turn; NO_TRADE while still falling with sellers in control |
| SW | SMART_WALLET_CONFIRMATION | any | corroboration only: evaluated on the routed candidate, never selects or trades alone |

Each record has a version (`F2.v1/early-2026.10.2`: strategy version and
feature version), stored with every signal (`evidence.strategy_version`).
M1-M3 are the existing migrated variants, measured from PumpSwap pool
reserves; they stay SHADOW-only. The tests show the strategies fire on
different patterns (F2 where F1 waits on a decelerating inflow; F3 at an age
where F1 and F2 refuse; P2 and P3 on a shallow and a deep pullback).

### 5.2 Routing (`entry_intel.route`, `entry_shadow.shadow_pass`)

1. Safety and data checks are part of every strategy (creator sold,
   scripted buys, dump, stale stream, round-trip cost): a failure is NO_TRADE.
2. Category: FRESH up to 600 s of age, MOMENTUM after, MIGRATION for
   migrated tokens (which only the pool variants measure).
3. Every strategy is evaluated independently; only the category's
   strategies can be routed.
4. Among the qualifying ones, the highest score is selected; every other
   strategy's decision and first reason is recorded with it.
5. None qualifies: NO_TRADE.
6. The paper route is the same router over the strategies in PAPER mode
   only. A token gets at most one routed paper candidate, ever (Redis claim
   `yx:ee:route:{mint}`, 3 days, kept even when no candidate could be
   created). So a loss never leads to another strategy re-entering the
   token, and the gate's own one-open-candidate rule is a second guard.
7. Blocking reasons are counted per strategy and day (`yx:ee:why:`, numbers
   normalized, 3 days), routing outcomes per day (`yx:ee:routes:`).

### 5.3 Paper entry delay (C4)

The server data showed no paper trade charged the measured LIVE drift. A
paper buy on the pump curve is now scheduled at the decision and filled when
the measured LIVE delay (median decision-to-confirm of the newest confirmed
LIVE buys, or the stated default) has passed, at the stream price of that
moment: quantities and prices are scaled by the price move between the
decision and the landing time. Without stream trades in that interval the
decision fill is kept and marked unmeasured. Migrated (PumpSwap) paper trades
keep the measured drift charge. Setting: Paper > execution, "Paper buys on
the pump curve land after the measured LIVE delay" (on by default).

### 5.4 Promotion and demotion

- Promotion: manual only, by the operator, audited. The readiness states of
  `entry_eval` (frozen test period, champion comparison, forward period,
  paper results) are shown; nothing is promoted automatically.
- Demotion: automatic, PAPER to SHADOW only, every 10 minutes: at least 30
  labelled executable returns among the strategy's newest 60 signals AND the
  upper bound of the 95% confidence interval of their mean below 0. One or
  two losses cannot trigger it; a noisy record with a slightly negative mean
  does not either. Each demotion writes `entry_intel.strategy_demoted` to the
  audit log with its evidence.

### 5.5 Dashboard

Entry Intelligence > "Strategy portfolio and performance, last 14 days":
code, title, category, version, mode, readiness, signals, labelled, win
rate, expectancy, profit factor, bad-entry rate, the demotion check, the top
blocking reasons and the baselines (`CURRENT_GATE_ENTRY`, `NAIVE_SAMPLE`,
`MIGRATED_NO_TRADE`). API: `GET /api/entry-intel/strategies`.

## 6. Test methodology and results

- `tools/regression_report.py` and `tests/test_regression_report.py`: a
  deterministic split by marker, mode and stage, cost-charge shares and the
  exit mix.
- RPC backoff overflow: `tests/test_rpc_rate_limits.py::test_a_long_429_streak_still_cools_down_instead_of_overflowing`
  reproduces the server error before the fix (PR #70).
- C3: `tests/test_safety_gate.py::test_a_trade_too_small_to_pay_its_fixed_costs_is_refused_never_enlarged`
  (refused at 3% fixed costs, accepted at a 10% limit with the size not
  increased, PAPER not refused), `test_max_fixed_cost_pct_is_bounded`, and
  `test_fixed_costs_shrink_a_risk_bound_live_size_on_a_small_wallet` (the
  small-wallet LIVE trade is now refused by default).
- C4: `tests/test_paper_entry_delay.py` (scaling, unmeasured fallback, settle
  once), `paper-trading/tests/test_gate_manage.py::test_paper_buy_is_held_until_it_lands_then_filled_at_the_landing_price`,
  `decision-engine/tests/test_entry_gate.py` (scheduled after the measured
  latency; switching it off keeps the immediate fill).
- C5: `tests/test_strategy_registry.py` (distinct signals per strategy,
  category routing, highest score wins, NO_TRADE when none qualifies, smart
  wallets never select, registry coverage, demotion never on one or two
  losses, only reliably losing PAPER strategies demoted, only the newest
  window counts); `engine-solana-discovery/tests/test_entry_shadow.py` (two
  PAPER strategies on one token open one candidate, the token is never
  routed again after its trade closed, SHADOW never trades, reason counters,
  demotion with audit); `apps/api/tests/test_entry_intel_api.py` (the
  strategies view).
- The ML entry-timing training reads at most the newest 50 000 labelled
  signals (it read the whole table before), since six strategies now record.

## 7. Net performance after realistic costs

PENDING (no claim).

## 8. Remaining risks and limitations

- The new strategies have no measured record yet. Their thresholds are
  first choices, set before any of their signals was labelled; they must not
  be tuned on the frozen test period.
- The operator lowered `min_position_size_quote` in the database risk
  settings. C3 now refuses LIVE trades that are too small for their fixed
  costs whatever that minimum is.
- `solana_momentum` (the existing momentum engine) is unchanged; C2 (set it
  to PAPER or OFF) is the operator's decision.

- Merge times are not deploy times. Windows in the report are approximate
  until the deploy times are known.
- Paper and LIVE never trade the same signal. Their comparison is per
  matched opportunity (`entry_parity`) and partly estimated.
- Small LIVE samples: groups under 20 trades are marked "(small)".

## 9. Commands (server)

```
cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T api python -m yonixalpha_core.tools.regression_report --days 21
```

Rollback:

- C3: Risk settings > `max_fixed_cost_pct` up to 0.10 (the ceiling), or
  revert the commit. It only refuses LIVE trades.
- C4: Paper > execution > switch "Paper buys ... land after the measured
  LIVE delay" off; paper fills are then immediate again.
- C5: every new strategy is SHADOW by default; to stop recording one, set it
  to PAUSED on the Entry Intelligence page. Reverting the commit removes the
  strategies; their recorded signals stay in `entry_signals`.
- Nothing deletes data. The global mode stays as the operator set it.

## 10. Recommendation on LIVE auto-entry

**Keep LIVE auto-entry OFF** (global mode PAPER). LIVE has not been
profitable in any of the 21 days measured (R1). It may be reconsidered only
after:

- C3 is in place (done); note that with the measured fixed costs of about
  0.00022 SOL per round trip, a LIVE trade needs about 0.011 SOL or more to
  pass the 2% rule;
- a strategy shows a positive net expectancy after realistic costs on an
  out-of-sample period, with at least 30 trades;
- paper / LIVE parity (C4) is measured on the new paper fills;
- the operator approves explicitly.
