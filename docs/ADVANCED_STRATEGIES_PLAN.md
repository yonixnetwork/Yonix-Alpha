# Advanced Strategies: Phase 1 audit and implementation plan

Date: 2026-10-15. Status: PLAN ONLY. No application code has been changed for this work yet.

Scope: bring back the non-memecoin strategies (Meta Muse Crossover, Gold vs BTC, Hyperliquid Grid, Confluence Matrix) as a separate "Advanced Strategies" area. They run in DEMO (simulated execution) only, and the Memecoin system is left untouched.

Labels used below:
- VERIFIED: checked in code, git, or a command run in this audit.
- NOT VERIFIED: not checked yet; says what would check it.
- BLOCKED: cannot be done without something that is missing.

---

## 1. Starting state (VERIFIED)

- **Branch:** `claude/yonixalpha-platform-architecture-804nb5` at `afd6897`.
- **main:** `661fb5b`, the merge of PR #80; it is the same tree as `afd6897`.
- **Working tree:** clean, with no uncommitted changes from any session.
- **Baseline tests:** a full run of every suite on this tree today (Oct 15), done one suite at a time:

  | Suite | Result |
  |---|---|
  | core | 1073 passed |
  | api | 189 passed |
  | data-solana | 11 passed |
  | data-evm | 14 passed |
  | copy-engine | 18 passed |
  | engine-solana-discovery | 33 passed |
  | engine-solana-migration | 11 passed |
  | engine-solana-momentum | 11 passed |
  | decision-engine | 79 passed |
  | ml | 50 passed |
  | paper-trading | 88 passed |

  Ruff was clean, and CI passed 26/26 on `afd6897`. This is the Memecoin regression baseline.
- **Memecoin settings baseline:** the server's `recovery_check` output (Oct 15, 12:27 UTC) is the record. Nothing in this plan changes any key in `risk_settings_versions`.

## 2. Why the strategies are not in the app today (VERIFIED)

- **Removal:** commit `7e87552` (Sep 29), "Remove futures, forex, grid and legacy strategies from the active app".
- **Archive:** the full tree before removal is kept on branch `archive/legacy-futures-forex-grid-2026-09-29` (`f3d833b`).
- **Reason:** `docs/MULTICHAIN_AUDIT_2026.md` section 9. It was an operator decision to focus the app on memecoins.
  - No bug, safety problem or loss is recorded as the reason.
  - Their DB tables and rows were kept as history.

What the archive contains and how it will be reused:

| Archived module | What it is | Reuse |
|---|---|---|
| `strategies/meta_muse.py` | Meta Muse rules port | Port the rules (logic unchanged) |
| `strategies/gold_btc_trend.py` | Gold vs BTC dual trend (trading) | Port the rules |
| `strategies/gold_btc.py` | BTC/gold ratio analytics (no trading) | Port as analytics only |
| `strategies/grid.py` | Grid math + circuit breakers | Port the math; rewrite the fill model (section 6) |
| `strategies/confluence.py` | Confluence Matrix scoring | Port; see the proximity-rule decision |
| `strategies/indicators.py` | EMA / RSI / ATR matching pandas | Port |
| `venues/binance_public.py`, `venues/hyperliquid.py` | Public market-data clients | Port the public (unauthenticated) parts only |
| `execution/*`, `futures_live.py`, `grid_live.py`, `services/execution-futures` | LIVE order sending | **Not restored.** This work is demo only. |
| `external_bots.py`, the bots' `control_api.py` | Remote control of the standalone bots | **Not restored.** It would run third-party processes with exchange keys. |

## 3. Source repositories (VERIFIED by clone and read, no code executed)

