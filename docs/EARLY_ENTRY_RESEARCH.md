# Early entry: repository and documentation research (2026-10-10)

The six repositories named in the task were cloned read-only (shallow,
no tags) and their source code was read: README, layout, dependencies,
licenses, tests and commit history. Repositories 4 and 6 in the task are
the same URL (`justFiveDev/Solana-Copy-Trading-Bot`), so it was reviewed
once. Nothing was copied into YONIXALPHA, and nothing was installed or run
from them. A README claim, such as a win rate, is reported here as a
claim. It is not evidence.

## 1. Evidence table (algorithms, not claims)

| Repository | What is really implemented (read in code) | Claims not verified | Reproducible idea | License | Deps / 2 GB fit | Security / execution risk | Decision |
|---|---|---|---|---|---|---|---|
| demiurge-substrate/pumpsniper-main (JS, 182 commits, last 2026-04-08) | `core/curve.js`: vSol velocity (SOL/s) over three windows, acceleration = v1 - v2, jerk, labels ACCELERATING / STEADY / DECELERATING; vSol per trade ("efficiency"); creator index; a paper simulator (`core/paper_sim.js`) with 14 friction layers | "94% win rate on paper (101 trades, 15 h)". The paper simulator draws slippage, fee wars and rug delay from `Math.random` (22 calls), so its results are not reproducible and not measured fills. A graduation-predictor claim cites a paper (arXiv 2602.14860) that was not checked here | Inflow velocity and its acceleration over consecutive windows; SOL per trade as a conviction measure; "decelerating = skip" | No LICENSE file and no license field: all rights reserved by default. Ideas only, no code reuse | Node, small; fits, but it is a whole bot | Holds a private key in `.env` and trades from it; Telegram channels | **Idea adopted, re-derived**: `inflow_sol_per_s`, `inflow_acceleration_sol_per_s2`, `sol_per_buy`, DECELERATING/EXHAUSTED phases in `entry_intel.py`. Win rate rejected as evidence |
| dunkin-dee/meme-sniper (Python, 9 commits, last 2026-08-06, 71 tests per its STATUS) | A recorder for the PumpPortal new-token and migration streams into SQLite; verified curve math (`curve.py`) pinned by tests on captured frames; Token-2022 decode; metadata enrichment | Graduation-rate figures after pump.fun "BOOST" (2026-07-21) and social-channel lift ratios are quoted from its own collection or other sources, not re-measured here | Record first, measure the regime, set a kill criterion in advance (median negative net of a realistic round-trip cost -> stop); "fewer trades to reach a vSOL level" as a feature; instant-bond bundles excluded as untradeable; cohort-based rates only | No LICENSE file: ideas only | Python + SQLite, small; fits | Paper only, no hot wallet | **Method adopted**: shadow recording first, the frozen test period, the executable-return label net of costs, and "promote only after out-of-sample improvement" (`entry_eval.py`). Instant bonds were already handled (`launch_features`) |
| m8s-lab/solana-sniping-bot (TypeScript, 19 commits, last 2025-06-12) | Yellowstone gRPC subscription to create/migrate transactions on pump.fun, PumpSwap, Raydium LaunchLab and Meteora DBC; metadata and X (third-party "xapi") check; buy and save to MongoDB; a cron that checks pool balances and sells | No performance claim. Filters are simple (metadata and socials) | Event-driven decision on the create transaction; pool-balance watch for exits | MIT (package.json) | Node, MongoDB, gRPC; a second database and stack would be needed; not a fit | `slippage = 10000` (100%) in the swap calls: any price is accepted. Pinned old `@solana/web3.js` 1.68.2 | **Rejected** as code. Unbounded slippage is the opposite of the existing guard. The event-driven idea was already present (logsSubscribe) |
| justFiveDev/Solana-Copy-Trading-Bot (Rust, 18 commits, last 2026-05-20) | `transactionSubscribe` (Helius-style) on one target wallet at `processed` commitment; parses Raydium and pump.fun swaps; mirrors them; optional Jito bundle with tip | Speed claims ("high performance") without measurements | Watching specific wallets' transactions | No LICENSE file | Rust, pinned solana-sdk 1.16; separate binary; not a fit | Mirrors whatever the target does with no safety gate; `processed` commitment can act on transactions that are later dropped | **Rejected**: copy trading stays disabled in this release (task rule). A wallet entry is used only as confirming evidence (strategy B) |
| martivic/pumpfun-2026 (Python, 4 commits, last 2026-01-19; a copy of chainstacklabs/pump-fun-bot) | Listeners for logs, blocks, Geyser and PumpPortal; universal trader; priority-fee helpers; many learning scripts (decode curve, compute bonding-curve PDA, manual buy/sell, compute-unit-optimized buy) | README says "NOT FOR PRODUCTION, learning purposes only" | Listener comparison (logs vs blockSubscribe vs Geyser) and compute-unit limits on buys | Apache-2.0 (LICENSE) | Python; fits, but duplicates what exists | Learning code; uses a local keypair | **Reference only**: already covered by `pump_stream` (logs) and the PumpPortal local-trade path. Nothing adopted |
| yuno-research/solana_copy_trading (Rust + Python, 117 commits, last 2026-01-02) | Python backtesting: first-buy-first-sell (FBFS) positions per wallet, wallet scoring by many metrics (win rate, hold times, buy size, liquidity at buy, diversity) with min-max scaling, and a genetic algorithm that picks the weights that maximize backtested PnL of the top 25 wallets; Rust live engine with Jito bundles | No reported out-of-sample result in the README. Weights tuned to maximize backtest PnL on the same history are prone to overfitting | Per-wallet realized results from FIFO-matched buys and sells; hold-time statistics; requiring a minimum number of closed positions | No LICENSE file | polars, numpy, Rust; the GA is heavy; not a fit on 2 GB | Live copy engine | **Idea adopted, safer form**: `entry_store._wallet_stats` computes FIFO realized results per wallet with fees, profit factor, drawdown and outlier dependence over 24 h / 7 d / 30 d, only from launches resolved **before** the decision time, and requires 5 closed launches. The GA weight search was rejected (overfitting; CPU) |

