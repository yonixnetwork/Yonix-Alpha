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
Fixed: comparison and review in SQL, date bounds, one index (migration 0041, built CONCURRENTLY), 60 s single-flight
cache on the three review endpoints, 25 s statement limit, 10 s pool wait, request IDs, slow-request list.

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
