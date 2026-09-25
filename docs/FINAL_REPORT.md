# Final Report — Control Center Specification (§97)

Branch `claude/yonixalpha-platform-architecture-804nb5`. This covers both implementation
passes: the first (safety gate, Solana pipeline, control API/UI; deployed to main at
`3692ef7`) and the second (everything after it: SHORT + order books, venues, strategies,
realtime, ML champion/challenger, full dashboard).

Verification states follow the spec's vocabulary: **VERIFIED**, **PARTIALLY VERIFIED**,
**IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION**, **NOT VERIFIED**, plus **BLOCKED** and
**NOT IMPLEMENTED** where those are the truth.

**The one limitation behind most "NOT VERIFIED" rows:** the build environment could not
reach any exchange or Solana endpoint (every request returned a proxy 403). All live-data
behaviour has to be confirmed on the droplet: §31 step 4 and
`python -m yonixalpha_core.tools.verify_live`.

Safety state at hand-off: `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false`,
`PAPER_TRADING=true`. No real order, swap or wallet transaction was made at any point, and
no code path in this branch can place one.

---

## 1. Existing architecture discovered

FastAPI API (`apps/api`), Next.js 15 dashboard (`apps/web`), and a shared Python package
(`packages/core-py`: config, DB models, risk, kill switch, ML registry). Nine services:
`data-solana`, `data-binance`, `engine-solana-{discovery,migration,momentum}`,
`engine-binance-futures`, `decision-engine`, `ml`, `paper-trading`. Runtime: Postgres 16,
Redis (AOF), nginx with Let's Encrypt, Docker Compose on one 2 GB DigitalOcean droplet
(`yonixalpha.com`). The full map is in `docs/FINAL_AUDIT_ARCHITECTURE.md` and
`docs/IMPLEMENTATION_MATRIX.md` §2.

## 2. Existing features preserved

JWT auth with refresh rotation, lockout and nginx rate limit; kill switch (Redis + AOF);
`engine-binance-futures` (signed REST, idempotent orders, reconciliation — untouched);
`execution_router` (Solana stays UNSUPPORTED); `data-binance`; the original ML pipeline and
its leakage/significance gates; audit log; Telegram alerting; deployment scripts, TLS,
certbot, CI. Every existing API route and page still works. Old pages were restyled or
extended, not replaced, except Overview, Strategies and Paper, which were rebuilt on the
same endpoints plus new ones.

## 3. Existing broken features (found, and what happened to them)

| Found | Resolution |
|---|---|
| `engine-solana-discovery` / `-momentum` subscribed to the whole SPL Token program (unsustainable on 2 GB / free RPC) | replaced by the pump.fun-scoped stream; momentum and migration engines moved to the `legacy` compose profile |
| `engine-solana-migration` had no parsers (detected nothing) | replaced by bonding-curve `complete` flag + migration event decoding |
| decision-engine could never reach an entry (confidence cap, no price feed) | replaced by the safety gate with curve/book/quote pricing |
| paper-trading had no Solana entry, no fees, no partial TPs | rewritten `paper_engine` (curve/book fills, fees, TPs, trailing, SHORT) |
| RPC URLs with API keys logged / stored / sent to Telegram; Telegram token in error logs | redacted (`redact.py`); **rotate any key configured before the first deploy** |
| Dashboard polled; no mobile nav; no settings pages | realtime WebSocket, collapsible/mobile shell, all pages in §20 |

## 4. Research performed

Official program/API documentation and official SDK source were read over GitHub raw and
PyPI, the only external sources the build environment could reach. Where only third-party
evidence existed, it is marked as such. Details: `IMPLEMENTATION_MATRIX.md` §1.

## 5. Official APIs reviewed

