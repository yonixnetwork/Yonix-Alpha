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
