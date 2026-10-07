# Paper vs live parity audit (2026-10-07)

Goal: LIVE is the ground truth of execution; PAPER must be a realistic simulation of what LIVE would have experienced.
Paper is not tuned to look like live and live is not tuned to look like paper. Every difference below was found by
reading the code path for the same Solana signal in both modes. "VERIFIED" = shown by a test; numbers from production
are NOT VERIFIED until `parity_report` (below) has been run on the server.

## Where the two paths split

Both modes share discovery, the safety gate (`safety/gate.py`), the risk plan (`safety/planning.py`), exit rules
(`paper_engine.manage_step`) and the opportunity ledger. They split after the gate:

- PAPER: `paper_engine.open_position` fills against the liquidity model the gate assessed (curve / pool constant product,
  pool fee, Token-2022 transfer fee, price impact) and `apply_step` fills exits against the current model.
- LIVE: `live_trading.enter_live` queues an order; `solana/live_exec.py` builds, guards, signs, simulates and sends;
  the fill is parsed from the confirmed transaction.

## Differences found

| # | Difference | Effect on results | Status |
|---|---|---|---|
| 1 | **Fixed per-trade costs** (buy and sell network + priority fee, rent reclaim fee or unreclaimed rent; `live_trading.fixed_trade_costs`) were counted only when the target was LIVE (`safety/pipeline.py`, `gate.py` `_live_target`) | Paper sized larger (no fixed costs in the risk budget), never got `FIXED_COSTS_EXCEED_RISK` / the smaller-size refusals that LIVE gets, and booked results without these fees. On 0.01-0.05 SOL positions the fees are a large share of the trade: paper took trades LIVE refused and reported better results | **FIXED.** New paper execution setting `charge_live_fixed_costs` (default on, dashboard Paper page): the pipeline passes the same fixed costs for Solana paper targets, the plan sizes and refuses exactly as LIVE (test `test_paper_counts_the_same_fixed_costs_as_live_when_set`), and the paper book is charged the buy fee at entry, the sell fee on every exit and the reclaim fee on close (`tests/test_paper_fixed_costs.py`). VERIFIED by tests |
| 2 | **Decision-to-fill latency**: paper fills at the decision-time curve state; LIVE confirms a median of about 3 s later (2026-10 trade reports) on a fast-moving curve | Paper entries are systematically earlier / cheaper on rising tokens, and paper exits fire at the exact stop price | NOT FIXED. Needs a measured latency model (price the fill at the curve state observed `median decision_to_confirm_ms` later). Measured inputs exist (`execution_analysis`, `trade_report`); left for a follow-up because it changes every paper fill and needs its own validation |
| 3 | **Execution failures**: LIVE orders fail (dropped, expired, slippage, program errors such as 6053 on 2026-10-06); paper always filled | Paper exits never fail | Partly modelled before this audit: `paper_execution` applies entry / exit failure rates, measured from LIVE orders once 20 have a final outcome. VERIFIED (existing tests). A deterministic program failure (all sells of one pool failing for hours) is not modelled |
| 4 | **Fee reserve**: LIVE sizes against wallet minus `min_sol_reserve`; paper sizes against its own book | Different available balance; affects size only near the balance limit | Documented, not changed: the paper book is a separate account by design |
| 5 | **Readiness**: a LIVE target needs wallet, executor heartbeat and RPC ready (`live_ready`); paper does not | LIVE refuses entries while not ready; paper keeps trading | By design (paper must not stop when the wallet is offline). The decision record shows `LIVE_NOT_READY` |
| 6 | **Venue at exit**: LIVE sells on the venue resolved at sell time (curve or PumpSwap); paper prices exits from the same sources (`gate_manage._note_migration`) | Same venue; LIVE can still fail on the program side (difference 3) | Equal by design |
| 7 | **EVM (BSC, Robinhood)**: paper only; EVM LIVE is locked (task #178) | No live comparison possible | NOT APPLICABLE until EVM live is unlocked |

## How to measure it on the server (read-only)

```
cd /opt/yonixalpha && C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml" && $C exec api python -m yonixalpha_core.tools.parity_report --days 7
```

It prints closed Solana positions by mode (count, win rate, mean / median result, fees per trade, hold time, exit
reasons), LIVE order outcomes, the paper failure rates in use, executable vs refused decisions by target with the
hard-block codes, and the copy-trading dispositions. Compare the week before and the week after this change: after
it, paper trades should be fewer and smaller where fixed costs bind, and paper fees per trade should match LIVE.

## Not done (and why)

- Matched same-signal pairs (one signal through both paper and live): the system routes a signal to one target, so no
  real pairs exist. A deterministic replay harness (same signals through both fill paths) is the next step; it needs
  the latency model of difference 2 first.
- No live transaction was sent for this audit.
