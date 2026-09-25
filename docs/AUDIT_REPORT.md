# Integration audit: every repository, every provider

Date: 2026-09-25. Branch `claude/yonixalpha-platform-architecture-804nb5`.

This report covers the full "audit every repository and complete all required
integrations" task. The earlier Pump.fun audit is kept below as a sub-section.

**What was not done, stated first.** No real order was placed on any venue:
not on Binance, Bybit, Hyperliquid, PumpPortal/Solana, or MT5. No real wallet,
API key or MT5 account was used, and none was available. From this
environment, egress to exchanges, Solana RPC, pumpportal.fun and dev.jup.ag is
blocked. The locks stayed `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false`
and `PAPER_TRADING=true` throughout.

Every live path is therefore **IMPLEMENTED — AWAITING CREDENTIAL
VERIFICATION**. It is tested against mocked venues that check request shapes
and signatures, and against fake exchange accounts. Nothing here claims that
everything works.

Detailed inventories:
- [integration-config-inventory.md](integration-config-inventory.md): every variable in every repository, mapped to YonixAlpha;
- [repository-integration-matrix.md](repository-integration-matrix.md): per repository feature, and third-party ACCEPT / REJECT / PARTIALLY USE decisions;
- [environment-variable-matrix.md](environment-variable-matrix.md): YonixAlpha's own variables;
- [CONFIGURATION.md](CONFIGURATION.md): `.env` versus database settings, and start-up validation.

## Final audit status