- pump.fun program + PumpSwap IDLs (`pump-fun/pump-public-docs`): accounts, events, discriminators.
- Solana JSON-RPC / `jsonParsed` token shapes (`anza-xyz/agave` account-decoder).
- Jupiter Swap API v1 (`jup-ag/jupiter-quote-api-node` OpenAPI).
- Binance USDⓈ-M futures REST: klines, depth, premiumIndex, openInterest (via `binance-futures-connector` source).
- Bybit V5: market kline/orderbook/tickers/recent-trade/funding/open-interest, account wallet-balance, position list, open orders, executions, closed PnL, HMAC-SHA256 signing (via `pybit` 5.17 source).
- Hyperliquid info API: allMids, l2Book, metaAndAssetCtxs, candleSnapshot, clearinghouseState, openOrders, userFills (via `hyperliquid-python-sdk` 0.24 source).

## 6. GitHub repositories reviewed

The user's own: `hyperliquid-grid-trading-bot`, `confluence-matrix-forex`,
`solana-token-scanner`, `meta-muse-crossover-strategy`, `solana-sniper-jupiter-swap-api`
(Phase 0 audits: `ARCHITECTURE_AUDIT.md`, `REUSE_MATRIX.md`). Official: the ones in §5.
Excluded on the user's instruction and not opened: `Meme-bot`, `trading-command-center`,
`goldvsbtc-binance-future`.

## 7. YouTube / other resources reviewed

**None.** Video content could not be accessed from the build environment, and nothing was
taken from secondary tutorials. Web search was used only to confirm facts that are then
cited to official sources (e.g. PumpSwap migration date, Binance XAUUSDT listing).

## 8. External repositories accepted / rejected

| Repository | Decision | Why |
|---|---|---|
| meta-muse-crossover-strategy | logic **accepted** (ported), execution rejected | 9/21 EMA divergence logic reused; its ccxt execution had no idempotency and no risk layer |
| confluence-matrix-forex | scoring **accepted** (ported); MT5 execution **BLOCKED** | MetaTrader5 ships Windows wheels only |
| hyperliquid-grid-trading-bot | grid math **accepted** (ported) + worst-case bound added | no restart reconciliation or risk budget in the original |
| solana-token-scanner / solana-sniper-jupiter-swap-api | ideas only | superseded by IDL-exact decoding and the gate; the sniper's execution path is not used |
| pybit, hyperliquid-python-sdk, binance connectors | **read, not installed** | small direct httpx adapters with our own rate budgets and error types; the SDKs were the reference for endpoints and signing |
| dexscreener PyPI client | field names only (third-party) | used as secondary evidence |

## 9. Files changed / 10. Files created

123 files touched in the second pass alone (+10.6k / −0.9k lines). Main groups:

- **Created, core:** `analytics.py`, `events.py`, `exit_intel.py`, `ml/gate_features.py`,
  `safety/pipeline.py`, `strategies/{catalog,confluence,gold_btc,grid,indicators,meta_muse}.py`,
  `venues/{common,binance_public,bybit,hyperliquid,registry}.py`.
- **Created, services:** `decision-engine/app/futures_eval.py`, `ml/app/gate_ml.py`,
  `paper-trading/app/grid_engine.py`.
- **Created, API:** routes `analytics`, `notifications`, `strategies`, `summary`, `tokens`,
  `venues`, `ws`; `health_state.py`, `util.py`; migration `0010`.
- **Created, web:** 12 new routes (see §20) and components `ConfirmDialog`, `DecisionsTable`,
  `GoldBtcSection`, `GridSection`, `LineChart`, `NotificationsBell`, `PerformancePanel`,
  `PositionsTable`, `SolanaFunnel`, `StrategyPage`, `StrategyPanel`, `ui`; libs `cc.ts`,
  `events.tsx`, `useApi.ts`.
- **Changed:** safety `gate/liquidity/models/planning/settings/store`, `paper_engine`,
  Solana `assembler/flow/pump_stream`, `state_machine`, `ml/registry`, `db/models`, `config`,
  `notify`; every service's `main.py` (heartbeats); API `control/ml/paper/system/main`;
  nginx (`/api/ws`); `.env.example`; web layout, overview, decisions, paper, rules, risk
  settings, strategies, ML, CSS.
- **Tests created:** listed in §22.

`git diff --stat 3692ef7..HEAD` gives the exact list.

## 11. Database changes