| Strategy | Public original (commit) | Your copy (commit) | Licence | Differences in your copy |
|---|---|---|---|---|
| Meta Muse Crossover | `hoziertom44-arch/Meta_Muse_Crossover_strategy` `a163ff7` (5 commits, Apr 11) | `yonixnetwork/meta-muse-crossover-strategy` (private) `ab3b52d` | **none in either** | ccxt instead of python-binance (Binance or Bybit); `control_api.py` added; keys moved to `.env`. Rules unchanged. |
| Hyperliquid Grid | `hoziertom44-arch/hyperliquid_grid_trading_bot` `710cebb` (7 commits, May 18) | `yonixnetwork/hyperliquid-grid-trading-bot` (private) `bfec033` | **none in either** | `control_api.py` added; client and config changes. `grid.py`, `risk.py`, `state.py` identical. |
| Confluence Matrix | `siriusforex-ai/confluence_matrix_forex` `f3beff9` (3 commits, May 9) | `yonixnetwork/confluence-matrix-forex` (private) `8389d89` | **none in either** | **The proximity score rule was changed** (see 5.4); `.env` loading; `position_state.py` and `tests/` added. |
| Gold vs BTC | no public original found | `yonixnetwork/goldvsbtc-binance-future` (private) `332bbca`, 1 commit | **none** | Only your copy exists. The README credits "RektCoder". |

Findings:
- **No licence anywhere.** Without a licence the code is "all rights reserved" by default. The plan therefore re-implements the trading *rules* in YonixAlpha's own code, as the archived ports did, and does not copy files. Each module records its source repo and commit for attribution.
- **Forking:**
  - Your organisation `yonixnetwork` already holds private copies of all four repos, so no new fork is needed to keep the sources in your account.
  - I did not fork anything. Forking an unlicensed third-party repo needs the owner's permission.
- **Security review of the sources:**
  - No `eval`, `exec`, `subprocess`, encoded payloads or hidden network calls were found.
  - The only outbound call besides the exchanges is the optional Telegram alert in the grid bot.
- **Things not to copy:**
  - Referral links and branding (Hyperliquid referral code, Telegram channels) in the grid and gold repos.
  - The public Confluence repo commits a 10-digit MT5 account number in `config.py`. Its password field looks like a placeholder.
  - The public grid repo ships `trades.jsonl` with 134 order *placements* (no fills, no PnL). It is not a performance record.
- **No backtest results** are claimed by Meta Muse, the grid or Gold vs BTC. Confluence ships `backtest.py` without results. No profitability claim is taken from any README.

## 4. Protected Memecoin boundary (VERIFIED from the code layout)

**Rule:** no file below is edited by this work.