| Status | Items |
|---|---|
| **COMPLETED** (code path traced; automated tests with fakes at the network boundary) | Audit of all 10 repositories plus the reference bot, reading source and config (not only README/.env.example). Execution providers: `BinanceProvider` (Algo-service stops), `BybitProvider`, `HyperliquidProvider` (official SDK signing), `MT5BridgeProvider`; each has BUY, SELL, STATUS, BALANCE, order confirmation and reconciliation inputs. LIVE futures and FX for Meta Muse, Gold vs BTC and Confluence: entry, exchange-side stop, exits via the shared `manage_step`, gap-free stop tightening, reconciliation. Live Hyperliquid grid (post-only orders, confirmed fills, exchange stop, breakers, mismatch → needs review). Gold vs BTC dual-trend strategy ported. MT5 bridge service (Windows) plus MT5 market data. PumpPortal data WebSocket (coverage/migration cross-check; key only for metered trades of held mints). Per-module config validator (CONFIGURATION_ERROR blocks AUTO/LIVE for that module only). Provider health with CONNECTED / DEGRADED / STALE / UNAVAILABLE / NOT CONFIGURED (plus UNKNOWN when there is no evidence). External-bot control-API adapter behind login, with a LIVE conflict guard. Sectioned `.env.example` containing only consumed variables. The three matrices. `execution-futures` service in Compose and CI. Dashboard: Live Execution futures section, External Bots page, configuration panel, venue live status |
| **PARTIALLY COMPLETED** | MT5 depth of market: many brokers publish none. Then `/book` is 404 and the gate returns NO_TRADE, so FX trading depends on the broker. Unknown-position detection on futures venues checks only symbols YonixAlpha has traded (the provider API is per symbol). Exchange balance sync uses the venue's *available* balance, so the live book shows free margin, not equity |
| **MISSING** | ML exit model (no closed-trade history to train one honestly). Automatic migration of the user's standalone bots: they are monitored and controllable, not rewritten in place |
| **BLOCKED** | Real calls to Binance, Bybit, Hyperliquid, PumpPortal, Solana RPC/WS, Helius, Jupiter, Telegram and an MT5 terminal: no egress from this environment, and no credentials. Official docs at pumpportal.fun, dev.jup.ag and developers.binance.com could not be fetched; the official SDK sources were used instead |
| **UNVERIFIED** | That real venues accept the requests exactly as built (they match the official SDKs' paths, parameters and signing, and are checked in tests). Binance Algo-order response fields beyond `algoId` / `clientAlgoId`. Hyperliquid `userFillsByTime` carrying our cloid. Real fill latency, slippage and fees. The guard allowlist against PumpPortal's current transactions |

## Verification status per integration

| Integration | Status | Evidence |
|---|---|---|
| Binance USDⓈ-M (orders, Algo stop, status, fills, balance, hedge-mode refusal) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | `test_execution_providers.py`: the signature is recomputed in the test; algoOrder params; -2013 → REJECTED |
| Bybit V5 | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | signature recomputed in the test (POST body / GET query); 110043 tolerated; position stop |
| Hyperliquid (futures + grid) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | real EIP-712 signing with a throwaway key; IOC/ALO/trigger wire shapes; partial IOC; grid lifecycle test |
| MT5 via bridge | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | bridge tested end to end through the real provider over ASGI, against a fake MetaTrader5 module |
| PumpPortal Local (Pump.fun trading) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | earlier audit (guard, sign, confirm, fills) |
| PumpPortal data WebSocket | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | scripted-socket tests: subscriptions, key only in URL, never logged |
| Jupiter | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | mocked quotes; health recorded |
| Solana RPC / WS / Helius | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | fake RPC, decoder tests |
| External bot control APIs | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | mocked bots: bearer token, no leakage, close confirmation |
| Config validation | VERIFIED (in this repository) | unit tests; API refusal tests; start-up log observed with the API running locally |
| Provider health states | VERIFIED (in this repository) | API tests; states observed in the browser |
| execution-futures container | VERIFIED (locally) | image built, non-root, 58 MiB RSS; locks closed → `disabled`; clean stop and restart recorded |
| Real exchange connectivity | BLOCKED | no egress, no credentials |
| Any real-money trade | NOT VERIFIED | deliberately not attempted |

## Parity matrix

"same" means the paper and live paths run the same code. "venue" means the
exchange does it for live, with the result read back.

| Module | Discovery | Analysis | Risk | Paper | Live | Exit | Reconciliation |
|---|---|---|---|---|---|---|---|
| Pump.fun fresh | program-log stream (+ PumpPortal coverage check) | safety gate (token, holders, flow, tax, sellability, liquidity) | same gate, sizing, max loss | curve-simulated fills | PumpPortal Local → guard → sign → confirm | `manage_step` + exit intelligence → SELL orders | wallet vs DB, stuck orders, missing tokens, unknown holdings |
| Pump.fun migrated | Pump.fun migration event (+ PumpPortal migrate) | same, PumpSwap pool from chain | same | pool-simulated fills | PumpPortal `pump-amm` | same | same |
| Solana momentum | active-mint scan | same gate | same | same | same live worker | same | same |
| Meta Muse | closed candles (Binance/Bybit/Hyperliquid) | dual-trend signal → gate (book liquidity, volatility) | same gate; strategy stop/target | book-simulated fills | `futures_live` → provider market order → fill read back → exchange stop | strategy exit rule + `manage_step` → reduce-only orders; stop tightened on venue | positions vs exchange, exchange-closed → booked from fills, missing stop re-placed, lost results via client id |
| Gold vs BTC trend | closed candles (XAUUSDT + BTCUSDT) | per-asset thresholds → gate | same | same | same | three repo exit rules + `manage_step` | same |
| Confluence Matrix | closed candles (Binance/Bybit/Hyperliquid or MT5 rates) | confluence score, pivots → gate (MT5 needs broker DOM) | same | same | same, venue `mt5` through the bridge | same | same (bridge-owned positions only) |
| Hyperliquid grid | live mid | grid build + worst-case loss vs risk budget | breakers (drawdown, range break), capped leverage | maker fills simulated at level price | ALO orders; fills only from order status; exchange stop on net position | breakers → cancel all + reduce-only flatten | exchange position vs grid each tick → needs review |
| External bots | – | – | LIVE conflict guard | – | status / close through their control APIs | close (confirmed, audited) | cached status |

### Pump.fun detail (unchanged from the earlier audit)

VERIFIED = exercised by a test in this repository (fakes at the network boundary).
"credential" = awaiting a real wallet and endpoints.

| Feature | Paper | Live | Verified | Notes |
|---|---|---|---|---|
| Detection (create/trade/migration from Pump.fun program logs) | yes | same | yes | one stream feeds both |
| Word filters / blacklist | yes | same gate | yes | |
| Token safety (authorities, Token-2022 extensions) | yes | same gate | yes | |
| Tax gate 5%/5% | yes | same gate | yes | UNKNOWN → NO_TRADE in AUTO |
| Sellability | yes | same gate | yes | curve/pool sell simulated at planned size |
| Liquidity vs size | yes | same gate | yes | REDUCE_SIZE / NO_TRADE |
| Creator / holder / funding indicators | yes | same gate | yes | |
| Fake-volume metrics | yes | same gate | yes | churn, repeated wallets, buyers stopping |
| Risk plan (SL/TP/trailing/size/max loss) | yes | same gate | yes | undefined max loss → NO_TRADE |
| Live readiness check | n/a | yes | yes | locks, worker ready, fresh wallet sync, reserve |
| Entry | simulated curve/pool fill | PumpPortal tx → guard → sign → simulate → send → confirm | yes (mocked executor) / credential | live position stays `pending_entry` until confirmed |
| Confirmation | n/a | signature status polled, rebroadcast every 3 s, EXPIRED after 75 s | yes (fake RPC) | "sent" is never treated as filled |
| Actual fill recording | simulated | pre/post wallet balances of our owner+mint | yes (fake tx) | ATA rent and network fee included in cost |
| Monitoring / pricing | curve (stream) or PumpSwap pool (RPC) | same | yes | |
| TP1–3, trailing, stop, manual exit | `manage_step` | same `manage_step`, exits become SELL orders | yes | |
| Exit intelligence (REDUCE/EXIT) | yes | same | yes | curve and PumpSwap flow |
| Holder monitoring after entry | yes | same | yes | holders re-read from chain at most once a minute; a creator dump, insider distribution or concentration jump since entry counts only together with sell pressure, seller dominance or a liquidity drop |
| Execution failures | simulated: entry/exit failure rate (operator setting, or the measured live rate once 20+ live orders of that side have a final outcome); deterministic per attempt | real | yes | failed paper entry opens nothing; failed paper exit is retried next tick at that tick's price; at most 5 simulated failures in a row |
| Failed exit handling | n/a | retried with +10% slippage per failure (capped), critical alert from the 2nd failure | yes | |
| Realized PnL | proceeds − cost (simulated) | proceeds − cost (confirmed) | yes | same `close_position` |
| ML sample + label | yes | yes (`live_execution_realized_pnl`) | yes | |
| DB records / timeline | yes | yes + `execution_orders` | yes | |
| Realtime events | yes | yes | yes | E2E asserts trade.created/updated/closed |
| Restart recovery | n/a | reconcile first; SIGNED/SUBMITTED looked up; stale PENDING cancelled | yes | |
| Duplicate prevention | n/a | idempotency key, one live position per mint, one pending order per position, row lock | yes | |
| Unknown holdings | n/a | recorded once per 24 h | yes | |
| Provider errors | n/a | guard refusal / HTTP error / simulation failure → FAILED, no fill | yes | |

## Final verification

| Check | Result |
|---|---|
| Unit tests (all 13 projects) | VERIFIED: 716 passed, 0 failed |
| Integration tests (DB + Redis, fakes at the network boundary) | VERIFIED: futures live lifecycle (10), live grid (4), execution-futures loop (5), MT5 bridge through the real provider (7), Pump.fun E2E/live worker (earlier) |
| API tests | VERIFIED: 101 passed. Includes config validation, futures settings, external bots, health states |
| WebSocket | VERIFIED as part of the API/E2E suites (event publication); the browser showed the realtime link connected |
| Database | VERIFIED: `alembic check` reports no drift. Downgrade/upgrade round trip on 0011 ran. No schema change was needed (new statuses fit the existing constraints) |
| Risk tests | VERIFIED: gate, planning and tax suites; live refusal when not ready, when a standalone bot conflicts, and when locks are closed |
| Paper tests | VERIFIED: paper-trading suite (61); paper grid stays out of live mode |
| Provider connectivity | BLOCKED (no egress, no credentials). Request shapes and signatures VERIFIED against mocks |
| Config validation | VERIFIED: unit + API tests; start-up log observed |
| Frontend | VERIFIED: ESLint clean, `tsc` clean, production build OK. Chromium at 1400 px and 390 px on Health, External Bots, Live Execution, venues (mt5, binance), Strategies, Gold vs BTC Trend: no console errors, no horizontal overflow. The missing favicon that caused a 404 was added |
| E2E | VERIFIED (paper): signal → gate → live order → fake exchange fill → exchange stop → exit → realized PnL, and the Pump.fun paper E2E from the earlier audit |
| Security | VERIFIED: no secret logged or returned (tests assert the absence of token/key values); control APIs behind login; bridge requires a ≥ 32-char token with constant-time compare; no committed keys found in the diff |
| Docker build | VERIFIED locally for `execution-futures`. This environment needed its proxy CA injected through a scratch Dockerfile; the committed Dockerfile is unchanged |
| Compose | VERIFIED: `docker compose config` with the base + prod files lists `execution-futures` |
| Restart | VERIFIED: the container records `service_stopped` on SIGTERM and `service_started` again; reconcile runs first after a start |

## The 44 report items

1. **Requirements completed**: the COMPLETED row above.
2. **Requirements partially completed**: the PARTIALLY COMPLETED row.
3. **Requirements missing**:
   - an ML exit model;
   - in-place rewriting of the standalone bots. They are monitored and controlled through their control APIs, and conflicts block LIVE.
4. **Existing architecture**:
   - Solana engines → Redis stream store → decision-engine (safety gate, futures runner) → paper-trading (paper, Pump.fun live worker) and the new execution-futures (futures/FX/grid live);
   - both feed the API and the Next.js dashboard. PostgreSQL holds state; Redis carries events, readiness and caches;
   - services/mt5-bridge runs on the Windows MT5 host, outside the stack.
5. **Features preserved**:
   - all Pump.fun paths;
   - paper futures and grid;
   - Gold vs BTC ratio analytics;
   - ML champion/challenger (no automatic promotion);
   - notifications, kill switch, word filters, every gate check;
   - `engine-binance-futures` (Phase 4 account sync), untouched.
6. **Features upgraded**:
   - futures strategies can trade LIVE;
   - Confluence can use MT5 data and execution;
   - the grid can run LIVE;
   - health states now include UNAVAILABLE and NOT CONFIGURED plus more providers;
   - the Solana live worker, reconcile and measured failure rates are scoped to PumpPortal orders only.
7. **Files changed** (main):
   - `config.py`, `safety/{gate,pipeline,settings,store}.py`, `strategies/{catalog,meta_muse,gold_btc}.py`;
   - `venues/{common,registry}.py`, `solana/market_data.py`, `live_trading.py`, `paper_execution.py`;
   - `decision-engine/app/futures_eval.py`, `paper-trading/app/{gate_manage,grid_engine,live_worker}.py`, `engine-solana-discovery/app/main.py`;
   - API `health_state.py`, `routes/{control,live,system,strategies,venues}.py`, `main.py`;
   - web health, live, venue pages, `layout.tsx`, `ui.tsx`, `cc.ts`;
   - `.env.example`, the three compose files, `ci.yml`, `docs/CONFIGURATION.md`.
8. **Files created**:
   - `execution/{base,binance,bybit,hyperliquid,mt5_bridge,registry}.py`;
   - `futures_live.py`, `grid_live.py`, `external_bots.py`, `config_validation.py`;
   - `solana/pumpportal_ws.py`, `strategies/gold_btc_trend.py`, `venues/mt5.py`;
   - `services/execution-futures/`, `services/mt5-bridge/`;
   - API `routes/bots.py`;
   - web `dashboard/bots`, `components/FuturesLive.tsx`, `app/icon.svg`;
   - tests for each; the three matrices.
9. **Database changes**: none needed.
   - Futures orders reuse `execution_orders`, with `provider` set to the venue provider, `amount_kind` `base`, and the client id stored in `signature`.
   - Grid orders use provider `hyperliquid_grid`.
   - Live books are `paper_accounts` rows named `live_<venue>`.
   - The live grid state is `strategy_states` key `live:<coin>`.
   - `alembic check` is clean.
10. **API changes**:
    - `GET /api/live/futures`, `PUT /api/live/futures/settings`, `GET /api/system/config-validation`;
    - `/api/external-bots` (list, `/{name}/config`, `/{name}/close`);
    - mode changes answer 409 CONFIGURATION_ERROR with variable names;
    - venues list `mt5` and `live_orders` as a health entry;
    - grid sessions carry `mode`.
11. **WebSocket changes**: none in protocol. Live futures and grid events are published with source `live` / `grid`, as trade.created / updated / closed and strategy.updated.
12. **PumpPortal integration**:
    - trading uses the Local Transaction API (no key, signed locally);
    - the data WebSocket (free new-token/migration feeds) is added as an independent coverage and migration check;
    - `PUMPPORTAL_API_KEY` is only for the metered trade feed of held mints;
    - the custodial Lightning API is rejected.

    IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
13. **Helius integration**:
    - standard RPC/WS (URLs derived from `HELIUS_API_KEY`, or the `HELIUS_*_URL` aliases);
    - health now has a `helius` entry.

    IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
14. **Solana execution integration**: unchanged from the earlier audit, and now isolated from futures orders by provider.
15. **Pump.fun fresh-token implementation**: unchanged, plus the PumpPortal coverage metric (health degrades if the on-chain stream misses announced tokens).
16. **Pump.fun migration implementation**: unchanged, plus the PumpPortal `migrate` cross-signal stored for comparison.
17. **PumpSwap execution implementation**: unchanged (`pump-amm` via PumpPortal Local, guarded).
18. **Buy implementation**:
    - Solana: unchanged.
    - Futures:
      - market order by client id;
      - quantity rounded to the venue step and checked against the minimum quantity and notional;
      - leverage set first; hedge mode refused;
      - the fill (quantity, average price, fee) is read back;
      - a fill more than `max_fill_deviation_pct` from the plan is closed at once.
19. **Sell implementation**:
    - Solana: unchanged.
    - Futures:
      - reduce-only market orders;
      - a full exit closes what the exchange actually holds;
      - partial take-profits are booked at the actual fill.
    - Grid: reduce-only flatten.
20. **Automatic exit implementation**:
    - the shared `manage_step` (stop, TPs, breakeven, trailing) plus the strategy exit rules;
    - an exchange-side stop covers the whole position and is only ever tightened: the new stop is placed before the old one is cancelled;
    - if a stop cannot be placed, the position is closed;
    - no manual step is needed in AUTO.
21. **Tax implementation**: unchanged — buy tax 5% / sell tax 5%; UNKNOWN → NO_TRADE in AUTO; kept separate from venue/protocol fees.
22. **Sellability implementation**: unchanged.
23. **Liquidity implementation**:
    - unchanged for Solana;
    - futures use the venue order book (MT5 needs the broker's depth of market, never invented).
24. **Token-risk implementation**: unchanged.
25. **Wallet-risk implementation**: unchanged.
26. **Word-filter implementation**: unchanged.
27. **Blacklist implementation**: unchanged.
28. **Automatic risk implementation**:
    - unchanged gate;
    - futures leverage is capped by both the gate's `max_leverage` and `futures_live_execution.max_leverage`;
    - a free-balance reserve is enforced;
    - grid worst-case loss must fit `max_daily_loss_quote`.
29. **Paper implementation**:
    - unchanged;
    - Gold vs BTC trend added;
    - Confluence can paper-trade on MT5 data.
30. **Live implementation**:
    - Pump.fun (unchanged);
    - Meta Muse, Gold vs BTC and Confluence on Binance, Bybit, Hyperliquid or MT5;
    - Hyperliquid grid.

    All locked by the three environment flags, global LIVE, module AUTO/MANUAL, venue readiness from execution-futures, config validation and the external-bot conflict guard. IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
31. **ML implementation**:
    - unchanged;
    - live futures samples are recorded like paper ones and labelled from realized PnL (source `live_execution_realized_pnl`);
    - a high ML score still cannot override a risk block.
32. **Strategy implementation**:
    - `gold_btc_trend` added (ported: gold 0.005% / BTC 0.03% gaps, three exit rules);
    - Meta Muse gained optional per-asset thresholds;
    - Confluence gained venue `mt5`.
33. **UI implementation**:
    - External Bots page;
    - Live Execution → Futures & FX (per-venue readiness, settings);
    - System Health → Configuration and the new provider entries;
    - venue pages show live execution state;
    - Gold vs BTC Trend in the navigation;
    - favicon.
34. **.env variables**: see [environment-variable-matrix.md](environment-variable-matrix.md).
    - Added (all consumed): `PUMPPORTAL_API_KEY`, `HYPERLIQUID_API_WALLET_PRIVATE_KEY`, `MT5_BRIDGE_URL/TOKEN`, and four `*_CONTROL_URL/*_TOKEN` pairs.
    - Removed: the unused `MAX_SLIPPAGE`.
    - Not invented: a Jupiter secret, `EXCHANGE`, `DASHBOARD_TOKEN`.
35. **Dashboard settings**:
    - futures live settings (max leverage, free-balance reserve, balance max age, max fill deviation);
    - venue per strategy (including `mt5`);
    - Gold vs BTC trend parameters;
    - everything earlier.
36. **Tests performed**:
    - pytest and ruff for 13 projects;
    - `alembic check` and a round trip;
    - ESLint, `tsc` and the Next.js build;
    - a Chromium sweep at two widths;
    - Docker build and a container run/restart;
    - `docker compose config`.
37. **Tests passed**:

    | Suite | Passed |
    |---|---|
    | core | 354 |
    | API | 101 |
    | decision-engine | 59 |
    | paper-trading | 61 |
    | engine-binance-futures | 48 |
    | ml | 23 |
    | data-binance | 14 |
    | momentum | 11 |
    | migration | 11 |
    | data-solana | 10 |
    | execution-futures | 9 |
    | discovery | 8 |
    | mt5-bridge | 7 |
    | **Total** | **716** |

38. **Tests failed**: none in the final run. Problems found and fixed while building:
    - a mode-change refusal that was too broad: unrelated PAPER modules blocked switching to MANUAL;
    - the grid order bookkeeping mutated a list while iterating it;
    - an MT5 fee float artefact in a fixture;
    - an IOC cancelled with a partial fill was mapped to a non-terminal state;
    - four test expectations updated for the new states and catalog entries;
    - two matrix claims about other repositories were checked against source, found wrong, and corrected before commit.
39. **Tests blocked**: anything needing a real exchange, Solana RPC, PumpPortal, Jupiter, Telegram or an MT5 terminal.
40. **Unverified integrations**:
    - every external service in the verification table;
    - the exact Binance Algo response fields;
    - broker depth-of-market availability for FX.
41. **Security findings**: see the list in [integration-config-inventory.md](integration-config-inventory.md#security-relevant-findings-in-the-source-repositories).
    - In the user's and reference repositories:
      - the reference bot has a hidden 0.5% referral fee;
      - two bots use the custodial Lightning API;
      - Meta Muse and goldvsbtc place stops through ccxt `stopLossPrice` against Binance's Algo migration (-4120). goldvsbtc then keeps the position open without protection;
      - the grid bot books "assume filled".
    - In this code:
      - keys are `SecretStr` and never logged or returned;
      - a Hyperliquid agent wallet (cannot withdraw), not the main key;
      - control APIs sit behind the dashboard login;
      - the bridge needs a ≥ 32-char token and is private-network only;
      - the external-bot close needs typed confirmation and is audited.
42. **Performance findings** (2 GB droplet):
    - execution-futures idles at about 58 MiB RSS. The image is 1.03 GB on disk, mostly the Hyperliquid SDK's Ethereum dependencies;
    - loops: orders every 2 s, manage every 5 s, reconcile every 30 s;
    - the PumpPortal free feed adds one WebSocket to the discovery service.
43. **Data-quality findings**:
    - PumpPortal coverage below 80% of ≥ 20 announced tokens marks the on-chain stream DEGRADED;
    - MT5 symbols whose profit currency differs from the account currency are refused, so PnL stays in one currency;
    - futures balances are available margin, not equity.
44. **Remaining work**:
    1. With small, dedicated funds, verify one venue at a time:
       - set the keys, open the three locks and set global LIVE;
       - put one strategy on AUTO or MANUAL with a small size;
       - watch the venue turn READY on Live Execution;
       - compare one entry, its exchange stop and one exit with the exchange's own history.
       - Recommended order: Binance testnet (`BINANCE_TESTNET=true`), Hyperliquid testnet, then mainnet.
    2. Install services/mt5-bridge on the Windows MT5 host and connect it over a private tunnel. Check whether the broker publishes depth of market for the Confluence symbols.
    3. Run the Pump.fun live check from the earlier audit.
    4. Decide whether to keep the standalone bots running. If kept, set their control URLs/tokens so YonixAlpha can see them and avoid trading the same strategy twice.
    5. Upgrade or retire the standalone Meta Muse and goldvsbtc bots: their Binance stop orders may be rejected (-4120).
    6. An ML exit model once enough closed trades exist.