- **0009** (first pass): `risk_assessments`, `risk_settings` (versioned), `platform_settings`,
  `strategy_configs`, `blacklist_rules`, `custom_rules`, `paper_accounts`,
  `trade_timeline_events`; gate fields on `paper_positions`.
- **0010**: `paper_orders`, `strategy_states`, `notifications`, `data_quality_events`;
  `ml_features` + `assessment_id`, `engine`, `feature_version`, `outcome`, `quality_status`;
  `paper_positions` + `management_paused`, `exit_requested`; candidate state widened to 32
  chars (new ANALYZING / WAITING_FOR_LIQUIDITY / WAITING_FOR_APPROVAL); approval state
  `IGNORED`. Downgrade maps new states back safely.
- Both migrations VERIFIED: upgrade → downgrade base → upgrade, `alembic check` clean.
- `model_versions.status` now also uses `challenger` and `superseded` (no schema change).

## 12. API changes

New: `/api/analytics/{performance,gold-btc}`, `/api/strategies` (+ `/{name}`, `/config`,
`/mode`, `/hyperliquid_grid/{start,stop}`), `/api/venues` (+ `/{venue}/market`,
`/{binance,bybit,hyperliquid}/account`), `/api/summary`, `/api/notifications` (+ `/read`,
`/read-all`, `/prefs`), `/api/tokens/{mint}`, `/api/paper/positions/{id}` (+ `/exit`,
`/pause`, `/resume`), `/api/paper/orders`, `/api/system/{health,observability}`,
`/api/ml/{review,predictions,data-quality,samples}`, `/api/ml/models/{id}/promote`,
`/api/ml/{name}/retire`, `/api/control/assessments/{id}/ignore`,
`PUT /api/control/{blacklist,rules}/{id}`. Changed: paper reset needs the account name typed
as `confirm`; position and assessment lists filter by `strategy`; risk scopes and modes now
cover every engine. Every endpoint requires auth (test `test_new_endpoints_require_auth`).

## 13. WebSocket changes

New `/api/ws`. The token is sent as the first message within 5 s, never in the URL.
Refresh tokens are rejected, the Origin must be a configured CORS origin, and a ping runs
every 20 s. The server relays the Redis `yx:events` bus, carrying the spec's event names
(balance/position/trade/token/signal/risk/strategy/system.health/ml/notification). nginx
has an upgrade-enabled `location = /api/ws`. The dashboard keeps one connection per tab,
reconnects with backoff, and pages fall back to polling. Status: VERIFIED (4 tests + live
in the browser check).

## 14. Risk-engine changes

One gate for every engine, now with LONG and SHORT (SHORT only on futures venues).
Order-book liquidity model: walk the book, taker fee, band depth, book-exhausted = block.
Strategy-supplied stop/targets carry `STRATEGY` provenance and are still validated
(distance vs costs, R:R). Short loss = (1+d)(1+c_out) − (1−c_in). `max_leverage` defaults
to 1 (hard max 5). Futures-scaled engine defaults. The effective mode is the most
restrictive of strategy and venue. ML can only add WAIT via `min_ml_confidence`.

## 15. Token-safety changes

Added wallet/flow findings: SNIPER_CONCENTRATION (early-buy share), SUSPICIOUS_CLUSTER
(synchronised buyers), ROUND_TRIP_VOLUME (wash-like buy→sell), SERIAL_CREATOR (launches by
the same creator in 24 h), LOW_VOLUME. Each has an operator-editable threshold. Existing
checks remain: mint/freeze authority, Token-2022 extensions, holder concentration, creator
share, liquidity, age, blacklist, custom rules.

## 16. Sellability changes

Exit cost is modelled for every venue before entry: curve math for pump.fun, Jupiter
round-trip quotes for migrated tokens, and a book walk for futures. A position is refused
when the stop sits inside round-trip costs. Paper fills are re-priced at execution and fail
beyond `max_slippage_bps`. Exit intelligence reduces or exits only on two independent pieces
of evidence.

## 17. Paper-trading changes

