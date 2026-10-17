# Review of PR 82 before deployment (2026-10-16)

Scope: everything this branch changes compared with `main` (042a627).
Not merged, not deployed, no production change, live trading still locked
(`LIVE_ORDERS_UNLOCKED = False` in `packages/crypto-py/yonixalpha_crypto/private/livelock.py`).

**No profitability claim is made.** No backtest has run on real market data
yet. The sandbox where this review ran cannot reach any exchange. Every
number in the tests comes from synthetic candles built to exercise a rule.

## 1. What was reviewed (the actual diff)

Before this review: 74 files, +11,340 / -264 lines compared with `origin/main`.
This review adds one commit (section 4).

| Area | Files | Notes |
|---|---|---|
| Crypto engine package | 46 files under `packages/crypto-py` | strategies, backtest, risk, portfolio, modes, private (keys / orders), migrations c0002-c0004, tests |
| API | 3 new route files (`crypto_center.py`, `crypto_connections.py`, `crypto_execution.py`), 3 new test files | `router.py`: 6 added lines (3 imports, 3 `include_router`); `crypto.py`: docstring only |
| Web | 8 new pages under `app/dashboard/crypto/`, `EquityChart.tsx`, `StrategyUi.tsx`, `archivedEngines.ts`, `archived/page.tsx` | shared: `layout.tsx` (menu), `page.tsx` (redirect), `globals.css` (additions only: 0 lines removed) |
| Memecoin overview | `dashboard/memecoin/page.tsx` | the old `dashboard/page.tsx`, moved; `diff` shows one changed line: the title "Dashboard" became "Memecoin Overview" |
| Docs | 3 files | plan, handoff, strategy report |

Protected paths (`packages/core-py`, every Memecoin service, `scripts`,
`apps/api/migrations`, `apps/web/lib`, `.env.example`, infra): **no changes**
(`git diff origin/main --stat -- <those paths>` is empty).

## 2. Findings

Severity: High = can break a risk limit or lose control of a position;
Medium = wrong or misleading result or behavior; Low = small accuracy or
efficiency issue. Every fix below has a test that fails on the old code and
passes on the new code, except F6, which is a new report field.

| # | Severity | Finding | Status |
|---|---|---|---|
| F1 | **High** | **Exchange-demo trades skipped limits on their real size.** The executor sizes the order again from the practice account's own balance. Portfolio and per-strategy limits were checked only against the paper-account estimate. The strategy's own exposure, open-trade count and daily loss were never counted for exchange trades. Example (now a test): a 200 USDT strategy exposure limit let a 300 USDT practice order through. | Fixed: `Executor.open_long(pre_trade_check=...)` runs the portfolio check and the strategy's exchange-trade limits on the final size, before anything is recorded or sent. Exchange-trade losses now count toward the cooldown. |
| F9 | **High** | **Disabling abandoned exit rules.** When a strategy was disabled, or the engine was off, in emergency stop or under the kill switch, its exit rule stopped running for positions it still held (for example the "close below SMA" exit). Stop, target, trailing, partials and the time stop kept working. | Fixed: the engine evaluates any strategy that still holds a position in **exits only** mode, once per closed candle. Entry signals are recorded as refused ("exits only: strategy disabled" or "exits only: engine ..."). Tested for strategy disabled, engine off and emergency stop. |
| F10 | **Medium-High** | **Backtests used different defaults from the settings page.** Shared defaults overrode a module's own defaults in the backtester. Affected: `sma_cross` stop 2 ATR instead of 2.5; `trend_pullback` and `rsi_reversion` stop 2 instead of 3; `sr_breakout` and `breakout_retest` ran with **no** 2R target. The registry, settings page and live adapter showed the module's values, so a backtest did not test what would be traded. | Fixed (merge order in `Module.p`). Test: for all 20 modules, backtest defaults equal the registry defaults. Parameter lines in `STRATEGY_LIBRARY_REPORT.md` regenerated from code (5 lines changed). |
| F11 | Medium | **Settings could detach a strategy from its open trade.** While a strategy held a trade, changing its exchange, symbols, paper/exchange-demo mode or connection meant the engine no longer found that trade, so its exit rule stopped running (protection still ran). | Fixed: those changes are refused while a position is open, with a clear message. Disabling the strategy and changing risk, exits or parameters stay allowed. |
| F2 | Medium | **One symbol decided a strategy's verdict.** The Strategy Center status and verdict came from one arbitrary "latest" run. A strategy that passed on BTC but failed on ETH could show "approved for consideration". | Fixed: the verdict is the **worst** finished run of the strategy's latest batch. Approval needs every symbol to be a candidate with none failed or queued. The page shows the counts, for example "worst of 3: ETH/USDT 1h (2 candidate, 1 failed)". |
| F3 | Medium | Backtests always used the published defaults, so saved parameters could not be tested before paper trading them. | Fixed: a batch can use "saved settings" (`param_source`). Each run records the settings version. The verdict then warns that saved parameters chosen after seeing earlier out-of-sample results are not independent evidence. |
| F7 | Medium | **`sr_breakout` exit drifted after entry.** Its "failed breakout" exit used the newest swing high, not the level that was broken. After a rally formed a higher pivot, it became an undocumented trailing exit. | Fixed: the exit is anchored to the level broken at the most recent entry signal. Module version 2. |
| F4 | Low-Medium | **Partial fills used future volume.** The volume cap used the fill bar's volume, which is not known when the order is placed. | Fixed: the cap uses the signal bar's volume. |
| F5 | Low-Medium | **History was downloaded again on every run.** When the exchange itself has gaps, the stored range stays below 98% complete. Each of a batch's 58 runs would then download about 17,500 candles again. | Fixed: a range is not fetched again within 1 hour. The run records why. |
| F6 | Low | **Data gaps were stored but not shown.** | Fixed: coverage and gap counts are recorded in each run, its summary and the verdict reasons. Below 98% coverage a "candidate" is lowered to "weak". |
| F8 | Info | **Two pairs of modules overlap.** `bollinger_reversion` and `zscore_reversion` use the same core trigger: a close back above the mean minus k standard deviations. They differ in window (20 vs 50), filter (200-bar trend vs ADX < 25) and exit. `regime_switch` reuses the Donchian and Bollinger rules, chosen by ADX. | Documented, not changed. The Compare page's correlation and overlap will show how similar their trades are once real results exist. |

