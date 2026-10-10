# Trading regression investigation and strategy recovery

Status (2026-10-10): **PHASE 1, AUDIT IN PROGRESS.** No strategy, threshold,
size, risk limit or exit rule was changed by this investigation. Nothing
below is a profitability claim. Sections marked PENDING need the server
measurements listed in section 9.

## 0. Protection applied first

| Action | How | Effect | Verified |
|---|---|---|---|
| Stop new LIVE auto-entries | Operator: Settings > Global mode > PAPER | The gate's `live_intent` requires global mode LIVE (`decision-engine/app/gate_eval.py`), so no new LIVE buy is created. | PENDING (operator action) |
| Keep protecting open LIVE positions | Nothing to change | The live worker (`paper-trading/app/live_worker.py`) and exit management check only the environment locks (`live_trading_permitted`), not the global mode. Stops, take-profits, trailing stops and sellable-amount checks keep running. | Code-traced |
| Do NOT use the kill switch or the .env locks for this | - | The kill switch cancels queued BUYs only, but closing the .env locks would also stop LIVE exits. | Code-traced |
| Paper and shadow keep running | Nothing to change | Paper trading and the entry-intelligence shadow pass are unaffected by the global mode. | Code-traced |

## 1. Root cause: evidence and confidence

**Not yet established.** The audit has so far produced the change timeline
(section 3) and four hypotheses. Each one says what evidence would confirm
or reject it. The regression report (`tools/regression_report.py`, section
9) produces that evidence from the production database.

| # | Hypothesis | Evidence for (so far) | Would be confirmed by | Would be rejected by |
|---|---|---|---|---|
| H1 | LIVE loses mainly to fixed costs: positions are tiny | Server 2026-10-09: LIVE buys of 0.0014-0.01 SOL. One round trip costs about 0.00022 SOL in network and priority fees with rent reclaim on (`live_trading.fixed_trade_costs`), which is 7-16% of a 0.0014-0.003 SOL trade, before the 1.25% pump.fun fee each side and price drift | Section 3 of the report: median cost drag for LIVE well above the median price move; LIVE losses concentrated in the smallest size band | LIVE price moves themselves negative at entry, with costs small |
| H2 | Late entries: bought after most of the move | Server 2026-10-10 `entry_timing`: 8 of 32 entries were 50%+ above the first detection price; gate wait median 1,001 s for entered tokens; about 5,000 data failures per day from an RPC backoff overflow (fixed in PR #70) | Report MFE small and MAE large for losers; loss share higher in tokens with long detection-to-entry | Losers with large MFE (good entries, bad exits) |
| H3 | PAPER "got worse" because its accounting became honest, not because the strategy changed | PR #53 (merged 2026-10-07) charges paper the LIVE fixed costs and sizes paper as LIVE would; PR #62 (2026-10-09) charges the measured LIVE price drift to paper fills | Report section 3: the share of paper trades charged fixed costs / drift jumps at those markers, and the price move before costs is unchanged | Paper price moves (before costs) also deteriorate at the marker |
| H4 | An exit change turned winners into losers | PR #66 (2026-10-09) defers partial take-profits worth less than 3 sell fees (paper only by default) | Report section 4: `take_profit_*` share falls and `stop_loss` share rises after #66 for PAPER; `exit_protection.deferred` timeline events exist | No exit-protection events (the server showed none on 2026-10-09) and an unchanged exit mix |

Confidence: none of H1-H4 is confirmed. H1 and H2 have direct server
evidence of their preconditions; H3 is certain as an accounting change, but
its share of the observed deterioration is unmeasured; H4 is unlikely given
that no exit-protection events were recorded.

## 2. Before / after performance

PENDING: `regression_report` sections 1-2 (per day, per window x mode x stage).

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

**Keep LIVE auto-entry OFF** (global mode PAPER) until section 1 has a
confirmed cause, and until any strategy that is to trade LIVE has passed the
out-of-sample gates defined in section 6 of the next phase.