## 2. Documentation

Outbound access to solana.com, the Pump.fun and PumpSwap docs, Helius and
Jito was blocked from the build container this session (HTTP connection
failed). The checks below therefore rest on what the code already does and
was verified against earlier (see `PUMPFUN_EXECUTION_RESEARCH.md` section 1
for what was reachable then). They do **not** rest on a fresh reading of
those pages.

| Topic | What YONIXALPHA relies on | Status |
|---|---|---|
| Solana RPC / WebSocket | `logsSubscribe` on the pump program at `confirmed`; `getMultipleAccounts` for batched reads | In production since earlier releases. The migrated sampler added here uses one `getMultipleAccounts` per pass at background priority |
| Commitment | `confirmed` for discovery, confirmation polling for orders | Unchanged. `processed` (used by the copy bot above) was not adopted |
| Priority fees | Per-trade compute-unit price with a hard cap (`max_priority_fee_sol`) enforced by the transaction guard | Unchanged |
| Provider selection / production readiness | Several RPC providers in the dashboard with health, failover and per-method capability (`rpc_manager`) | Unchanged |
| Pump.fun bonding curve | Constant product on virtual reserves; fees read from the global account; graduation to PumpSwap | Used by `entry_outcomes.label_fresh` to compute executable returns (`opportunity_analysis.round_trip`) |
| PumpSwap | Pool vault balances; pool fee from recent trades (`pumpswap.recent_pool_trades`) | The latest observed fee per pool is now cached (`yx:pumpswap:fee:`) for the migrated labeller. Without a known fee, the return is marked theoretical |
| Helius | Used as an RPC provider. `getProgramAccountsV2` was rejected earlier for creator history (cost) | Unchanged |
| Jito | Not used. Bundles would add a tip per trade, which is significant at the current trade size (0.0014-0.01 SOL), and a new execution path | **Not adopted** in this release. Revisit only if measured landing latency, not decision latency, turns out to be the bottleneck |

## 3. Algorithms selected and rejected

Selected (re-derived, written from scratch in `entry_intel.py`):

- Windowed inflow (10 s / 30 s / 60 s, with the previous 10 s and 60 s for
  comparison), inflow acceleration, trade-rate acceleration, new buyers and
  sellers per window, net buy pressure.
- Organic-demand checks: meaningful independent buyers (minimum size,
  excluding the creator), tiny-trade share, top-buyer share, synchronized
  buyer clusters, a large buy followed by a dump, creator sells.
- Phases: EARLY_ACCEL, HEALTHY_CONTINUATION, EXHAUSTED, DISTRIBUTION,
  UNCLEAR, from displacement since launch, drawdown from the peak, the
  largest pullback so far and fading versus the 60 s average.
- Round-trip cost at a reference size from the curve, so a signal whose
  costs eat the move is NO_TRADE.
- Smart-wallet evidence from FIFO-realized results resolved before the
  decision. It confirms another strategy's candidate and never triggers
  alone.
- Evaluation: chronological train / validation / frozen test periods, the
  existing pipeline as champion, executable return after costs and measured
  latency as the label, and a small logistic model with exported
  coefficients (pure-Python inference).

Rejected:

- Random-draw paper friction (pumpsniper). Paper must use measured
  latency, drift and failure rates (already the case in `paper_execution`).
- Unbounded slippage (m8s-lab).
- Mirroring a wallet's trade as an entry (both copy bots). Copy trading
  stays disabled.
- Genetic weight search on backtest PnL (yuno). Overfits the period it is
  tuned on and is CPU-heavy.
- `processed` commitment for decisions.
- Jito bundles at the current size (see above).
- Any README win rate as evidence.
