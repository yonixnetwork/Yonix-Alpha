# YonixAlpha full audit report (2026-10-07)

Companion documents: YONIXALPHA_FULL_SYSTEM_AUDIT.md (component table, dependency map), SCREENSHOT_ISSUES.md
(each screenshot traced), PAPER_VS_LIVE_PARITY_AUDIT.md. Zero-fabrication rule: VERIFIED only where a test or server
output shows it. No live transaction was sent for this audit. Paper remains the default; nothing here enables live
trading.

## A. Current architecture
One FastAPI process behind nginx (60 s timeout), Postgres 16 and Redis, and nine worker containers (data-solana,
engine-solana-discovery, decision-engine, paper-trading, data-evm, copy-engine, ml, plus the two optional Solana
engines) on a 2 vCPU / 2 GB droplet. See the dependency map in YONIXALPHA_FULL_SYSTEM_AUDIT.md.

## B. Screenshot problems
Eleven observations, each traced to its endpoint and data: see SCREENSHOT_ISSUES.md.

## C. 504 root causes
| Page | Classification | Evidence |
|---|---|---|
| Opportunity outcomes (comparison) | SERIALIZATION_DELAY + MEMORY_PRESSURE | VERIFIED locally: 52.6-59.3 s and +1.5 GB for 150 000 rows; after the fix 2.3 s |
| Ledger review | SLOW_QUERY (16 queries; unbounded category count) | VERIFIED: one pass 0.44 s |
| Losing trades list | SLOW_QUERY (count over the whole history) | Code; bounded by the page's period now |
| EVM / wallet ML | SLOW_QUERY (+ missing index) | Code; production time NOT VERIFIED (`db_health` measures it) |
| Wallets overview | QUEUE_BACKLOG (pool held by the above; dashboard re-polled while requests were still running) | Mechanism VERIFIED by tests; production NOT VERIFIED |
Missing ML never caused a 504: these pages read stored samples and models; an untrained model reads as SHADOW /
NOT_AVAILABLE. After this change a slow statement is stopped at 25 s and answered as 503 QUERY_TIMEOUT with a request
ID that System Health lists.

## D. Paper vs live discrepancy
Proven in code: paper did not count or charge the fixed costs a LIVE round trip pays, so it took trades LIVE refused,
sized larger and booked better results. Fixed (setting `charge_live_fixed_costs`, default on). Still open: paper has
no decision-to-fill latency model. Details and the measuring command: PAPER_VS_LIVE_PARITY_AUDIT.md.

