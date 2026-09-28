# YonixAlpha intelligence audit and 2026 research (2026-09-28)

This document comes before any code change in the intelligence upgrade.

It maps what exists and what must not be broken. It classifies what the
2026 research actually supports, and then lists what is implemented.

Classification labels used below:

| Label | Meaning |
|---|---|
| **VERIFIED FACT** | Checked against code, chain data or official documentation in this session. |
| **SOURCE CLAIM** | Stated by the source; not independently reproduced here. |
| **EXPERIMENTAL FINDING** | A measurement in a published study or repository, with its sample; not reproduced on YonixAlpha data. |
| **ANECDOTAL** | Social, marketing or news statement. |
| **INFERENCE** | Our own reasoning. |

## 1. Architecture map (as built)

| Concern | Module(s) |
|---|---|
| Discovery (all Pump events) | `solana/pump_stream.py`, fed by the PumpPortal WebSocket and program logs. It holds create / trade / complete / migration in Redis: trades 3 h, at most 400 per mint. |
| Market data | `solana/assembler.py`, which gathers curve, mint, holders, pool trades, SOL/USD, and Jupiter quotes for non-Pump tokens. Sources: `solana/market_data.py`, `sol_price.py`, `valuation.py`. |
| Token state / lifecycle | `solana/venue.py`, which resolves from chain: bonding curve, PumpSwap, migrating, Jupiter. Also `state_machine.py` (candidate states). |
| Fresh engine | `engine-solana-discovery/app/funnel.py::observe_fresh`, using the observation report `solana/observation.py`, then `decision-engine/app/gate_eval.py` (engine `solana_fresh`). |
| Migrated engine | `gate_eval.py` with lifecycle MIGRATED, via `assemble_migrated` (pool trades, pool state, liquidity report). The separate `services/engine-solana-migration` is legacy scaffolding that does not run in production (rpc_check: NOT_DEPLOYED). |
| Momentum engine | `funnel.py::momentum_prefilter` (active tokens older than 30 min or near migration), then the gate with `strategies/solana.py::momentum_signal`. `services/engine-solana-momentum` is legacy transfer-based scaffolding and NOT_DEPLOYED. |
| Signal | `strategies/solana.py` (fresh_launch_signal, momentum_signal, post_migration_signal) |
| Decision + risk | `safety/gate.py::assess` (every finding), `safety/planning.py::plan_trade` (stop, size, TP, trailing; costs incl. fixed live costs) |
| ML | `ml/gate_features.py`, `ml/registry.py`; `services/ml/app/gate_ml.py` (per-engine challenger training, drift, data quality). Only traded positions are labelled. |
| ML Review | `apps/api/.../ml.py` (+ `/ml/opportunities`), `opportunities.py` (all opportunities with horizons 5 s–30 min, loss analysis) |
| Execution | `live_trading.py` (orders, apply_outcome), `solana/live_exec.py` (executor), `solana/tx_builders.py`, `solana/pump_tx.py`, `solana/txguard.py`, `solana/rent_reclaim.py`, `paper-trading/app/live_worker.py` |
| Position manager / exits | `paper-trading/app/gate_manage.py` (marks, `exit_intel.solana_exit_decision`), `paper_engine.manage_step` (stop / TP / trailing), `live_trading.manage_live_position` |
| RPC | `solana/rpc.py` (priority classes, failover, capabilities), `solana/rpc_registry.py` |
| Database | `db/models.py`; Alembic in `apps/api/migrations` |
| Event bus | Redis pub/sub `events.publish` → API WebSocket → dashboard `useApi reloadOn` |
| Dashboard | `apps/web` (Next.js) |

The live execution hops are documented step by step in `EXECUTION_PATH_MAP.md`:
- §1 BUY
- §2 SELL
- §2b token-account close

## 2. DO NOT BREAK

These modules are not changed by this upgrade.