## 3. The specific questions

- **Distinct and correct entry and exit rules (20 modules):**
  - Each module has its own rule.
  - Each is tested on a scenario built for it, plus a no-trade case.
  - No-look-ahead is tested for every module and indicator: decisions on data cut at each bar must equal decisions on the full data.
  - The live adapter decides like the backtester.
  - Issues found: F7 (fixed), F10 (fixed), F8 (documented overlap).
- **Costs in every metric:**
  - Entry and exit fees, half the spread and slippage are applied on every fill.
  - Funding applies when it is set (spot: 0).
  - The entry fee is part of each trade's profit or loss.
  - A test checks that the equity change equals the sum of trade P&L, and that a stop-out loses exactly the risk budget, fees included.
  - Gaps through a stop fill at the open, worse than the stop.
- **Out-of-sample data cannot leak into parameter selection:**
  - The grid is chosen on the training period only.
  - Sensitivity uses training plus validation.
  - Each walk-forward fold picks parameters from data before that fold.
  - The verdict uses the fixed-parameter out-of-sample run.
  - Indicators are causal.
  - The remaining human risk (choosing saved parameters after looking at earlier results) is now flagged on the run (F3).
- **Concurrent strategies and global limits:**
  - The engine runs strategies one after another in a single worker, and commits each entry before evaluating the next strategy.
  - New test: two strategies signal on the same candle with a 1-position portfolio limit. One opens; the other is refused with the portfolio reason.
  - Exchange-demo entries now also pass the limits on their real size (F1).
  - Prerequisite: run only **one** crypto-trading worker. Paper entries are not locked across processes.
- **Position sizing:**
  - Sizing uses the budget divided by the loss per unit, where the loss per unit includes entry and exit slippage and fees on both legs.
  - The size is then capped by the maximum position, the strategy's exposure room, cash and exchange minimums.
  - A size below the exchange minimum is refused, not rounded up.
  - Percent-of-equity mode uses the account the order is sent to (paper or practice). The limits are then checked again on that size (F1).
- **Partial fills:**
  - Backtest: capped by volume (F4) and flagged on the trade.
  - Paper: capped by the top-of-book size.
  - Exchange: the filled quantity is what gets protected.
- **Missing data:**
  - Live: stale, misaligned or too few candles produce a refused signal with the reason.
  - Backtest: gaps are recorded, shown and lower a "candidate" (F5, F6).
  - A symbol with no data fails its run with the error; the rest of the queue continues.
- **Open positions and exposure:**
  - The portfolio counts paper and exchange-demo positions together: risk, exposure in total and per coin (three BTC strategies count as one BTC exposure), daily loss, and drawdown (which sets the emergency stop).
  - Protection keeps running in every engine state.
