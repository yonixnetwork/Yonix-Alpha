# Session handoff (2026-10-16)

This file is the handoff from one Claude Code session to the next. It covers
what was done, what runs on the server today, the rules the operator set,
and the next steps. Read it fully before changing anything. Detailed plans
and evidence live in the documents named below; this file does not repeat
them.

## 1. Objectives

**Long-running project.** YonixAlpha: a self-hosted trading platform. Its
main product is the **Memecoin Trading** system (Solana, PAPER mode by
default), with an API, a dashboard and a set of Docker workers on one small
VPS.

**Previous objective (finished and deployed).** The "deep strategy recovery":
- read-only audit: `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md`;
- corrections R1-R10 for the Memecoin system (PR #79);
- a fix to the `recovery_check` report tool (PR #80).

**Current objective.** The operator's prompts were "ADD NEW INDEPENDENT TRADING
STRATEGIES WITHOUT TOUCHING THE EXISTING MEMECOIN SYSTEM" and then the
"YONIXALPHA MASTER DEVELOPMENT PROMPT". They ask for:
- a separate **Crypto Trading** engine for regular crypto, in its own
  dashboard tab ("Crypto Trading > Strategy Lab"), apart from Memecoin;
- primary exchanges Bybit and Binance; initial assets BTC, ETH, SOL;
- **DEMO only**: the only allowed modes are DISABLED and DEMO;
- strategies brought back from the operator's research repos: Meta Muse,
  Gold vs BTC, Hyperliquid grid, Confluence;
- strict protection of the Memecoin system;
- each round: audit first, then the smallest safe unfinished task, then
  prepare deployment for approval;
- the operator's final report uses sections A-G.

## 2. What has been implemented

**Memecoin recovery (PR #79 and #80, merged and deployed).**
- R1-R10 are in `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md` section B3.
- R1 is a settings change. R2-R6 and R9-R10 are code changes. R7 is the RPC
  outage alarm and provider audit. R8 scrubs URL keys from stored records.

**Crypto Trading foundation and Meta Muse v1 (PR #81, merged at `042a627`,
deployed on 2026-10-16):**
- new package `packages/crypto-py` (`yonixalpha_crypto`):
  - own Postgres schema `crypto` with its own Alembic history;
  - public-only exchange adapters for Bybit V5 spot, Binance spot and
    Binance.US spot;
  - candle hygiene, demo-only guard, isolated USDT risk and a demo fill model;
  - per-strategy versioned and audited settings;
  - an engine loop with protection of open demo positions;
  - Meta Muse strategy v1;
- new worker `services/crypto-trading`;
- API routes `/api/crypto/*`;
- dashboard page `/dashboard/crypto` and the "Crypto Trading" nav group;
- read-only tool `python -m yonixalpha_crypto.tools.check_exchanges`;
- plan and audit document `docs/ADVANCED_STRATEGIES_PLAN.md` (sections 1-14).

## 3. What is working (verified on the server, 2026-10-16)

- API, web and crypto-trading were rebuilt and started with a targeted
  deploy. The Memecoin containers were not touched. The API reports healthy.
- **Binance.US** is reachable from the server: HTTP 200.
  - BTC/USDT, ETH/USDT and SOL/USDT are TRADING. Tick is 0.01 for all three;
    steps are 0.00001 / 0.0001 / 0.001; minimum value is 1 USDT.
  - Spreads were 0.03 to 0.94 bps.
- The engine was turned ON and Meta Muse was enabled in DEMO on `binance_us`,
  5m candles, symbols [BTC/USDT, ETH/USDT].
- The worker is **Up**. Every 5 minutes it fetches klines from
  api.binance.us (HTTP 200) and writes a signal row.
  - The first four rows (07:00 to 07:15 UTC) are all `NONE` with the reason
    "no divergence of two strong trends". This is normal.
- Memecoin recovery_check (24 h window ending 2026-10-16 07:23 UTC):
  - R2: 28 refusals;
  - R3: 508 plans with the low-volatility floor;
  - R4/R5: 10 measured fills;
  - R9: back-off is active;
  - R10: default ACCOUNT scope.
- Backups:
  - nightly cron at 03:00 UTC, now with `YONIXALPHA_BACKUP_RETENTION_DAYS=3`;
  - latest verified complete backup:
    `/opt/yonixalpha-backups/yonixalpha-20261016T055353Z.sql.gz` (3.2 GB).

## 4. What is not working, blocked or not implemented

| Item | Status | Reason |
|---|---|---|
| Bybit from the server | BLOCKED | `api.bybit.com` answers HTTP 403. The server is in a US location (DigitalOcean NYC3). The adapter is kept and tested; do not try to get around the refusal. |
| Binance.com from the server | BLOCKED | HTTP 451 "restricted location", on both spot and USD-M futures (fapi). |
| Meta Muse SHORT side | Recorded, not simulated | Spot is long-only, so SHORT signals are stored as REJECTED. The original strategy trades USD-M futures both ways. No reachable perp source exists today: Bybit is 403 and Binance fapi is 451. |
| Gold vs BTC | BLOCKED | Needs a gold source; the operator must choose. Candidates: PAXG, or `xyz:GOLD` on Hyperliquid (not verified). Binance XAUUSDT is refused (451). It must use the original 31 candles. |
| Hyperliquid grid | NOT IMPLEMENTED | Hyperliquid's public API is reachable from the server (BTC, ETH, PAXG listed; the `xyz` perp dex exists). It is a separate venue and must never be shown as Bybit or Binance. |
| Confluence | BLOCKED | No forex data source (the original uses MT5, which is Windows-only). EURUSD and GBPUSD stay blocked. Operator decision: gold perp demo allowed, forex blocked. Default rule = the original proximity rule. |
| Backtests | NOT IMPLEMENTED | Run them locally, never on the VPS. The metrics show "unavailable" below 20 closed demo trades (`metrics.MIN_SAMPLE`). |
| R6 recovery count | NEEDS VERIFICATION | `recovery_check` printed "curve price used after the migration 201". The 24 h window starts before the R6 deploy (PR #79 merged 2026-10-15 11:53 UTC), so these may all be old rows. The hourly split query in section 15 settles it. If counts continue after the paper-trading / decision-engine start time, R6 has a bug. |
| R6 "unavailable 2327" | Known limitation, not a bug | Pool trades are recorded only while a migrated token is evaluated or held. Other horizons are left UNAVAILABLE rather than given a wrong price. |
| Health check probes blocked venues | Minor | Every 10 minutes the worker pings Bybit (403) and Binance.com (451). This is harmless log noise. A proposed improvement is to check only the exchanges used by enabled strategies; it has not been done. |

## 5. Bugs and errors encountered (this phase)

1. **Server disk full during deploy** (`[Errno 28] No space left on device`
   while building the api image).
   - Cause: 14 days of ~3 GB nightly backups (43 GB).
   - Fixed by:
     - `docker builder prune -f` (6.2 GB);
     - deleting the Oct 2-14 backups (the operator ran the command);
     - lowering retention to 3 days in root's crontab.
   - The disk is now about 56% used.
   - One backup, `yonixalpha-20261015T204000Z.sql.gz` (425 MB), is probably
     incomplete. Retention will remove it.
2. **Precedence bug** in the Binance adapter symbol selection. Fixed with
   parentheses.
3. **Static guard test** flagged the words `yonixalpha_core` inside comments.
   It now matches import lines only.
4. **Forming-candle test** built a duplicate timestamp. Fixed with a timedelta.
5. **Tests that assumed Meta Muse could not be enabled** broke once it became
   IMPLEMENTED. Updated `test_config_store` and `test_crypto_api`.
6. **Engine switch first designed as an env var** (`CRYPTO_WORKER_ENABLED`).
   Rejected: editing `.env` recreates every Memecoin container. Moved to the
   DB with exit-when-idle (see section 7).
7. **`recovery_check` counts were misleading.** Fixed in PR #80.
8. **End-to-end test assumed Bybit** as the default exchange. Updated to
   Binance.US when the default changed.

## 6. Fixes attempted and results

- Every fix above was verified by the package test suites and by CI. PR #81
  had 28/28 checks green before the merge.
- The disk cleanup was verified with `df -h` (38 GB free) and a successful
  rebuild.
- The backup was verified: `zcat ... | tail -n 6` shows
  "-- PostgreSQL database dump complete". The trailing `\unrestrict <key>`
  line is normal for newer pg_dump.

## 7. Important decisions

- **Code location:** all new code lives in `packages/crypto-py`, never in
  `packages/core-py`. Every Memecoin image does `COPY packages/core-py`, so
  any core-py change rebuilds and restarts all Memecoin containers.
  `yonixalpha_crypto` must never import `yonixalpha_core`; a static test
  enforces this.
- **Database:** own schema `crypto` and own Alembic version table
  `crypto.alembic_version`. The main Alembic chain (`apps/api/migrations`) is
  untouched.
- **Engine switch in the DB:** the switch is the row `crypto.engine_state`
  (id 1), not `.env`, because every Memecoin service has
  `env_file: ../../.env`.
- **Exit when idle:** the worker exits with status 0 when the engine is
  DISABLED / EMERGENCY_STOP / WAITING_FOR_SCHEMA and no demo position is
  open. Prod compose uses `restart: on-failure`, so it stays stopped until
  `up -d crypto-trading`.
- **Default exchange:** Meta Muse defaults to **binance_us**. Bybit 403 and
  Binance.com 451 were verified from the server.
- **Fees:** labelled "ASSUMED public base tier", with their sources:

  | Exchange | Maker / taker |
  |---|---|
  | bybit | 10 / 10 bps |
  | binance | 10 / 10 bps |
  | binance_us | 0 / 2 bps |

- **Reuse of research code:** the operator's research repos have no licence.
  Rules are re-implemented and no code is copied. Do not fork; the operator
  chose "no new forks".
- **Meta Muse defaults are the original's:** EMA 9/21 (pandas
  `ewm(span, adjust=False)`), trend threshold 0.15%, stop 1%, target 2%,
  150 candles, 5m.
- **Deploy style:** a targeted deploy of only the changed services (see
  section 15). Do not use `scripts/deploy.sh` for Crypto-only changes,
  because it rebuilds and recreates everything.

## 8. Architecture (Crypto Trading)

```
dashboard /dashboard/crypto (apps/web)
   -> /api/crypto/* (apps/api/app/api/routes/crypto.py, login required)
        reads / writes Postgres schema "crypto" via yonixalpha_crypto
        POST /crypto/health-check sets Redis key yx:crypto:health_request
   worker services/crypto-trading (app/main.py -> yonixalpha_crypto.engine.main)
        loop every CRYPTO_LOOP_SECONDS (30 s):
          read crypto.engine_state + strategy_configs
          read the platform kill switch (Redis yonixalpha:kill_switch:engaged, never written)
          protect_positions (stop / target on the bid) in every state
          if ON and not stopped: for each enabled DEMO + IMPLEMENTED strategy,
            once per closed candle: runner.step_strategy
              -> candles (PublicHttp GET only) -> clean -> Signal row (unique per candle)
              -> strategy.evaluate -> risk.size_entry -> demo.open_long / close_long
          health check every 600 s (or on request) -> Redis yx:crypto:health
          heartbeat -> Redis yx:crypto:heartbeat
```

**Engine states:** WAITING_FOR_SCHEMA, KILL_SWITCH, EMERGENCY_STOP, DISABLED,
IDLE, RUNNING. Under memory pressure (RSS above 85% of the container limit)
new entries pause.

**API startup:** the API runs `alembic upgrade head`, then
`python -m yonixalpha_crypto.migrate upgrade`. If the crypto migration fails,
the API still starts and the engine reports WAITING_FOR_SCHEMA.

## 9. Important files and changes

**New in PR #81:**
- `packages/crypto-py/yonixalpha_crypto/`:
  - `__init__.py`: `SCHEMA="crypto"`, `REDIS_PREFIX="yx:crypto:"`.
  - `config.py`: settings from env.
  - `exchanges/profiles.py`: names, URLs, fees and notes. The Bybit note says
    403; the binance note says 451.
  - `exchanges/base.py`: `InstrumentSpec` (round_qty / round_price against
    the order), `TopOfBook`, `MarketData`.
  - `exchanges/bybit.py`, `exchanges/binance.py`: the binance adapter is used
    for both `binance` and `binance_us`.
  - `exchanges/__init__.py`: `market_data()`, `EXCHANGES`.
  - `publichttp.py`: the only HTTP client. GET only, allowlisted public
    paths, refuses signature / key parameters and auth headers before
    sending (`RealOrderBlocked`).
  - `candles.py`: `clean()` drops duplicates, sorts, drops the forming
    candle, detects gaps and refuses stale data. Also `INTERVAL_SECONDS`.
  - `guard.py`: `MODES=("DISABLED","DEMO")`, `require_demo()`.
  - `risk.py`: `RiskLimits` (defaults in the table in section 10),
    `size_entry()`.
  - `demo.py`: fill model, demo accounts (starting balance 1000 USDT),
    `open_long` / `close_long`. Both are idempotent by `client_id`.
  - `db.py`: the ORM tables (section 10) and `schema_ready()`.
  - `migrations/` and `migrate.py`: own Alembic; revision `c0001`.
  - `registry.py`: `StrategySpec` entries.
    - meta_muse: IMPLEMENTED v1, default_exchange `binance_us`.
    - gold_vs_btc and confluence_matrix: BLOCKED.
    - hyperliquid_grid: NOT_IMPLEMENTED.
  - `config_store.py`: `ensure_defaults`, `validate`, `save_strategy` (with
    `expected_version`), `set_engine`, audit rows.
  - `strategies/__init__.py` (Decision, IMPLEMENTATIONS) and
    `strategies/meta_muse.py`.
  - `runner.py`: `step_strategy`, `protect_positions`, `InstrumentCache`.
  - `engine.py`: `Engine.tick`, exit-when-idle, health check, heartbeat.
  - `metrics.py` (MIN_SAMPLE=20), `resources.py`.
  - `tools/check_exchanges.py`.
- `packages/crypto-py/tests/`: conftest (FakeMarket, candles, book, spec)
  plus tests for demo_guard, exchanges, risk_demo, config_store, engine,
  migrations and meta_muse.
- `services/crypto-trading/`: Dockerfile (copies only
  `packages/crypto-py`), `app/main.py`, `tests/test_main.py`.
- `apps/api/app/api/routes/crypto.py` and `apps/api/tests/test_crypto_api.py`.
- `apps/web/app/dashboard/crypto/page.tsx`.

**Shared files changed (additive only):**
- `apps/api/app/api/router.py`: +2 lines (import and include `crypto`).
- `apps/api/Dockerfile`: COPY crypto-py; sed on both package paths; the CMD
  runs the crypto migration step.
- `apps/api/requirements.txt`: `-e ../../packages/crypto-py`.
- `apps/web/app/dashboard/layout.tsx`: one nav group, "Crypto Trading" >
  Strategy Lab, placed after Trading.
- `infra/docker/docker-compose.yml`: service `crypto-trading` (mem_limit
  128m, memswap 128m, cpus 0.25, no profile, depends on postgres / redis /
  api healthy).
- `infra/docker/docker-compose.prod.yml`: `crypto-trading: restart: on-failure`.
- `.github/workflows/ci.yml`: matrix rows for `packages/crypto-py` and
  `services/crypto-trading`.

**Earlier (PRs #79 / #80):**
- `packages/core-py/yonixalpha_core/tools/recovery_check.py` and its test.
- The R2-R10 code locations are listed in the recovery report, section B3.

## 10. APIs, database, environment, integrations

**API (all under `/api`, login required):**
- `GET /crypto/status`: engine, exchanges, strategies, accounts, worker hint.
- `PUT /crypto/engine`: `{enabled?, emergency_stop?, note?}`.
- `PUT /crypto/strategies/{id}`: partial config plus `expected_version`.
  Validated; only an IMPLEMENTED strategy can be enabled.
- `POST /crypto/health-check`.
- `GET /crypto/signals|orders|positions|events|metrics`.

**Database schema `crypto`:**
- `engine_state`: id 1, enabled, emergency_stop, note, updated_at, updated_by.
- `strategy_configs`: strategy_id, enabled, mode, exchange, symbols,
  interval, params, risk, version.
- `config_audit`.
- `signals`: unique `uq_signal_candle` on (strategy, exchange, symbol,
  interval, candle_open_time); side, action, reasons, detail.
- `demo_accounts`: per (exchange, quote), starting_balance, cash.
- `demo_orders`: unique `client_id`.
- `demo_positions`: OPEN / CLOSED, stop / target, P&L.
- `events`.
- `alembic_version`.

**Risk defaults (USDT):**

| Setting | Default |
|---|---|
| risk_per_trade_quote | 5 |
| max_position_quote | 100 |
| max_open_positions | 1 |
| max_daily_loss_quote | 15 |
| max_total_exposure_quote | 200 |

Spread limit, slippage and book age are also in `RiskLimits`.

**Environment variables (all optional):**
- `DATABASE_URL`, `REDIS_URL` (shared).
- `CRYPTO_LOOP_SECONDS` (30), `CRYPTO_HEALTH_EVERY_SECONDS` (600).
- `CRYPTO_{BYBIT,BINANCE,BINANCE_US}_BASE_URL` (overrides).
- No exchange key exists or may be added.

**Redis keys:**
- `yonixalpha:kill_switch:engaged`: read only.
- `yx:crypto:heartbeat`, `yx:crypto:health`, `yx:crypto:health_request`.

**Server:**
- `/opt/yonixalpha`, DigitalOcean NYC3, 2 vCPU, 2 GB RAM, 87 GB disk.
- Memory is tight; the worker's idle RSS is about 62 MB.
- System profile SOLANA_ONLY.
- Compose prefix:
  `docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml`.

## 11. Operator requirements and constraints (keep them)

**Memecoin protection:**
- Do not modify anything pertaining to the existing Memecoin Trading system.
- Do not change shared files if that could alter Memecoin behaviour
  indirectly.
- Do not register new strategy workers inside the Memecoin scheduler.
- Do not route new strategies through Memecoin execution functions.
- Do not enable, disable, restart, reset, reconfigure or redeploy Memecoin as
  part of Crypto work.

**Demo only:**
- Do not enable real exchange orders or real blockchain transactions.
- The demo-only rule must be enforced at the backend execution boundary.
- There must be no hidden live-trading path.
- Do not request, expose or require live trading credentials for public
  market data or demo functionality.

**Honesty:**
- A public repo without a licence is not permission to copy or adapt its
  code.
- Do not claim a fork or integration succeeded unless it actually did.
- Do not fabricate results. A feature is not complete just because a file
  was created.
- Do not claim production success without verification.

**Instruments:**
- Do not substitute XAUUSDT with another instrument without documenting and
  validating the change.
- Keep EURUSD and GBPUSD blocked unless a suitable source is configured.
- Hyperliquid, Forex and gold-vs-BTC are separate integrations. Never present
  them as Bybit or Binance.

**Infrastructure and secrets:**
- Do not upgrade the server or add paid infrastructure or a paid Forex
  provider without approval.
- Never expose credentials or tokens. Never print or commit the private key.
  Never ask for or print the Etherscan key or the X bearer token. Do not
  repeat the admin password.

**Deployment and live trading:**
- Do not deploy untested changes to production, execute live trades, or
  reactivate live auto-entry without explicit approval.
- Do not automatically deploy or restart the Memecoin system.

**Memecoin settings:**
- Keep SOLANA_ONLY.
- Keep BSC, Robinhood and copy trading disabled; preserve their code and data.
- Do not delete trading history or reset the paper account.
- Do not increase sizes, risk limits, slippage or fees to recover losses.
- Never pause execution, position monitoring or risk.

**Database and tests:**
- Migrations must be reversible and backward-compatible. No destructive
  migrations, and no full-table scans on production.
- Do not delete or weaken tests to get green CI. Never test with real
  transactions.

**Repository:**
- Do not change repo visibility or GitHub settings.
- Do not create a PR unless asked. The operator approves merges; once the
  operator said "if it is green merge it", CI green was the condition.

**Workflow:**
- No emojis in the operator-facing text. No model names or IDs in commits,
  PRs or code.
- Run `gitleaks protect --staged --no-banner` before every commit.
- Branch: `claude/yonixalpha-platform-architecture-804nb5`. After a merged
  PR, restart it from `origin/main`.
- Commit trailer:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: <session url>
  ```
  The PR body ends with the Claude Code footer.
- Use simple language. Give single-line copy-paste commands and remind the
  operator to copy only the command boxes. The operator runs all server
  commands and pastes the output back.

## 12. Must NOT be changed or broken

- `packages/core-py` and every Memecoin service folder: `data-solana`,
  `data-evm`, `copy-engine`, `engine-solana-*`, `decision-engine`, `ml`,
  `paper-trading`.
- `scripts/`, `apps/api/migrations`, `apps/web/lib`, and the existing API
  route files.
- `.env` and `.env.example`: no new required keys for Crypto.
- The kill switch key and its semantics. Crypto reads it and never writes it.
- `yonixalpha_crypto` must not import `yonixalpha_core`.
- The DISABLED/DEMO-only modes, `require_demo` before every fill, and the
  `PublicHttp` allowlist.
- Protection of open demo positions in every engine state.
- Before each PR, check the boundary with:
  `git diff origin/main --stat -- packages/core-py services/{data-solana,data-evm,copy-engine,engine-solana-discovery,engine-solana-migration,engine-solana-momentum,decision-engine,ml,paper-trading} scripts apps/api/migrations apps/web/lib`.
  It must be empty.

## 13. Tests performed and results

**Locally, at the final commit:**
- crypto-py: 58 passed (including `test_default_exchange_is_the_one_reachable_from_the_server`).
- crypto-trading: 2 passed.
- API crypto tests: 6 passed.
- ruff: clean.
- Memecoin suites (core, api, all services) were run earlier on this code:
  all passed.

**CI:** PR #81 at `22196c3`: 28/28 checks green (push and PR runs).

**Browser:** the Strategy Lab page was checked locally.

**Server:**
- `check_exchanges --exchange binance_us`: reachable, all three symbols
  TRADING.
- Worker logs: Binance.US klines HTTP 200 every 5 minutes.
- `crypto.signals`: 4 rows, all NONE.
- `recovery_check --hours 24`: see section 3.

**Local test prerequisites:**
- Postgres at
  `postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test`
  and Redis at `redis://localhost:6379/12` (crypto-py). Other suites use
  other Redis DBs.
- Install with `pip install -r requirements-dev.txt` in each package or
  service, then run `python -m pytest -q`.
- For the API suite, unset `ADMIN_PASSWORD_HASH`, `DATABASE_URL`, `REDIS_URL`
  and `JWT_SECRET`.

## 14. Exact current state

- `origin/main` is at `042a627` (merge of PR #81), deployed on the server
  (`git log --oneline -1` there shows `042a627`).
- The branch `claude/yonixalpha-platform-architecture-804nb5` was reset to
  `origin/main`, with only this handoff file added on top.
- Production:
  - Memecoin containers are unchanged and still on the images they had before
    this deploy;
  - api, web and crypto-trading run the new images;
  - the crypto engine is ON, Meta Muse is enabled in DEMO on binance_us, and
    the worker is Up;
  - no demo position had opened at handoff time.
- Disk: 38 GB free after the cleanup (before the rebuild); check `df -h /` again. Backup retention is 3 days.
- Older pending items from previous phases (not started; verify they are
  still wanted before working on them):
  - "Manual BUY: operator overrides preference filters";
  - "EVM live execution (off, locked)";
  - "Screenshot fixes: health crash, perf/ML review timeouts, hide disabled
    UI, fresh/observation, live not trading, paper fee parity".

## 15. Next steps (in order)

1. **Settle the R6 count (read only).** Ask the operator to run the command
   below and paste the output. If every hour with a count is before the
   paper-trading / decision-engine start times, R6 is fine. If not, find
   which code path still writes `source = 'pump_stream trades'` for horizons
   at or after `migrated_at`. Start from `opportunities.merged_series` and
   `solana/pumpswap.covered_price_at`.
   ```
   echo "select date_trunc('hour', o.decided_at) as hour_utc, count(*) as curve_after_migration from opportunity_outcomes o cross join lateral jsonb_each(o.horizons) h join (values ('T+5s',5),('T+10s',10),('T+20s',20),('T+30s',30),('T+60s',60),('T+5m',300),('T+15m',900),('T+30m',1800),('T+60m',3600)) as hs(name,sec) on hs.name = h.key where o.decided_at >= now() - interval '30 hours' and h.value->>'source' = 'pump_stream trades' and o.migrated_at is not null and o.decided_at + make_interval(secs => hs.sec) >= o.migrated_at group by 1 order by 1;" | docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"' ; docker inspect -f '{{.Name}} started {{.State.StartedAt}}' yonixalpha-paper-trading-1 yonixalpha-decision-engine-1
   ```
2. **Monitor Meta Muse in DEMO** (read only). Query `crypto.signals`,
   `crypto.demo_orders` and `crypto.demo_positions`. Check the worker logs
   for errors and that the container stays Up.
3. **Small improvement (optional, Crypto-only).** Run the 10-minute health
   check only for exchanges used by enabled strategies, or let the operator
   mark a venue "blocked from this server", so the worker stops pinging
   Bybit and Binance.com.
4. **Next strategy increment, Hyperliquid grid DEMO.** Hyperliquid is
   reachable from the server. Build it in `packages/crypto-py` as a
   separately labelled venue: public `info` endpoint only, demo fills, own
   tests.
5. **Gold vs BTC.** First ask the operator to choose the gold source (PAXG
   spot on Binance.US if listed, or Hyperliquid PAXG / `xyz:GOLD` after
   verification). Use 31 candles as in the original.
6. **Confluence.** Use the gold perp, demo only, with the original proximity
   rule as the default. Forex stays blocked.
7. **Backtests.** Run them locally on downloaded public candles, never on the
   VPS. Report them separately from demo results.
8. **Deploy procedure** (after the operator approves a merged PR). Back up,
   pull, then rebuild and start only the changed Crypto services:
   ```
   cd /opt/yonixalpha && scripts/backup-db.sh
   cd /opt/yonixalpha && git pull --ff-only && git log --oneline -1
   cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml build api web crypto-trading && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --no-deps api web crypto-trading
   ```
   Check the disk with `df -h /` before any build.
9. **Operator reminder:** rotate the Chainstack key, which was exposed in an
   earlier phase (R8).