**Transaction building, guarding and signing**
- `solana/live_exec.py`: `execute`, `_send_and_confirm`, `lookup`, `parse_fill`, `close_token_accounts`
- `solana/tx_builders.py`: `NativePumpBuilder`, `PumpPortalBuilder`, `JupiterBuilder`
- `solana/pump_tx.py`: instruction layouts
- `solana/txguard.py`: `inspect`, `inspect_close_accounts`

**Routing and RPC**
- `solana/venue.py`: route and migration re-check
- `solana/rpc.py`: failover, priority classes, capabilities

**Orders, fills and exits**
- `live_trading.py`:
  - `enter_live`, `request_live_exit`, `process_order`
  - `apply_outcome` (fill and cost basis)
  - `reconcile`, `request_rent_reclaim`
- `paper-trading/app/live_worker.py`: pickup and processing
- `paper_engine.manage_step`: stop / TP / trailing mechanics
- `safety/planning.py`: sizing invariant (loss at stop ≤ max loss)

**Additions only:**
- new features;
- new findings, which only tighten or inform;
- data capture;
- reports;
- shadow models.

No existing safety finding is loosened.

## 3. Engine audit

| Engine | Current behaviour | Existing features | Missing (vs request / research) | Risk | Change |
|---|---|---|---|---|---|
| Fresh | Stream → observation T0 / T+half / T+window (10 s default) → continue monitoring up to 15 min → promote → gate | buyers, sellers, volume, sell pressure, drawdown from peak, creator history, duplicate names, early-buy share, sync-buy cluster, round-trip share, churn, funding links, entry deterioration, exit-check-at-entry, volatility confidence | time series T0/5/10/20/30/60 s with curve velocity / acceleration; trades-to-reach-SOL efficiency; buyer breadth; Mayhem regime (curve math invalid); instant-bond; manipulation score across signal families; smart-money / cohort features; causal data-quality metadata | Mayhem tokens are priced and sized with constant-product math that does not hold for them (see R1) | Launch features, Mayhem regime finding, manipulation score, wallet intelligence |
| Migrated | Venue from chain; pool trades + pool state; migrated liquidity report (USD); post_migration_signal | pool liquidity, impact, fees, SOL/USD liquidity rule | post-migration state (DUMPING / STABILIZING / RECOVERING / CONTINUING / WEAK); instant-bond flag; BOOST window awareness (first 5 min after migration are protocol buybacks since 2026-07-21, R3) | Post-migration flow in the BOOST window is read as organic demand | Post-migration classifier + BOOST window finding |
| Momentum | Funnel scans ACTIVE (any traded token ≥ 30 min old or ≥ 70% curve progress) → momentum_signal (acceleration 1 m vs previous) | trade-rate / volume / price acceleration | buyer / seller / new-wallet acceleration, 1 m / 5 m returns as features, smart-money acceleration | Low | Momentum features in the same launch-feature module |
| Decision | `assess`: every finding → decision; operator request bypasses signal / ML only | complete safety gate | the new findings above; ML shadow targets as evidence only | — | Additive findings (configurable) |
| Risk | `plan_trade` + account limits, kill switch; live fixed costs | loss at stop incl. costs | — | — | Unchanged |
| ML | One gate model per engine, trained only on traded positions (label = realized PnL > 0) | champion / challenger, drift (PSI), data quality | learning from non-traded opportunities; multiple targets; executable returns; time-based evaluation with PR-AUC / calibration / precision@K; regime tags | Selection bias: only trades the gate already allowed are learned | Multi-target SHADOW models on the opportunity ledger (never used for decisions) |
| ML Review | Opportunities with horizons 5 s–30 min, winners vs losers, traded vs rejected-that-rose, LOSS_ANALYSIS | horizons, peak, drawdown, loss classes | missed-winner analysis with the path and the drawdown before the peak; premature / late exit analysis after exit; recovery paths; executable (not market-cap) returns; signal vs execution quality; snipe latency | Hindsight: "rejected then pumped" read as a mistake | Counterfactual classification + exit analysis + path view |
| Execution | See EXECUTION_PATH_MAP | — | — | — | **DO NOT BREAK: unchanged** |

## 4. Research (2026)