- **Settings persist:**
  - Settings are stored in Postgres (`crypto.strategy_configs`), one row per strategy, versioned (a stale save is refused) and audited (`crypto.config_audit`).
  - A save never touches another strategy (tested).
  - Portfolio limits are stored and versioned the same way.
- **Disabling a strategy:**
  - New entries stop.
  - Stop, target, trailing, partials, the time stop and now the strategy's own exit rule keep running until the position is closed (F9).
  - Settings that would detach a held position are refused (F11).
- **Memecoin unchanged:**
  - No file under the Memecoin packages, services, scripts, migrations, `apps/web/lib` or infra changed.
  - Shared web files only gained new menu items and CSS.
  - The Memecoin overview page moved, with one changed line (its title).
  - Every Memecoin test suite passes (section 5).
  - The crypto code has its own Postgres schema and its own migration chain, and never imports `yonixalpha_core` (enforced by a test).

## 4. Fixes in this review (one commit)

Code: `private/executor.py`, `private/service.py`, `runner.py`, `engine.py`,
`config_store.py`, `strategies/library.py`, `backtest/{engine,data,jobs,worker}.py`,
`apps/api/app/api/routes/crypto_center.py`, `apps/web/app/dashboard/crypto/{center,backtests}/page.tsx`.

New or changed tests:
- `test_portfolio_modes.py`: F1 (strategy limit and portfolio limit), two strategies against the global limit, F9 (three cases plus a refused entry), F11.
- `test_backtest.py`: F4 (two tests), F5, F6.
- `test_library.py`: F7; F10 (20 cases).
- `test_crypto_center_api.py`: F2, F3.

The existing exchange-demo test was itself an example of F1: its strategy had
a 200 USDT exposure limit and sent a 300 USDT order. Its limit is now 300.

## 5. Test results (run in this session, after the fixes)

| Command | Result |
|---|---|
| `packages/crypto-py`: `ruff check yonixalpha_crypto tests` | clean |
| `packages/crypto-py`: `python -m pytest -q` | **251 passed** (212 before the review; 244 after the fixes; 251 with the local runner) |
| `services/crypto-trading`: `python -m pytest -q` | 2 passed |
| `apps/api`: `ruff check app tests` and the full `python -m pytest -q` (Memecoin routes included) | clean; **211 passed** (209 before the review + 2 new) |
| `apps/web`: `npx tsc --noEmit`, `npx next lint`, `npx next build` | clean |
| Memecoin: `packages/core-py` | 1073 passed |
| Memecoin services: copy-engine 18, data-evm 14, data-solana 11, decision-engine 79, engine-solana-discovery 33, engine-solana-migration 11, engine-solana-momentum 11, ml 50, paper-trading 88 | all passed |
| The new tests run against the OLD code | they fail (F1, F9: 6 failed; F10: 5 failed; F7: 1 failed), proving each tests its defect |
| `gitleaks protect --staged` | no leaks |

Not tested here: real exchange hosts (blocked from this sandbox), a real
practice account, and real historical candles.

## 6. Production resources: read-only commands (to run on the server)

Nothing below changes anything. Run each one, then paste the output.

1. Machine: `cd /opt/yonixalpha && nproc && free -m && df -h / && uptime`
2. Containers: `docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"`
3. Docker disk: `docker system df`
4. Database size: `cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select pg_size_pretty(pg_database_size(current_database()))"'`
5. Binance.US reachability for candles (public, no key): `curl -s -o /dev/null -w "%{http_code}\n" "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1"`

### Measured on the server (2026-10-17, read-only, run by the operator)

| Item | Value | Reading |
|---|---|---|
| CPU | 2 cores, load average about 1.1 | about half busy |
| Memory | 1,967 MB total, **464 MB available**, swap **1,032 of 2,047 MB used** | the machine already swaps: deploy at a quiet time |
| Disk `/` | 87 GB, 35 GB free (61%) | enough for a full backup (about 3 GB) and the image builds |
| Docker | images 4.2 GB (1.1 GB reclaimable), volumes 26.3 GB, build cache 3.4 GB (0.7 GB reclaimable) | nothing to clean for this change |
| Postgres database | **24 GB** (almost all Memecoin data) | a full dump is large; the crypto schema alone is small |
| crypto-trading container | 54 MB of its 128 MB limit | running now |
| Largest containers | postgres 362 MB, redis 222 MB, engine-solana-discovery 112 MB, decision-engine 89 MB, paper-trading 84 MB | the Memecoin workers take most of the memory |
| Binance.US public candles | HTTP 200 | live paper trading has its data |

