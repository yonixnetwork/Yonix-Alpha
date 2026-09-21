# YonixAlpha — Architecture Audit (Phase 0)

Date: 2026-09-21
Scope: `yonixnetwork/yonix-alpha` (target repo) + 5 approved reference repositories.

## 1. Target repo state

`yonixnetwork/yonix-alpha` is **completely empty** — no commits, no branches, on GitHub or
locally. This is a greenfield build. There is nothing to preserve or migrate in the target
repo itself; all "preserve working code, refactor incrementally" guidance applies to the five
reference repos below, whose useful *patterns* (not verbatim code, in most cases) will be
ported into the new codebase.

## 2. Reference repositories audited

Each repo was cloned read-only, its full source read, dependencies inspected, `git log`
reviewed, and grepped for hardcoded secrets / private keys / suspicious network or exec calls.
No repo contained committed secrets, obfuscated code, telemetry beacons, or remote
code-execution vectors. All five are small, single-purpose, single/few-commit personal or
tutorial-style projects — **none are production-grade services**. None have a LICENSE file
despite two READMEs claiming MIT; treat all five as unlicensed until confirmed with the
originating org, and prefer re-implementing small pieces from scratch over copying files
verbatim where license status is unclear.

### 2.1 `hyperliquid-grid-trading-bot`
Single-asset grid bot on Hyperliquid perpetuals via the official `hyperliquid-python-sdk`.
REST-only (WebSocket explicitly disabled — `Info(skip_ws=True)`). Functionally complete for
its narrow scope: grid construction, fill detection via REST polling/diffing, weighted-average
P&L accounting, drawdown + range-break circuit breakers, flatten-on-pause. Clean secret
handling (API-wallet-only private key, `.env` gitignored, nothing in git history). Gaps: no
restart-time reconciliation against pre-existing exchange orders (risk of duplicate grids on
restart), no backoff on sustained REST failure, no tests, no LICENSE file.
**Verdict: pattern reference for risk engine + position accounting; not directly portable
(Hyperliquid-specific, no WS, single-asset).**

### 2.2 `confluence-matrix-forex`
MT5 (MetaTrader5, Windows-IPC) forex breakout bot with a confluence-score entry gate. Fully
implemented (not a stub), backtested with a real event-loop backtester and R-multiple metrics,
look-ahead-safe (pivot confirmation is lag-shifted). Fork of an upstream project with three
documented bug fixes, covered by unit tests against a stubbed MT5 module. No secrets in repo
or history; no dangerous code execution. Its indicator/strategy logic (RSI, ATR, forex market
structure) is explicitly out of scope for crypto reuse per the project brief, but its
**risk/position-management scaffolding is the strongest generic reference of the five repos**:
a durable idempotent position-state machine (`position_state.py`), fixed-%-risk lot sizing
(`calc_lot_size`), a partial-close + move-to-breakeven two-step sequence with a guard-before-act
idempotency pattern (has one bug worth fixing on port: SL-move failure after a successful
partial close still marks the step "done"), and R-multiple-based backtest metrics.
**Verdict: no portable strategy logic, but the best reference for the shared Risk Engine /
Position Manager's state machine and sizing formula.**

### 2.3 `solana-token-scanner`
A ~110-line tutorial script (not the production scanner the name implies) that subscribes to
PumpPortal's free public WebSocket (`subscribeNewToken`) and prints new pump.fun mint events to
a terminal. No database, no analytics, no migration detection, no holder/wallet analysis, no
REST API integrations, no wallet/key handling at all (nothing to secure). README describes a
TypeScript version and a scam-filter roadmap that **do not exist in the repo** — treat the
README as aspirational, not descriptive.
**Verdict: minimal WebSocket-connect/reconnect idiom and PumpPortal event-field reference only.
Engines A, B, and C must be built from scratch — this repo does not cover migration detection
or existing-token momentum at all.**