**Access limits.** pump.fun, arxiv.org, launchwatch.dev and major news
sites are blocked by this environment's network egress. They were read
through search-engine summaries, which are secondary sources. GitHub
repositories were cloned and read directly.

### R1. Mayhem Mode tokens do not follow the constant-product curve
- **VERIFIED FACT:** `is_mayhem_mode` is a BondingCurve field. Checked in the
  official docs repo (PUMP_PROGRAM_README) and already decoded in
  `solana/pumpfun.py`. `buy_v2` selects one of 8 Mayhem-reserved fee
  recipients.
- **EXPERIMENTAL FINDING** (meme-sniper STREAM_NOTES §7, 2026-08-03, n=37,
  checked against chain):
  - every Mayhem token violated `vSol × vTok = k`; every non-Mayhem token
    held it (residual ≤ 1e-9);
  - virtual SOL was observed below 30 and above 115 with `complete = false`;
  - Mayhem share per sample swung between 3% and 38%.
- **SOURCE CLAIM / ANECDOTAL** (news, via search):
  - an AI agent trades Mayhem coins randomly (roughly direction-neutral)
    for 24 h;
  - 1B extra supply is minted;
  - unsold tokens are burned at the end.
- **INFERENCE:** YonixAlpha prices, simulates and sizes curve tokens with
  constant-product math (`safety/liquidity.py`, `planning.py`). For Mayhem
  tokens those numbers are not merely noisy but invalid. Their flow also
  contains non-organic agent trades.
- **→ Implemented:**
  - Mayhem is a separate regime;
  - the curve-math validity check (k consistency across trades) is recorded;
  - automatic entries are refused by default (configurable).

### R2. Instant bonds
- **EXPERIMENTAL FINDING** (meme-sniper):
  - create and migrate were observed 24 ms apart: a create plus full-curve
    buy in one bundle, which nobody else can buy into;
  - they flag gaps under 5 s and exclude them from graduation rates.
- **→ Implemented:**
  - `instant_bond` classification;
  - excluded from migration labels;
  - its post-migration trading is kept as its own regime.

### R3. BOOST (2026-07-21)
- **SOURCE CLAIM** (meme-sniper README / STATUS; news via search):
  - about 17.6 SOL of buybacks and burns in the first 5 minutes after
    migration;
  - graduation rate reported to go from about 0.2% to 4.7–6.7%.
- **INFERENCE:**
  - post-migration buyer flow in the first 5 minutes is partly
    protocol-driven;
  - models or thresholds fitted before 2026-07-21 describe a different
    regime.
- **→ Implemented:**
  - `data_regime` tag (pre/post BOOST);
  - BOOST-window flag on post-migration analysis and findings.

### R4. Market-cap returns overstate executable returns
- **EXPERIMENTAL FINDING** (solana-sniper-reverse-engineering, Jan–Jun 2026
  census, 5,075,807 deployments):
  - market-cap P&L overstated the studied bot's real median ROI (+6.6%) by
    7.2–16.6×;
  - their fill model was calibrated on 4,195 real fills (out-of-sample
    R² 0.47).
- **EXPERIMENTAL FINDING** (solana-launch-study, about 8 h, 8,587 launches):
  - T+30 s entries lost 9.7–17.0% across all 48 exit rules tested (n=177);
  - the median loss (−4.6%) is about the round-trip cost;
  - "Predicting graduation and profiting from it are different problems."
- **→ Implemented:**
  - executable-return simulation (curve fills at a reference size, program
    fees, fixed network costs) stored next to theoretical returns;
  - ML targets use executable returns.

### R5. Trade efficiency
- **SOURCE CLAIM** (meme-sniper config, citing earlier literature):
  - `trades_to_reach_vsol` at vSOL checkpoints is "the strongest published
    predictor" (inverse);
  - meme-sniper itself has no post-BOOST labels yet.
- **→ Implemented:**
  - trades and seconds to reach SOL-accumulated checkpoints;
  - meaningful-trade ratio;
  - as a **feature only**, to be evaluated on our own outcomes.