Conclusions:
- **The tight resource is memory.** The machine already uses 1 GB of swap. This supports the operator's decision (2026-10-17): **no historical backtests run on this server**.
- **The heaviest deploy step is building the web image** (`next build`, several hundred MB for a short time). Prefer a quiet period, and check `free -m` first. Build the three images one at a time.
- **Backups:** the nightly script dumps the whole database (24 GB live, about 3 GB compressed). 35 GB is free, so a pre-deploy backup fits.

## 7. Backtests: local only (decision 2026-10-17)

The operator decided that historical backtests run locally, on downloaded public candles, **never on the production VPS**. Runbook: `docs/LOCAL_BACKTESTS.md`.

- **Code** (`backtest/policy.py`):
  - `SERVER_BACKTESTS_ENABLED = False`: the queue worker refuses to start, the API refuses to queue a batch (409), and the "backtests" switch cannot be turned on.
  - `RESULT_IMPORT_ENABLED = False`: a results bundle can be imported only into a localhost database until the workflow is verified.
- **Dashboard:** the Backtests page has no "New batch" form. It explains the policy and shows only stored results (none yet). The server-backtest switches on Portfolio Risk are read-only.
- **Local runner** (`python -m yonixalpha_crypto.backtest.local`):
  - commands: `fetch` (public API) or `convert` (data.binance.vision files), `plan`, `run`, `verify`, `import`;
  - every bundle and report carries data provenance, fees, spread, slippage, the configuration, the code version and git commit, and every run's train / validation / out-of-sample periods;
  - the same code and data give the same `content_crc`, and `verify` re-runs a bundle to prove it.
- **Workload on a normal computer** (measured on synthetic candles of the real size):
  - about 15 s per run for 17,520 hourly candles with the grid on, under 100 MB;
  - the 58-run first batch takes about 15 minutes on one core.
- **Shared code:** the 20-strategy registry, metrics and dashboard are unchanged. The worker and the runner share one run function (`backtest/run.py`).

## 8. Deployment prerequisites (all need your approval first)

1. Your separate approval to merge and to deploy.
2. The outputs of section 6 (received 2026-10-17): enough disk; deploy at a quiet time because of memory.
3. A database backup before deploying (the existing backup procedure in the handoff).
4. A targeted deploy of api, web and crypto-trading only. Do not restart the Memecoin workers.
5. At API start, migrations c0002-c0004 run automatically on the `crypto` schema only (all additive).
6. After deploy, check:
   - the API health endpoint;
   - Strategy Center lists 20 modules, all "backtest pending";
   - the Memecoin pages still load from Archived Engines;
   - `LIVE_ORDERS_UNLOCKED` is still `False`.
7. Leave the engine switched off until you choose to start it. Backtests are never run or scheduled on the server.
8. Run only one crypto-trading worker.

## 9. Rollback

1. Turn off the Crypto Trading engine on the dashboard. Paper and practice positions keep their protection.
2. **Code rollback (enough on its own):** redeploy the previous `main` commit (042a627) for api, web and crypto-trading only.
   - The database changes are additive, and the old code works with the extra tables and columns.
   - The old API's start-up step `python -m yonixalpha_crypto.migrate upgrade` will log "crypto schema migration failed" because it does not know c0002-c0004. The API still starts (the Dockerfile tolerates that step failing).
   - The Memecoin overview returns to `/dashboard`.
3. **Optional, to remove the new tables:**
   - Do this BEFORE step 2, while the new code is still running.
   - First export any backtest results you want to keep.
   - Then run: `cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T api python -m yonixalpha_crypto.migrate downgrade c0001`
   - This reverses c0004, c0003 and c0002, dropping the candle store, backtests, portfolio settings, exchange connections and execution tables.
   - It refuses nothing, so check first that no exchange-demo trade is open.
   - Use `downgrade c0003` to remove only the backtest and portfolio tables.
   - The Memecoin schema is never touched.
4. The upgrade, check, downgrade and upgrade cycle is tested in `tests/test_migrations.py`.

## 10. Still open

- **No real results:** no real backtest, paper or exchange-demo results exist yet.
- **Outputs still needed from you:**
  - the R6 count command;
  - the practice-host reachability check;
  - (the server resources arrived on 2026-10-17; see section 6).
- **Exchange demo** needs:
  - the server to reach the practice hosts;
  - a practice key entered in the app (never in chat).
- **Data access from the US server:**
  - Bybit and Binance.com public data are refused (403 / 451).
  - Paper trading uses Binance.US public data. Backtests run locally, on whatever public source is reachable there (`docs/LOCAL_BACKTESTS.md`).
- **Live trading:** stays locked until you review and authorize it in writing.