### 2.4 `meta-muse-crossover-strategy`
A ccxt-based (not Binance's official SDK) BTC/ETH inverse-divergence futures bot, exchange-
selectable between Binance and Bybit. REST-polling only (60s interval) — **no WebSocket usage
whatsoever**, despite being the designated primary reference for the Binance Futures engine.
Functionally complete signal/execution logic (9/21 EMA divergence, real order placement, SL/TP
via exchange-native protective orders) but strategy/execution are not separated into distinct
layers, no client-order-ID idempotency, no persistence, no PnL/liquidation-price calculation,
no backtest validation of the strategy's edge. Clean secret handling. Useful defensive patterns:
re-deriving position truth from REST every tick rather than trusting cache, and re-verifying
protective (SL/TP) orders exist on the exchange every cycle rather than assuming local state is
correct.
**Verdict: the ccxt/execution layer should be *replaced*, not reused — YonixAlpha's Binance
engine should be built on Binance's official futures connector SDK directly, using only this
repo's reconciliation and protective-order-verification patterns.**

### 2.5 `solana-sniper-jupiter-swap-api`
A 307-line single-shot Python CLI implementing the modern Jupiter Swap API v1 flow: quote →
build (with dynamic compute-unit limit, dynamic slippage, capped priority fee) → sign
(solders) → send → confirm. Private key handling verified clean (env-var only, never logged,
never sent anywhere, no leak in git history). Confirms the architectural risk flagged in the
project brief: **it has zero bonding-curve-native execution capability** — it assumes every
token is Jupiter-routable and will simply fail with a generic exception on a pre-migration
token. `skip_preflight=True` and no idempotency/replay protection.
**Verdict: solid reference for the post-migration Jupiter/DEX execution adapter's quote and
transaction-building logic. Confirms a separate, purpose-built bonding-curve executor (direct
pump.fun program interaction) is mandatory — no reference repo provides this.**

## 3. Cross-cutting findings

- **No repo has a production data layer.** None use PostgreSQL; state is in-memory, JSON files,
  or absent. YonixAlpha's persistence layer (Postgres + Redis, migrations, the full schema in
  the spec) must be built from scratch.
- **No repo has WebSocket-based market/account data**, except the token-scanner's read-only
  PumpPortal subscription. Full WS infrastructure (Solana program-log subscriptions, Binance
  market + user-data streams, reconnection, resubscription, backoff) must be built new.
- **No repo separates strategy from execution cleanly.** Every repo mixes signal computation
  and order placement in one class/module. YonixAlpha's execution-provider abstraction
  (`ExecutionProvider` → `SolanaBondingCurveExecutor` / `JupiterExecutor` /
  `BinanceFuturesExecutor`) has no existing implementation to lift — design it fresh, informed
  by the adapter "shape" (thin wrapper, typed order-info, defensive error handling) seen in the
  Hyperliquid client.
- **Idempotency is the single most common gap.** None of the five repos use client order IDs or
  crash-safe "submitted but unconfirmed" reconciliation. This must be designed centrally in the
  new Execution Engine / Position Manager rather than inherited from any reference repo.
- **Recurring good pattern worth generalizing**: "always re-derive truth from the venue via
  REST/on-chain state rather than trusting local cache," seen independently in the Hyperliquid
  bot's order-diff reconciliation and the meta-muse bot's per-tick position/protective-order
  re-verification. This should become a first-class principle of YonixAlpha's state machine
  (Section 10 of the spec): reconstruct state from Postgres + live exchange/on-chain state after
  restart, never from RAM alone.
- **Secrets hygiene was uniformly good** across all five repos (env-var-only key loading, no
  secrets in git history, no logging of private keys). No repo needs remediation on this front,
  but none demonstrate the *additional* production controls YonixAlpha requires (secret
  rotation, KMS/vault integration, audit logging of credential use) — those must be designed new.
- **License risk**: two repos' READMEs claim MIT with no LICENSE file present anywhere; three
  have no license claim at all. Do not copy files verbatim into `yonix-alpha` without
  confirming licensing with the `yonixnetwork` org owner — prefer re-implementing the small
  amount of genuinely reusable logic (a few hundred lines per repo, per the audits above).