### R6. Creator history must be causal
- **EXPERIMENTAL FINDING** (solana-launch-study FINDINGS §2.3):
  - a non-causal creator launch count roughly doubled the apparent effect;
  - 0 prior launches (60 min) were 83.4% dead vs 97.1% for 10+ prior.
- **→ Check:**
  - YonixAlpha's creator history is counted at decision time (on chain +
    stream), so it is causal by construction;
  - the snapshot stores `as_of`;
  - UNKNOWN stays UNKNOWN.

### R7. Metadata origin and socials
- **EXPERIMENTAL FINDING** (solana-launch-study, one 8 h sample):
  - `ipfs.io` metadata migrated 6.2% vs `metadata.j7tracker.io` 0.6%.
- **SOURCE CLAIM** (meme-sniper): Telegram-advertising tokens graduate
  8.94× more often.
- **→ Implemented:** metadata host recorded as a feature; no rule.

### R8. Wallet reputation
- **SOURCE CLAIM** (three.ws smart-money.md):
  - reputation from graduation outcomes about 6 h after launch;
  - top 60 buyers per coin;
  - win rate, early-win rate (bought within 180 s), dump rate (sold ≥ 50%);
  - confidence = `judged / 12`;
  - smart-money labelling kept separate from sybil / funder clustering.
- **→ Implemented (own method):**
  - Beta-Binomial shrinkage toward the observed base rate, with a posterior
    lower bound, so 1/1 ≠ 100/100;
  - computed only from outcomes resolved before the decision time (causal);
  - no whitelist.

### R9. Coordinated early buyers ("sniper cohorts", dump clusters)
- **SOURCE CLAIM** (arXiv 2607.02795, via search; 166,098 launches,
  2026-06-11..25):
  - 1,012 persistent cohorts of 2–12 wallets;
  - found with union-find on cross-launch co-occurrence of first-buyer
    windows.
- **SOURCE CLAIM** (MadeOnSol): "3+ [cluster wallets] → 94% dump vs 61%
  base". Not reproduced; **not** used as a threshold.
- **→ Implemented:**
  - persistent co-occurrence cohorts among current early buyers (causal
    lookback);
  - their historical fast-dump rate;
  - `dump_cluster_score` UNKNOWN / LOW / MEDIUM / HIGH with evidence;
  - thresholds configurable.

### R10. Manipulation
- **SOURCE CLAIM** (arXiv 2609.10246 "Meme Coin Factories", via search):
  - 15 M coins over two years;
  - five manipulation classes: wash trading, creator-address obfuscation,
    coordinated sells, copycat coins, social media manipulation;
  - wash trading at least 17% of transactions;
  - 82.8% of >100% return tokens showed artificial growth.
- **SOURCE CLAIM** (launchwatch.dev, via search): manufactured pumps and
  single-block collapses detected from 1-minute candles.
- **INFERENCE:** many "missed winners" will be manufactured. Counterfactual
  analysis must show the manipulation score at rejection, not just the
  later gain.
- **→ Implemented:** a `manipulation` score built from independent signal
  families:
  - wash / round-trip;
  - synchronized buys;
  - same-second sell clusters;
  - regular trade sizes;
  - dust buys;
  - straight-line price;
  - creator-linked funding;
  - dump cohort;
  - copycat name.
  - HIGH needs several families, never one indicator.

### R11. Execution latency
- **EXPERIMENTAL FINDING** (reverse-engineering):
  - the studied bot's median entry was +118 transactions after the create,
    in the same block;
  - 79.6% of entries were in block zero.
- **INFERENCE:**
  - YonixAlpha's measured decision → confirm is about 5.5 s (trade_report,
    2026-09-28);
  - it is not a block-zero sniper;
  - fresh signals must be judged against that latency tier;
  - snipe-quality latency is shown in ML Review.

## 5. What this upgrade does not claim

- No new feature is claimed to predict profit. Each is recorded, causal and
  evaluated on YonixAlpha's own executable outcomes.
- External constants are not copied as rules.
- Shadow models never influence live decisions.
- Position size is never increased by ML confidence.