- Spot vs futures accounting (futures margin = notional/leverage), SHORT PnL.
- Four books in their own currencies: SOL, USDT ×2, USDC.
- Operator exit-now / pause (stop still enforced) / resume. Explicit outcome labels at close.
- Hyperliquid paper grid with reserved capital, maker fills, drawdown and range breakers,
  and a refusal when the worst-case loss exceeds the daily-loss budget.
- MANUAL grid waits for operator start. Reset needs typed confirmation.

## 18. ML changes

Gate-model pipeline with data-quality quarantine, one sample per trade, temporal holdout,
and champion scored on the same holdout. Promotion is operator-only and audited, as is
retirement. PSI + accuracy drift sets a flag that makes decisions ignore the model. Scores
are explained (exact linear contributions) and recorded with an "influenced the decision"
flag. See `ML.md`.

## 19. Strategy integrations

| Strategy | State |
|---|---|
| Fresh tokens (pump.fun) | PAPER; gate + curve fills; live data NOT VERIFIED |
| Migrated tokens | PAPER; always needs approval (no wallet-level data) |
| Solana momentum | PAPER; unvalidated heuristic |
| Meta Muse | PAPER on Binance (venue configurable); ported logic; no edge claimed |
| Confluence Matrix | PAPER on Binance XAUUSDT; MT5/forex BLOCKED |
| Hyperliquid grid | PAPER; live mids NOT VERIFIED |
| Gold vs BTC | ANALYTICS ONLY; no trading signal |
| Binance / Bybit / Hyperliquid venues | public data + read-only accounts; live orders NOT IMPLEMENTED |

## 20. UI pages added

Fresh Tokens, Migrated Tokens, Momentum, Binance Futures / Bybit / Hyperliquid
(`/dashboard/venues/[venue]`), Meta Muse / Confluence Matrix / Hyperliquid Grid / Gold vs
BTC (`/dashboard/strategies/[name]`), ML Review, System Health, Notifications, Settings,
Trade Details (`/dashboard/trades/[id]`), Token Details (`/dashboard/tokens/[mint]`).

## 21. UI pages modified

- Layout: lucide icons, live topbar (per-account balance + PnL today, open positions, modes,
  live-lock, worst connection, realtime indicator, notifications bell), skip link.
- Dashboard (rebuilt), Strategies (catalog), Paper Trading (full §48 analytics, controls).
- Decision detail: IGNORE, confirmations, STRATEGY provenance, side/leverage, ML influence.
- Decisions filters, Blacklist & Filters (edit + confirmed delete), Risk Settings (all
  scopes), ML Engine (link to review).

## 22. Tests performed

- Python (pytest on local Postgres 16 + Redis): core, API, and every service.
- `ruff` on the whole repository.
- Alembic round-trip + `alembic check`.
- Web: `tsc --noEmit`, `next lint`, production `next build`, `npm audit`.
- Browser: Playwright/Chromium against a DB seeded by the real pipeline (Solana gate
  path, the actual Meta Muse runner, closed trades, grid, notifications, challenger). All 23
  dashboard routes at 1440 px, 6 key routes at 390 px, confirm dialog, bell, drawer,
  horizontal-overflow check, and the realtime indicator.

New test files this pass: `test_short_and_book.py`, `test_venues.py`, `test_strategies.py`,
`test_events.py`, `test_gate_features.py`, `test_analytics_catalog.py`, `test_ws.py`,
`test_control_center.py`, `test_futures_eval.py`, `test_ml_champion.py`, `test_gate_ml.py`,
`test_engines.py`, plus additions to existing files.

## 23. Tests passed

| Suite | Passed |
|---|---|
| packages/core-py | 276 |
| apps/api | 91 |
| decision-engine | 40 |
| engine-binance-futures | 48 |
| paper-trading | 36 |
| ml | 23 |
| data-binance | 14 |
| engine-solana-migration | 11 |
| engine-solana-momentum | 11 |
| data-solana | 10 |
| engine-solana-discovery | 8 |
| **Total** | **568** |

Plus ruff clean, tsc clean, lint clean, build OK, npm audit 0 vulnerabilities, migrations
clean. Browser check: no unexpected console errors (details in §24).

## 24. Tests failed

