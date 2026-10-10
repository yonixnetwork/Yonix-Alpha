# Trading regression investigation and strategy recovery

Status (2026-10-10): **PHASE 1 AUDIT DONE: causes located (section 1); corrections proposed.** No strategy, threshold,
size, risk limit or exit rule was changed by this investigation. Nothing
below is a profitability claim. Sections marked PENDING need the server
measurements listed in section 9.

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
| C3 | Refuse a LIVE entry whose fixed round-trip costs exceed a set share of its size (NO_TRADE, a tightening, never a size increase) | Code, tested, shadow-reported first | PROPOSED |
| C4 | Paper / LIVE parity: find why measured LIVE drift is not charged to paper; if the sample is too small, use the stream price after the measured LIVE latency (measured, not invented) | Code | PROPOSED |
| C5 | Strategy registry and the category strategies of the recovery plan, in SHADOW against the existing pipeline, before any of them may trade | Code | NEXT PHASE |

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

PENDING. No new strategy will be routed to execution before section 1 is
answered. Existing shadow strategies (EARLY_ACCELERATION,
MOMENTUM_CONTINUATION, SMART_WALLET_CONFIRMATION, migrated variants) only
record signals.

## 6. Test methodology and results

- `tools/regression_report.py` and `tests/test_regression_report.py`: a
  deterministic split by marker, mode and stage, cost-charge shares and the
  exit mix.
- RPC backoff overflow: `tests/test_rpc_rate_limits.py::test_a_long_429_streak_still_cools_down_instead_of_overflowing`
  reproduces the server error before the fix (PR #70).

## 7. Net performance after realistic costs

PENDING (no claim).

## 8. Remaining risks and limitations

- Merge times are not deploy times. Windows in the report are approximate
  until the deploy times are known.
- Paper and LIVE never trade the same signal. Their comparison is per
  matched opportunity (`entry_parity`) and partly estimated.
- Small LIVE samples: groups under 20 trades are marked "(small)".

## 9. Commands (server)

```
cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T api python -m yonixalpha_core.tools.regression_report --days 21
```

Rollback of this phase: nothing to roll back. The tool is read-only, and
the global mode can be switched back by the operator.

## 10. Recommendation on LIVE auto-entry

**Keep LIVE auto-entry OFF** (global mode PAPER). LIVE has not been
profitable in any of the 21 days measured (R1). It may be reconsidered only
after:

- C3 is in place;
- a strategy shows a positive net expectancy after realistic costs on an
  out-of-sample period, with at least 30 trades;
- paper / LIVE parity (C4) is understood;
- the operator approves explicitly.