| Area | Protected paths |
|---|---|
| Solana / memecoin services | `services/data-solana`, `services/engine-solana-discovery`, `services/engine-solana-migration`, `services/engine-solana-momentum`, `services/decision-engine`, `services/paper-trading`, `services/ml`, `services/copy-engine`, `services/data-evm` |
| Shared core | `packages/core-py/**` (safety gate, risk settings, ENGINE_DEFAULTS, solana/*, pump*, paper_engine, paper_execution, live_trading, opportunities, rpc_usage, kill_switch, retention, system_profile, db/models.py, config.py) |
| Existing API routes | every file in `apps/api/app/api/routes/` that exists today |
| Existing pages | every page under `apps/web/app/dashboard/` that exists today, and `apps/web/lib/*` |
| Deploy / profiles | `scripts/deploy.sh`, `scripts/compose-profiles.sh`, existing service blocks in `infra/docker/*.yml` |
| Data | all existing tables and rows; no migration touches them |

**Why new code cannot go in `packages/core-py`:** every Memecoin service image copies `packages/core-py` (`COPY packages/core-py /packages/core-py` in each Dockerfile). Any change there rebuilds and restarts every Memecoin container on the next deploy, which is what the Oct 15 deploy showed. Keeping `packages/core-py` byte-for-byte unchanged is what keeps the Memecoin images identical.

**Shared files that need a small, additive edit.** Each change is new lines only, and none changes Memecoin behaviour:

| File | Change | Why it is safe |
|---|---|---|
| `apps/web/app/dashboard/layout.tsx` | one new nav group "Advanced Strategies" below "Trading" | UI list entry; no existing item changes |
| `apps/api/app/api/router.py` | `include_router(advanced.router)` | new `/api/advanced/*` paths only |
| `apps/api/Dockerfile`, `apps/api/requirements.txt` | copy and install the new package | API image only; Memecoin images unaffected |
| `apps/api/migrations/env.py` | also register the new package's metadata | needed so `alembic check` knows the new tables |
| `apps/api/migrations/versions/0048_*.py` | **new** tables only (`adv_*`) | additive, reversible (downgrade drops only `adv_*`) |
| `infra/docker/docker-compose.yml` (+ prod) | one **new** service block `advanced-strategies` | other service blocks untouched, so compose does not recreate them |
| `.github/workflows/ci.yml` | one new matrix row for the new package's tests | CI only |

The API and web containers restart on deploy; they serve the dashboard, not the trading engines. The Memecoin engine containers (data-solana, engine-solana-discovery, decision-engine, paper-trading, ml) are not rebuilt or restarted. The deploy section gives a targeted command that names only the changed services.

## 5. Strategy findings

### 5.1 Meta Muse Crossover (VERIFIED from source)

The name says "crossover", but the source is an **inverse-divergence** strategy:
- **Trend:** EMA(9) vs EMA(21) on 5m closes of BTCUSDT and ETHUSDT. A trend is strong when |fast - slow| / slow >= 0.15%.
- **Entries:** BTC up-strong with ETH down-strong gives SHORT ETH. BTC down-strong with ETH up-strong gives LONG ETH.
- **Exits:**
  - Exit early when either trend weakens or the signal no longer matches the position.
  - Fixed stop at 1% and take-profit at 2%.
- **Original settings:** $50 margin at 10x, checked every 60 s.
- **Original flaw:** it reads the still-forming candle, so decisions are not reproducible afterwards.
- **Adaptations:**
  - Closed candles only.
  - Demo sizing from a per-strategy demo balance and risk limit.
  - Leverage default 1x (configurable, capped).
- **Data:** Binance USDⓈ-M public klines (no key).

### 5.2 Gold vs BTC dual trend (VERIFIED from your private repo)

- **Type:** a directional strategy that uses one asset (gold) as a filter for trading the other (BTC).
- **Trend:** EMA(9) vs EMA(21) on 15m candles. Strength thresholds are separate per asset:
  - gold: strong at >= 0.005% gap;
  - BTC: strong at >= 0.03% gap.
- **Entries:** gold UP-strong with BTC DOWN-strong gives SHORT BTC. Gold DOWN-strong with BTC UP-strong gives LONG BTC.
- **Exits:**
  - Exit when either trend weakens, when both move the same way, or when the signal flips.
  - Stop 1%, take-profit 2%.
- **Fidelity details:**
  - The original computes its EMAs on only 31 candles, while the archived port used 150, so the values differ slightly. The plan keeps 31 by default to match the original; 150 is available as an option.
  - The original re-opens the opposite side at once on a flip. The port decides on the next closed candle.
- **Instrument (NOT VERIFIED):**
  - The original trades `XAU/USDT:USDT`.
  - Earlier docs say Binance listed XAUUSDT perpetuals in 2026-01, but nobody ever checked it live (`IMPLEMENTATION_MATRIX.md`: "NOT VERIFIED (live)").
  - This sandbox's network blocks Binance, and **your server is in a US data centre (nyc3)**. Binance.com APIs often refuse US IPs (HTTP 451).
  - Both must be checked on the server (section 9). If XAUUSDT is not reachable there, the strategy stays BLOCKED. PAXG is a different instrument and will not be substituted silently.
- **Two-instrument handling:**
  - Align on candle open time, closed candles only.
  - Skip when either series is stale or missing a bar.
  - Charge funding on the traded BTC perp.
- **Ratio analytics:** `gold_btc.py` (ratio, z-score, correlation) stays descriptive, with no trades.

### 5.3 Hyperliquid Grid (VERIFIED from source)

- **Range and levels:**
  - Range is mid ± range_pct% (auto) or manual bounds.
  - Levels are grid_levels + 1, evenly spaced.
  - Size per level = capital × leverage / levels / range midpoint.
- **Orders:**
  - Buys below mid, sells above; the level within half a spacing of mid is skipped.
  - A filled buy is replaced by a sell one spacing up, and vice versa.
  - Orders are post-only (ALO).
- **Risk:** pause at max drawdown % or when price leaves the range by range_break_pct.
- **Original defaults:** BTC, ±0.5%, 7 levels, $15 at 5x, mainnet.
- **Gaps in the original:**
  - **No restart recovery.** A restart builds a fresh grid at the current price, and old orders are left behind.
  - **No fee or inventory modelling.**
  - **No cap on one-sided inventory** except the drawdown pause.
- **Demo design:**
  - Fills only when the traded price moves **through** the level by at least one tick (not merely touching it), from Hyperliquid public trades or mid. This still assumes queue priority, and that is stated in the UI.
  - Maker fee charged.
  - Funding charged on net inventory.
  - Worst-case loss checked before start.
  - Grid state persisted, so a restart resumes the same grid instead of rebuilding it.
  - A max inventory per side.

### 5.4 Confluence Matrix (VERIFIED from source)

- **Pivots:**
  - Symmetric pivots (lookback 12), confirmed 12 bars later, so there is no look-ahead.
  - Entry is a Market Structure Break: the close crosses the last confirmed pivot high (BUY) or low (SELL).
- **Score (0-100):**

  | Component | Points |
  |---|---|
  | structure | +30 |
  | liquidity grab | +25 |
  | RSI side | +20 |
  | proximity | +25 |

  - V1 mode trades every MSB; V2 needs score >= 75.
- **Plan:**
  - Stop beyond the opposite pivot plus 5% of the wave.
  - TP1 at pivot + 0.618 × wave (close 50%, stop to breakeven); TP2 at 1.414 × wave.
  - Cooldown 5 bars; optional killzones.
- **Original market:** GOLD, EURUSD and GBPUSD on M15 through **MetaTrader 5** (broker XM). MT5's Python package is Windows-only.
- **Proximity rule (DECISION NEEDED):**
  - The public original's rule (`low <= lastL + ATR or high >= lastH - ATR`) is true almost every bar.
  - Your copy changed it to "close within 1 ATR past the broken pivot".
  - It only matters in V2 mode. The plan implements both, labelled, with the original as default unless you choose otherwise.
- **Forex (BLOCKED):**
  - The app has no forex data feed or broker connection, and MT5 cannot run on the Linux server.
  - EURUSD/GBPUSD stay BLOCKED until you choose a data source (a broker API with a free practice account needs your account and credentials).
  - The same rules can run in demo on Binance XAUUSDT, **labelled as an adapted instrument** (a perpetual future that tracks spot gold, not XM's GOLD CFD), if the server check passes.

### 5.5 Other removed strategies

- The archive holds no other non-memecoin strategy. The only other module in `strategies/` was `solana.py`, which is still active.
- No mean-reversion or breakout module was ever written.
- The Bybit venue and LIVE futures execution are not restored (live is out of scope).

## 6. Architecture (to build in Phase 2)

- **New package:** `packages/advanced-py` (`yonixalpha_advanced`), outside `packages/core-py`.
  - It holds strategies, indicators, the public market-data clients, a demo executor, demo accounts, per-strategy settings and risk.
  - It may *read* two things from core-py, without changing core-py: the DB session factory and the global kill-switch Redis key.
- **New worker:** `services/advanced-strategies`.
  - One process with a memory limit (target 160 MB).
  - It polls public candles once per closed bar and grid prices every few seconds.
  - It does nothing while every strategy is disabled (the default).
- **New tables (migration 0048, additive):**

  | Table | Holds |
  |---|---|
  | `adv_strategy_config` | one row per strategy; versioned; saving one never touches another |
  | `adv_signals` | signal history |
  | `adv_demo_accounts` | demo balance per strategy, separate from every Memecoin paper account |
  | `adv_demo_positions` | demo positions |
  | `adv_demo_fills` | fees, slippage, funding per fill |
  | `adv_grid_state` | grid state, so a restart resumes the same grid |
  | `adv_events` | errors and blocked reasons |

- **Demo-only enforcement (structural, not a switch):**
  - The package contains no exchange-key setting, no signing code and no private-endpoint client.
  - The only executor is `DemoExecutor`. The mode field accepts only `DEMO`.
  - A test fails the build if the package imports signing libraries (`eth_account`, `hmac`-signed Binance requests, `ccxt` private calls) or references key environment variables.
- **Controls:**
  - Per-strategy enable/disable stored in `adv_strategy_config` and enforced by the worker.
  - The API refuses unknown keys.
  - The global kill switch also stops them; it is read only, never written.
- **UI:** a new nav group "Advanced Strategies" with an overview and one page per strategy. Each page shows:
  - status, settings, signals, demo trades;
  - fees, slippage and funding;
  - realized / unrealized PnL;
  - win rate and profit factor with the sample size, shown as "unavailable" below a minimum sample;
  - max drawdown;
  - errors and blocked conditions.
- **Costs modelled:**
  - taker/maker fees per venue;
  - spread from the book where available, otherwise a configurable spread;
  - slippage by size;
  - funding every 8 h (Binance) or hourly (Hyperliquid) at the published rate;
  - latency as one bar or a configurable delay;
  - stale data refusal.

## 7. Order of work (each step its own PR, CI green before the next)

1. **Foundation:**
   - package, worker skeleton, tables, per-strategy config API, demo executor, demo-only tests;
   - the new tab with status and controls (no strategy logic yet);
   - Memecoin regression suites rerun.
2. **Meta Muse:** strategy, demo trades, unit and replay tests on recorded candles.
3. **Gold vs BTC:** only after the server check confirms XAUUSDT is reachable; otherwise ship it BLOCKED with the reason shown.
4. **Grid:** Hyperliquid public data, demo fills, restart recovery, trend and gap tests.
5. **Confluence:** XAUUSDT adapted demo if allowed; forex BLOCKED.
6. **Backtests:**
   - small, chronological train/test splits with all costs, run locally or in CI, not on the VPS;
   - every strategy stays disabled unless its results justify enabling demo.

## 8. Resources (NOT VERIFIED)

- The server name says 2 vCPU / 2 GB.
- Current usage must be measured on the server before the worker is deployed (commands in section 9).
- The worker starts idle and is capped by a container memory limit.

## 9. Server checks needed (read only, you run them)

These answer the open questions before any strategy code depends on them:

- Binance USDⓈ-M reachable from the server, and XAUUSDT, BTCUSDT and ETHUSDT listed and trading.
- Hyperliquid public info API reachable.
- Current memory and CPU of each container.

## 10. Operator decisions (2026-10-15)

| Question | Decision |
|---|---|
| GitHub | No new forks. The yonixnetwork private copies are the record; rules are re-implemented with attribution (repo + commit), no files copied. |
| Confluence proximity rule | The public original's rule is the default; your corrected rule is a labelled option. |
| Forex | Confluence runs in demo on Binance XAUUSDT, labelled as an adapted instrument, if the server check passes; EURUSD / GBPUSD stay BLOCKED. |
| Gold vs BTC EMA history | 31 candles (as the original); 150 available as a setting. |

## 11. Server checks (2026-10-15, run by the operator)

- **Binance USDⓈ-M: BLOCKED from the server.**
  - `fapi/v1/ping` returns HTTP 451.
  - The ticker for XAUUSDT, BTCUSDT and ETHUSDT returns "Service unavailable from a restricted location according to 'b. Eligibility'".
  - The server (DigitalOcean nyc3) is in a location Binance restricts. YonixAlpha will not route around this (no proxies or VPNs), because that would break Binance's terms.
  - So every Binance-based design (Meta Muse on BTCUSDT/ETHUSDT, Gold vs BTC on XAUUSDT/BTCUSDT, Confluence on XAUUSDT) is BLOCKED as planned. It needs another data venue (operator decision) or stays blocked.
- **Hyperliquid: REACHABLE from the server.**
  - The main perp list (`meta`) includes BTC, ETH and PAXG.
  - `perpDexs` lists a builder-deployed dex `xyz` (HIP-3) with commodity and stock perps (BRENTOIL, CL, COPPER, CORN, ...). Whether `xyz:GOLD` exists is NOT VERIFIED yet (the output was cut at 800 characters).
  - Hyperliquid's terms also restrict some locations for *trading*. Demo mode uses only public market data and sends nothing.
- **Memory: tight.**
  - 1967 MB total, 388 MB available, and 1001 MB of swap already in use.
  - The largest users are postgres (273 MB) and redis (268 MB).
  - The new worker must be small and capped (target 128 MB), and it must be possible to leave it stopped.

## 12. Audit checklist for the Crypto Trading engine (2026-10-15, second directive)

**Repository.** `raymondyonpandadugul-cloud/yonix-alpha` cannot be read from this session: an anonymous clone asks for credentials, so it is private or does not exist. The server's deploy log reads "From github.com:yonixnetwork/Yonix-Alpha", so production deploys this repository, and the work continues here.

| Item | Status | Evidence |
|---|---|---|
| Memecoin engine and its controls (system profile, kill switch, per-engine risk) | COMPLETED, PROTECTED | all suites pass at `afd6897`; untouched |
| Resource monitoring (host, Postgres, Redis, services) | COMPLETED (Memecoin side) | `yonixalpha_core/resources.py`, `GET /system/resources`; read-only reuse allowed |
| Global kill switch | COMPLETED | Redis key `yonixalpha:kill_switch:engaged` (`kill_switch.py`); the new engine reads it, never writes it |
| Crypto Trading engine (regular crypto, separate from Memecoin) | NOT IMPLEMENTED | removed Sep 29 (`7e87552`); archive only |
| Bybit public market data | BLOCKED on this server | server: HTTP 403 on `/v5/market/time` (Oct 15); the adapter exists and is tested, but it is not used from this server |
| Binance (binance.com) public market data | BLOCKED on this server | server: HTTP 451 "restricted location" (Oct 15) |
| Binance.US public market data (separate exchange, spot only) | VERIFIED reachable | server: HTTP 200 on `/api/v3/ping` (Oct 15); a different exchange entity with its own fees and instruments, configured separately and never presented as binance.com |
| Exchange abstraction (symbols, precision, minimum sizes) | NOT IMPLEMENTED (current app) | archive `venues/common.py` only had Ticker / Candle types |
| Crypto demo execution and accounting | NOT IMPLEMENTED | the Memecoin paper engine exists but is protected and SOL-denominated; it is not reused |
| Crypto strategy registry and per-strategy config | NOT IMPLEMENTED | the Memecoin registry (`strategy_registry.py`) is protected |
| Meta Muse module | NOT IMPLEMENTED | rules verified (section 5.1) |
| Gold vs BTC module | BLOCKED | Binance 451; gold source choice pending (PAXG or `xyz:GOLD` on Hyperliquid, check pending) |
| Hyperliquid grid module | NOT IMPLEMENTED | Hyperliquid public API reachable from the server (Oct 15) |
| Confluence on forex | BLOCKED | no forex data source; MT5 is Windows-only (`requirements.txt: MetaTrader5`) |
| Live order paths for new modules | NOT IMPLEMENTED (deliberately) | demo only |

**Candle counts (verified in source, correcting the brief):**
- **Meta Muse** fetches **150** candles (`config.py: MAX_KLINES = 150`), so its original-compatible default is 150.
- **Gold vs BTC** fetches **31** (`bot.py: get_candles(symbol, SLOW_MA_PERIOD + 10)`), so its default is 31, with 150 as an option (operator decision, section 10).
- Both originals read the still-forming candle. The ports use closed candles only (no look-ahead, reproducible).

**Research repositories (licences checked by shallow clone; no code executed):**

| Repository | Commit | Licence | Use |
|---|---|---|---|
| freqtrade/freqtrade | b330689 | GPL-3.0 | ideas only; YonixAlpha has no licence file and is public, so GPL code is not copied |
| freqtrade/freqtrade-strategies | f3340ce | GPL-3.0 | ideas only |
| EA31337/EA31337-Libre | 146fe75 | GPL-3.0 | ideas only (MQL, MetaTrader) |
| EA31337/EA31337-strategies | 55fcb1f | GPL-3.0 | ideas only (MQL) |
| ArturSepp/TrendFollowingSystems | b1d75d9 | GPL-3.0 | ideas only |
| arwensorcenelli/trend-bot | ab75bb1 | none | research only: 10/50 MA trend with a 3×ATR stop on index futures, next-day execution |
| grinay/geektrade-strategies | 855641d | MIT | candidate: Pine strategies built for Bybit (ETH Momentum Breakout; its README claims 56% win rate and PF 3.08, NOT VERIFIED, not reused as evidence) |
| Solana references (6 repos) | see clone log | none | research only; Memecoin is protected |

**Next step (smallest safe unfinished task):** the Crypto Trading engine foundation (section 13), with no strategy trading yet.

## 13. Foundation design (implemented in the next commits)

- **Package:** `packages/crypto-py` (`yonixalpha_crypto`). It does not import `yonixalpha_core` at runtime.
- **Database:** own Postgres schema `crypto`, with its own Alembic history (`crypto.alembic_version`).
  - The main Alembic chain, `apps/api/migrations`, and every existing table are untouched.
  - The main Alembic autogenerate compares only the default schema.
- **Exchanges:** each is configured separately with its own base URL, fee assumption (labelled "ASSUMED, not account-verified"), symbols and precision rules:

  | Exchange | API |
  |---|---|
  | `bybit` | V5 spot |
  | `binance` | binance.com spot |
  | `binance_us` | api.binance.us spot |

- **Public-only HTTP client:**
  - It allows only GET requests to listed public market-data paths.
  - It refuses order paths, signature parameters and API-key headers before any request is sent.
  - The package holds no credentials.
- **Modes:** only `DISABLED` and `DEMO`. Any other mode is rejected by validation and again at the executor.
- **Risk:** quote-currency (USDT) limits per strategy:
  - risk per trade, maximum position, maximum open positions;
  - daily loss, total exposure;
  - stale-data rejection, minimum order size, precision rounding.
- **Demo executor:**
  - fills at the ask (buy) or bid (sell), plus a slippage allowance;
  - charges taker fees;
  - rejects orders below the minimum size;
  - uses an idempotent client id, so a repeated signal cannot double-fill;
  - keeps separate demo accounts per exchange.
- **Worker:** `services/crypto-trading`.
  - Memory limit 128 MB and CPU 0.25 in compose.
  - Runs only while the engine is ON or a demo position is open; otherwise it exits to free memory. While running it checks its config every 30 s and runs a public exchange health check every 10 min.
  - It honours its own emergency stop and the global kill switch.
- **API:** `/api/crypto/*` (authenticated), in a new route file plus one router line. Config saves are versioned per strategy and audited in `crypto.config_audit`.
- **UI:** a new nav group "Crypto Trading", separate from the Memecoin "Trading" group.

## 14. Implemented in this branch (2026-10-15): Crypto Trading foundation + Meta Muse

What exists in code, with the evidence for each item:

| Item | Status | Evidence |
|---|---|---|
| Package `packages/crypto-py` (`yonixalpha_crypto`), no import of `yonixalpha_core` | COMPLETED | `tests/test_demo_guard.py::test_the_package_has_no_credentials_signing_or_memecoin_imports` |
| Own schema `crypto` + own Alembic history (`crypto.alembic_version`) | COMPLETED | `tests/test_migrations.py`: upgrade, `alembic check`, downgrade to base, and a probe table in `public` survives |
| Bybit V5 spot adapter (instruments, kline, tickers, time) | COMPLETED (MockTransport) | `tests/test_exchanges.py`; BLOCKED from the server (HTTP 403, Oct 15), so not the default |
| Binance spot adapter, used separately for `binance` and `binance_us` | COMPLETED (MockTransport) | 451 handling and NOTIONAL / MIN_NOTIONAL filters tested; binance.com is BLOCKED from the server (HTTP 451); Binance.US answers HTTP 200 and is the default exchange for Meta Muse |
| Candle hygiene (duplicates, order, forming candle, gaps, stale) | COMPLETED | `test_candle_hygiene_*` |
| Public-only HTTP client; order paths, signed params and key headers refused before sending | COMPLETED | `test_anything_but_public_market_data_is_refused`, `test_a_blocked_request_never_reaches_the_network` |
| Modes DISABLED / DEMO only; executor calls `require_demo` before any fill | COMPLETED | `test_only_demo_executes`, `test_the_executor_refuses_a_non_demo_mode_before_writing_anything` |
| Isolated USDT risk (risk per trade, position, exposure, cash, daily loss, open positions, spread, price age, minimum size, precision) | COMPLETED | `tests/test_risk_demo.py` |
| Demo fill model (ask / bid + slippage, tick rounding against the order, taker fee, top-of-book size limit, idempotent client id) | COMPLETED | `test_fill_model_*`, `test_one_demo_entry_per_closed_candle_and_restart_is_safe` |
| Per-strategy config (versioned, validated, audited, off by default, never re-enabled by a restart) | COMPLETED | `tests/test_config_store.py`, `apps/api/tests/test_crypto_api.py` |
| Engine switch + own emergency stop + platform kill switch (read only) | COMPLETED | `test_kill_switch_and_emergency_stop_block_new_entries`; the kill-switch key matches `yonixalpha_core` (API test) |
| Protection of open demo positions while the strategy or engine is off | COMPLETED | `test_protection_keeps_working_when_the_strategy_and_engine_are_off` |
| Worker `services/crypto-trading` (runs only while the engine is ON or a demo position is open, exits otherwise; 128 MB / 0.25 CPU hard limits; restart on failure only) | COMPLETED | `services/crypto-trading/tests` (exits by itself when idle); compose renders `mem_limit 134217728`, `cpus 0.25`, `restart on-failure`; idle RSS measured at 62 MB |
| API `/api/crypto/*` (login required) | COMPLETED | `test_crypto_api.py` (401 without login, 503 before the schema exists, per-strategy saves, Memecoin settings untouched) |
| UI: nav group "Crypto Trading" > Strategy Lab | COMPLETED | typecheck, lint and a local browser check |
| Meta Muse module (v1) | COMPLETED, DEMO, spot LONG only | `tests/test_meta_muse.py` (EMA = pandas adjust=False, divergence rules, exits, closed candles, defaults = original), plus an end-to-end engine test |
| Read-only exchange check tool | COMPLETED | `python -m yonixalpha_crypto.tools.check_exchanges` |
| Backtests / performance evidence | NOT IMPLEMENTED | no strategy has demo or backtest results yet; the metrics show "unavailable" below 20 closed trades |
| Gold vs BTC, Hyperliquid grid, Confluence | BLOCKED / NOT IMPLEMENTED | as listed in the registry with reasons |

**Meta Muse on spot: a real limitation, not hidden.**
- The original trades USD-M futures both ways. On spot only the LONG half can be simulated; SHORT signals are stored with the reason and not filled.
- A faithful version needs perpetual market data plus a demo short and funding model, for example Bybit linear USDT perpetuals. Bybit is refused from the server (HTTP 403) and Binance USD-M too (HTTP 451), so this stays a later increment with no reachable source today; Hyperliquid perps are a separate venue and would be a separate, clearly labelled integration.

**Memecoin protection check:** `git diff origin/main --stat` shows no change under any of these paths:
- `packages/core-py`
- `services/{data-solana, data-evm, copy-engine, engine-solana-*, decision-engine, ml, paper-trading}`
- `scripts`
- `apps/api/migrations`
- `apps/web/lib`
- existing API route files

The only shared files touched are additive: `apps/api/app/api/router.py` (+2 lines), `apps/api/Dockerfile`, `apps/api/requirements.txt`, `apps/web/app/dashboard/layout.tsx` (one nav group), compose (one new service) and CI (two matrix rows). `.env.example` is not changed.

**Deployment effect (for review):**
- The API and web images change. Nothing in this change alters the Memecoin worker images: `packages/core-py` and their service folders are byte-for-byte unchanged. They can still be rebuilt and recreated for an existing reason, when `deploy.sh` prunes build cache older than 48 h, which happens with or without this change. To avoid even that, deploy only the changed services with the targeted command in the deployment notes.
- The new `crypto-trading` container starts and exits within seconds while the engine is OFF (the default).
- The on/off switch is a row in `crypto.engine_state`, not a line in `.env`. Every Memecoin service loads the whole `.env`, so editing it would change their configuration and make compose recreate them; the database switch avoids that.
- The API creates the `crypto` schema at start. If that fails, the API still starts.