None in the final run. Failures found and fixed during development include:
- a closed position processed twice raising;
- the performance filter reading an account before data loaded (the Bybit page showed
  every account);
- the overall health reading CONNECTED while dependencies were UNKNOWN (now it can't);
- analytics counting trades from before a reset (a test-data issue; the exclusion is
  correct);
- the stop sitting inside pump.fun round-trip costs (10% Solana stop floor);
- several test-data mistakes.

Expected browser console errors, which are not failures:
- Gold vs BTC returns 502 because Binance is unreachable from the sandbox (shown as an error
  notice);
- the seed's synthetic mint is not valid base58, so the token route correctly answers 422;
- the login page's favicon is a 404.

## 25. Tests blocked

Everything that needs the network: pump.fun live logs, Solana RPC reads, Jupiter,
DexScreener, Binance/Bybit/Hyperliquid public endpoints, Bybit/Binance credentials,
Hyperliquid account by address, Telegram delivery, and the live Gold vs BTC chart.
All blocked by the build environment's proxy (403).

## 26. Remaining issues

- Every live-data path is **NOT VERIFIED** until run on the droplet.
- **No strategy has a demonstrated edge.** There are no backtests. Paper results will be
  the first evidence, and they are simulations: no latency, same-slot competition or MEV.
- Live execution: Pump.fun only, IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION (see
  `docs/AUDIT_REPORT.md`); futures venues are not implemented.
- Confluence on MT5 is BLOCKED.
- Grid results are per session, not per trade (win rate doesn't apply).
- Tokens are kept in `localStorage` (pre-existing; see `SECURITY.md`).
- `next lint` is deprecated in Next 16 (a migration is needed when Next is upgraded).
- No ML model exists until 50 clean labeled trades per engine family.
- Migrated tokens always need approval (DexScreener has no wallet identities).

## 27. Provider credentials required

Required: a Solana RPC + WebSocket endpoint that supports `logsSubscribe` (e.g. Helius).
Optional:
- Jupiter API key;
- Binance API key/secret (read side of the futures engine);
- **read-only** Bybit key/secret;
- Hyperliquid account address (not a secret);
- Telegram bot token + chat id.

## 28. Environment variables required

- `DATABASE_URL`/Postgres vars, `REDIS_URL`, `JWT_SECRET`, `ADMIN_USERNAME`,
  `ADMIN_PASSWORD_HASH`, `CORS_ORIGINS`, domain vars — unchanged from `DEPLOYMENT.md`.
- `SOLANA_RPC_URL`, `SOLANA_WS_URL` (+ backups).
- Locks: `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false`, `PAPER_TRADING=true`.
- Optional: `JUPITER_API_KEY`, `BINANCE_API_KEY/SECRET/TESTNET`,
  `BYBIT_API_KEY/SECRET/TESTNET`, `HYPERLIQUID_ACCOUNT_ADDRESS/TESTNET`,
  `TELEGRAM_BOT_TOKEN/CHAT_ID`, `BINANCE_SYMBOLS`.
- `NEXT_PUBLIC_API_URL` for the web build.

## 29. Dashboard-configurable settings

- **Risk settings per engine**: sizing, stops, TP ladder, trailing, slippage, costs,
  liquidity, holder/creator/wallet thresholds, leverage (≤ 5), daily loss, positions,
  `min_ml_confidence`. Versioned and clamped by hard limits.
- **Modes**: global mode; per strategy/venue mode (OFF/MANUAL/PAPER/AUTO; LIVE refused while
  locked).
- **Strategy configs**: Meta Muse, Confluence, Grid, Gold vs BTC — strict validation.
- **Blacklist and custom rules.**
- **Notifications**: Telegram per kind.
- **Paper books**: reset + starting balance.
- **Grid**: start/stop.
- **ML**: promote/retire.
- **Kill switch.**
- **Approvals**: approve / decline / ignore.
- **Positions**: exit / pause / resume.

## 30. `.env` settings

Secrets and locks only: credentials in §27, the three locks, infrastructure URLs. Nothing
in the dashboard can read or change them. The venue pages report only whether a credential
is set (tested: no secret appears in `/api/venues`, `/api/summary`, `/api/system/health`).

## 31. Deployment steps

`CONTROL_CENTER.md` §2 has the exact commands. In short:

1. `git pull`.
2. Confirm the three locks in `.env`.
3. Build `api web decision-engine paper-trading ml engine-solana-discovery data-solana
   data-binance engine-binance-futures reverse-proxy` one at a time. Remove the legacy
   Solana engines, run `up -d`, then `restart reverse-proxy`.
4. Run `verify_live` (§3 of that doc) and open **System Health**. Each connection should
   move from UNKNOWN to CONNECTED with evidence.
5. Leave everything in PAPER and let paper trades accumulate.

## 32. Security findings

- **Fixed:** secret-bearing RPC URLs and the Telegram token leaking to logs, DB and
  Telegram. Rotate any key configured before the first deploy.
- **Added:**
  - WS auth by first message with an Origin check, refresh tokens refused;
  - read-only venue calls only;
  - audit log on every control action (mode, config, rules, approvals, position controls,
    grid, promotion/retirement, prefs, resets);
  - confirmation dialogs, and typed confirmation for resets;
  - Redis `volatile-lru` so the kill switch (no TTL) is never evicted;
  - postcss advisory in Next's build chain removed via an npm override (audit: 0).
- **Known:** tokens in `localStorage` (now documented in `SECURITY.md`; exposure limited by
  the CSP and the ≤15 min access-token lifetime). CSP `connect-src` now names
  `wss://$host` explicitly so older Safari allows the dashboard WebSocket.

## 33. Performance findings

- No new containers for the 2 GB droplet: futures, grid and gate-ML run as loops in
  existing services. Two Solana containers were removed earlier.
- Per-venue client-side rate budgets (120 req/min, well under public limits).
- Futures run once per closed candle (Redis dedupe); Gold vs BTC is cached 60 s.
- The joblib champion is cached per process.
- The API loads scikit-learn only if needed (it doesn't install the `ml` extra).
- Redis capped at 256 MB. Heartbeats report RSS per service, visible in System Health.

## 34. Data-quality findings

- DexScreener has no wallet identities, so migrated-token manipulation checks are
  impossible; those trades need approval.
- Any stale or missing source changes the gate's data status. It never defaults a value.
- ML samples are quarantined for duplicates, label/outcome mismatch, incomplete or
  corrupted trades, missing or impossible features, missing timestamps and future leakage,
  with a reason row each.
- Repeated evaluations of one candidate count as one training sample.

## 35. ML limitations

- No trained gate model exists (zero labeled gate trades). All decisions are **rules only**.
- Once models exist: they are logistic regressions on a handful of features, trained on
  paper outcomes. Those outcomes are simulations whose own fill model has limits.
- The AUC bar (≥ 0.55, lower bound > 0.5) guards against noise, not against regime change.
  Drift monitoring is the only safeguard for that.
- ML is advisory and can only make the gate more cautious.

## 36. Exact features verified

Everything marked VERIFIED in `CONTROL_CENTER.md` §4:
- the gate (LONG/SHORT, provenance, costs, sizing);
- order-book fills;
- wallet indicators;
- exit intelligence;
- the Meta Muse / Confluence runners and the paper grid on synthetic market data;
- realtime bus + WebSocket;
- notifications and preferences;
- ML quality / challenger / promotion / drift / champion inference;
- every new API endpoint;
- migrations;
- the dashboard in a real browser (desktop + mobile), including live WebSocket updates.

## 37. Exact features not verified

- Live pump.fun stream and every Solana RPC read.
- Jupiter and DexScreener.
- Binance / Bybit / Hyperliquid live market data.
- Every account/credential read (IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION).
- Telegram delivery.
- The live Gold vs BTC chart.
- Any strategy's profitability.
- Model quality (no model exists).
- Behaviour under real market load on the 2 GB droplet.

Live execution: Pump.fun only, IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION (superseded
by `docs/AUDIT_REPORT.md`). Confluence MT5 is BLOCKED.