## 4. Missing infrastructure (must be built new, no reference exists)

- PostgreSQL schema + migrations (trades, orders, positions, tokens, wallets, model_versions, etc.)
- Redis-backed live state / caching / locks / queue coordination
- Migration/graduation detection (Engine B) — entirely unimplemented in any reference repo
- Existing-token momentum + volume-quality scoring (Engine C) — entirely unimplemented
- Bonding-curve-native execution adapter (pump.fun or equivalent program interaction)
- Binance WebSocket market + user-data streams with reconnection/state reconciliation
- Feature engine, ML training/inference/registry pipeline, backtesting framework with realistic
  fees/slippage/latency
- Web dashboard, authentication/JWT, audit logging, Telegram integration, admin controls
- Docker Compose multi-service deployment, reverse proxy + HTTPS, CI/CD

## 5. Proposed directory structure

```
yonix-alpha/
├── apps/
│   ├── api/                    # FastAPI backend
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── api/            # routers: auth, dashboard, tokens, scanner, positions,
│   │   │   │                   # orders, trades, strategies, risk, models, settings, system, health
│   │   │   ├── auth/
│   │   │   ├── ws/              # websocket gateway (position.updated, trade.created, ...)
│   │   │   ├── config/          # RiskConfig, StrategyConfig, SolanaConfig, BinanceConfig, ...
│   │   │   └── db/              # SQLAlchemy models, migrations (alembic)
│   │   └── tests/
│   └── web/                    # Next.js/React/TypeScript dashboard
│       ├── app/
│       └── components/
├── services/
│   ├── engine-solana-discovery/    # Engine A — new token discovery
│   ├── engine-solana-migration/    # Engine B — migration/graduation sniper
│   ├── engine-solana-momentum/     # Engine C — existing-token momentum
│   ├── engine-binance-futures/     # Binance USDT-M futures engine
│   ├── decision/                   # shared: feature engine, risk engine, signal engine,
│   │                                # decision engine, state machine, execution router
│   ├── execution/                  # ExecutionProvider + adapters:
│   │   ├── bonding_curve_executor/
│   │   ├── jupiter_executor/
│   │   └── binance_futures_executor/
│   ├── ml/                         # training / inference / model registry / validation
│   └── notifier/                   # Telegram alerts
├── packages/                       # shared libraries across services (Python + TS)
│   ├── core-py/                    # shared Python: config, logging, db clients, schemas
│   └── core-ts/                    # shared TS types for frontend/backend contract
├── infra/
│   ├── docker/
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.dev.yml
│   │   └── docker-compose.prod.yml
│   ├── nginx/ (or caddy/)
│   └── migrations/                 # DB migrations (if not colocated with apps/api)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ARCHITECTURE_AUDIT.md        # this file
│   ├── REUSE_MATRIX.md
│   ├── DEPLOYMENT.md
│   ├── SECURITY.md
│   ├── ENVIRONMENT.md
│   ├── DATABASE.md
│   ├── API.md
│   ├── TRADING_ENGINE.md
│   ├── ML.md
│   ├── TESTING.md
│   └── OPERATIONS.md
├── .env.example
├── .gitignore
└── README.md
```

Rationale: services are split by trading engine (matching the spec's explicit requirement that
each Solana engine be independent) with a shared `decision/` and `execution/` layer so no
engine invents its own risk logic or duplicates exchange-specific code inside strategy code —
directly addressing the cross-cutting gap (strategy/execution not separated) found in every
reference repo.

## 6. Next steps (Phase 1+)

See `REUSE_MATRIX.md` for the component-level reuse decisions. Phase 1 (Foundation) begins
with: repo scaffolding per the structure above, `.env.example`, structured logging, Postgres +
Redis + migrations, Docker Compose, health checks, authentication, base API, frontend shell.