## E. Solana execution audit
Unchanged in this audit (DO NOT rebuild). 2026-10-06: 101 confirmed sells; then 6 428 failed sells of one token (7wmm)
with 6053 over 9 hours on the same RPC endpoint; no Pump program upgrade or GlobalConfig change in that window; the
same builder's sell for that pool simulates OK now. Root cause NOT VERIFIED. Since PR #51 the unsigned transaction of a
failed simulation is kept, so a repeat can be inspected; `pumpswap_window_check` (PR #52) checks whether other traders
could sell during the window. The earlier report's 3012 failures were the operator's manual sell (token account
closed), shown by the order rows.

## F. Wallet architecture
Solana: one ed25519 key (`WALLET_PRIVATE_KEY`, server .env). EVM: one secp256k1 account shared by BSC and Robinhood
Chain (`EVM_WALLET_PRIVATE_KEY` optional, or `EVM_WALLET_ADDRESS` watch-only); separate keys, neither derived from the
other. On the server the EVM account is NOT CONFIGURED (screenshot), which is why BSC / Robinhood LIVE show NOT
CONFIGURED.
Seed phrase support was **not implemented** on purpose: storing a 12-word phrase needs a designed encrypted vault (no
database / Redis / log / browser copy), and derivation paths must be checked against the addresses the operator
expects before any use. Proposal for a separate, reviewed change: phrase entered only on the server (like the .env
keys), derive Solana `m/44'/501'/0'/0'` and EVM `m/44'/60'/0'/0/0`, show only the derived public addresses for
confirmation, keep the phrase out of every store. A wallet TEST button that reads balances only (no transaction) is
also proposed for that change.

## G. BSC execution
Paper only. Paper BUY now in the token table; paper SELL in the positions table. LIVE: locked (task #178).

## H. Robinhood Chain execution
Paper only, as BSC. Pons V2 tokens that graduate to Uniswap V4 cannot be priced or exited because Uniswap V4 quoting is
not implemented (MISSING). The page now says so per position and an alert goes out after 15 minutes.

## I. Copy-trading failures
Every copy event already stores decision, reason, latency stages and paper outcome. There was no summary of why copies
were skipped; `parity_report` section 4 prints it per chain and side with median latency. Production numbers NOT
VERIFIED until it is run.

## J. ML training pipeline / K. ML validation
Unchanged in this audit. Contribution 0 % (SHADOW) for every model; governance gates, frozen validation sets and the
decision-time feature cut-off from earlier work stand (tests pass). ML is not required for any page.

## L. Repository update system
Working as designed: GitHub API, REVIEW ONLY for third-party repositories, PIN BUMP for pinned dependencies, nothing
auto-deploys (screenshot 10).

## M. Database performance / N. API performance
Fixed: comparison and review in SQL, date bounds, one index (migration 0041, built CONCURRENTLY), single-flight
cache on the three review endpoints (since the follow-up in section V: computed in the background, 30 minutes fresh),
25 s statement limit, 10 s pool wait, request IDs, slow-request list.

## O. Security findings
No secret is logged or returned by the new code: the slow-request list records query parameter names only (test).
The kept unsigned transaction carries no signature (test).

## P. Missing features (not done here)
Uniswap V4 quoting for graduated Pons tokens; paper latency model; seed-phrase vault; live-sell backoff after repeated
identical program errors (proposed, needs operator agreement); matched paper/live replay harness.

## Q. Deprecated features
None removed in this audit.

## R. Fixes implemented
See the PR description (request timing, statement limit, SQL aggregation, cache, index, dashboard polling, unpriced
EVM positions, EVM BUY button, named health badge, slow-request view, paper fixed costs, db_health, parity_report).

## S. Tests performed
New tests: request timing (5), SQL comparison parity with the old code, db_health end to end, parity_report end to
end, unpriced EVM position (worker + API), paper fixed costs (4 + gate test). Full suites: see the PR.

## T. Tests that could not be performed
Anything on the production database or chain: page timings on real data, the 7wmm window check, live sends (not
authorized), EVM live (locked).

## U. Remaining risks
2 GB droplet memory; ml / copy-engine periodic CPU load; Pons positions without an exit route; live sells retry without
backoff on deterministic errors; paper still has no latency model.

## V. Server findings after deploying 8ad1d33 (2026-10-07 13:05) and the follow-up fixes
Measured on the server (`db_health`, `parity_report --days 7`, `pumpswap_window_check`):
- **Host overloaded.** Load 9.48 on 2 vCPU, 392 MB free of 1 967 MB, 1 172 MB swap in use. The database is far larger
  than memory: evm_trades 3.5 GB (4.4 M rows), risk_assessments 3.1 GB, opportunity_outcomes 2.5 GB, market_snapshots
  1.75 GB, copy_events 922 MB. Under this load the review aggregates still ran past 120 s; the ledger category count
  over all history took 75 s.
- **Copy trading too late.** TOO_LATE: BSC 270 217, Robinhood 6 648; median detection 300 841 ms (BSC) and 216 923 ms
  (Robinhood). Found in code: the copy engine's per-second poll compared `lower(trader)`, which no index serves, so every
  tick on each chain read the whole evm_trades table (matching the 12 s evm_trades statements in pg_stat_activity). The
  14-day prune and the market-regime windows had the same full scans.
- **Paper exit failure rate distorted.** "exit 50 % (measured from 6 553 live SELL orders)": 6 450 of those were retries
  of the one stuck 7wmm sell, each counted as a separate trial, so every paper exit was failed at the 50 % cap.
- **Paper vs live (7 days).** LIVE 16 trades, 6.2 % win, mean -11.87 %. PAPER 96 trades, 43.8 % win, mean +10.03 %,
  carried by 17 take_profit_3 exits at about +103 %. These are the numbers before paper charged LIVE fixed costs.
- **7wmm.** Other traders sold the pool throughout the window with the same accounts as our confirmed sells; our
  simulations failed. Root cause still NOT VERIFIED (single RPC endpoint, or something in our transaction).

Fixed in this follow-up (tests in the PR):
| Finding | Fix | Verification |
|---|---|---|
| Copy poll / prune / regime full scans | Index `ix_evm_trades_chain_at (chain, at)`, migration 0042, built CONCURRENTLY; the poll is `target_trades()` | VERIFIED: the plan uses the index (test fails without it). Production latency: NOT VERIFIED until `parity_report` runs again |
| Retries counted as trials | `measured_live_rates` counts one result per position and side: its first final attempt | VERIFIED: 300 retries of one position count once (test) |
| Review pages past the proxy limit | Computed in a background task on their own two connections (240 s statement limit, one at a time); a request is answered at once from the last result, marked stale while a refresh runs; with no result yet, 503 REVIEW_COMPUTING; the page shows when the numbers were computed | VERIFIED by tests (fresh, stale, computing, failed refresh). Production: NOT VERIFIED (`db_health` section 6 lists each result, its compute time and last error) |
| db_health too slow to finish | `--skip-timings` for a quick run; the copy poll is timed; review results listed | VERIFIED (test) |

Not changed, needs the operator's decision:
- **Server size.** Historical ML / trading data may not be deleted (rule), so the data will keep growing past 2 GB of
  memory. Recommended: resize the droplet (for example 4 vCPU / 8 GB). Until then the review pages show older results.
- **7wmm.** Proposed: re-simulate on a second RPC endpoint before treating a sell as failed, and back off after
  repeated identical program errors. Not implemented: it changes working live execution and the root cause is not yet
  verified.

## W. Server findings after deploying aa8c0b3 (2026-10-07 15:20) and the second follow-up
Measured (`db_health --skip-timings`, `parity_report --days 1`):
- VERIFIED improved: the paper exit failure rate is 4.35 % measured from 46 live sells (was the 50 % cap); review pages
  are answered from background results (ledger review 372 s, comparison 621 s, EVM ML 436 s to compute); median copy
  detection over the last day 119 s on BSC and 78 s on Robinhood (was 301 s / 217 s over 7 days; the day still
  includes hours before the deploy, so the effect of the index alone is NOT VERIFIED).
- Still overloaded: load 12.4, 363 MB free, 1.4 GB swap.
- New hot spots found in pg_stat_activity and the slow-request list:
  - the wallet-label step (`build_missed`) ran one query per copy target / validated wallet, each reading 7 days of
    evm_trades (44 s each, in parallel); the ml EVM / wallet step had been RUNNING for 50 minutes;
  - `/api/chains/solana` took 35-48 s on every refresh: it built every chain's launchpad statuses and then this
    chain's again, and the Solana checks counted all of opportunity_outcomes (2.6 GB) and 24 h of risk_assessments.

Fixed:
| Finding | Fix | Verification |
|---|---|---|
| One 7-day scan per wallet | One scan per chain for all wallets, same results | VERIFIED: existing missed-winner test passes |
| Solana page counts | Newest-row lookups on indexed timestamps (same PASS / FAIL); the page builds only its chain's launchpads, once | VERIFIED: new test for both checks; control-center API tests pass |
| Reviews recomputed continuously | A result stays fresh 30 minutes (was 5); it took 6-10 minutes to compute | Config |

## X. Server findings after deploying b0a5ce6 (2026-10-07 22:00) and the third follow-up
Measured (`db_health --skip-timings`, `parity_report --days 1`):
- Load 9.8 / 7.9 / 7.3 (was 12.4 / 10.7 / 10.6). No API request slower than 2 s was recorded after 16:42 (the Solana
  page last appears at 15:52, before the deploy). Whether the dashboard was open in that time is NOT VERIFIED.
- Paper exit failure rate 4.26 % (47 live sells); paper charged LIVE fixed costs.
- Copy detection median over the day: BSC 111 s, Robinhood 64 s. Still far too slow.
- Still running long: the missed-winner query (108 s, three parallel workers), data-evm's entry pass over evm_tokens
  (49 s), and the copy engine's launchpad restore (15 s).

Found in code and fixed:
| Finding | Fix | Verification |
|---|---|---|
| The copy engine re-read every token of every launchpad (15 s) every 60 s inside its watch loop, so target trades waited behind it | Full read on start and hourly; otherwise only tokens created or traded since the previous refresh (indexed) | VERIFIED: test (new and re-traded tokens registered, untouched ones skipped until the full read) |
| data-evm entry pass: no index on evm_tokens.safety_at | Index `ix_evm_tokens_chain_safety_at`, migration 0043, CONCURRENTLY | alembic upgrade + check |
| Missed-winner query every 30 minutes (108 s) | Once every 6 hours; rows are written once each, none is lost | VERIFIED: test |
| Copy delay cause unknown (feed or engine) | `db_health` prints the age of the newest stored EVM trade and each scan cursor | VERIFIED: test |

