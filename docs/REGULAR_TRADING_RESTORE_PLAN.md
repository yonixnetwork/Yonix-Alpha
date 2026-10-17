# Regular trading restore: audit, archive and plan (2026-10-16)

Directive: "Restore regular trading, archive Memecoin from the active
dashboard, research strategies, build professional trade management."
This file records the Phase 1 audit, the Phase 2 change, and the decisions
the next phases need. Labels: VERIFIED (checked in code, Git or on the
server), DOC (official documentation, not yet tested by us), NOT VERIFIED.

## 1. How regular trading was removed (VERIFIED from Git)

- Commit `7e87552` (2026-09-29), "Remove futures, forex, grid and legacy
  strategies from the active app": 154 files, about 10,400 lines deleted.
- It was archived first on branch
  `archive/legacy-futures-forex-grid-2026-09-29` (`f3d833b`), which still
  exists on GitHub. The parent commit `7e87552^` also holds everything.
- What it removed:
  - services `data-binance`, `engine-binance-futures`, `execution-futures`,
    `mt5-bridge`;
  - in `packages/core-py`: `execution/{base,binance,bybit,hyperliquid,mt5_bridge,registry}.py`
    (signed live order providers with exchange-side stops),
    `futures_live.py`, `grid_live.py`, `external_bots.py`, venue adapters
    and the strategies Meta Muse, Confluence, Grid, Gold vs BTC;
  - API routes `venues`, `bots`, gold-btc, live futures and grid; web pages
    `venues`, `bots` and the futures / grid / gold sections.
- The database history was kept (no table was dropped).

**Why it is not restored "as it was":** all of that code lived inside
`packages/core-py` and was wired into the Memecoin `decision-engine` and
`paper-trading` workers. Restoring it in place would change every Memecoin
image and its scheduler, which the operator's rules forbid. It also read
exchange keys from `.env`, which every Memecoin container loads. The plan is
to **port** the useful parts (provider contract, Bybit / Binance signing,
exchange-side stop handling, reconciliation inputs, their tests) into
`packages/crypto-py`, which is our own code and needs no licence. Nothing
is copied from the unlicensed research repositories.

## 2. Checklist

| Item | Status | Evidence / note |
|---|---|---|
| Crypto engine, own schema `crypto`, worker, API, Strategy Lab | COMPLETED, deployed | PR #81, handoff section 2 |
| Public market data: Binance.US spot | VERIFIED working from the server | HTTP 200, worker logs |
| Public market data: Bybit, Binance.com | BLOCKED from this server | Bybit HTTP 403, Binance.com HTTP 451 (server is in a US region, DigitalOcean NYC3) |
| Meta Muse v1 (spot LONG, DEMO) | COMPLETED, running in DEMO | candle default **150 is correct**: original `config.py: MAX_KLINES = 150` at `a163ff7`. The "31 candles" figure belongs to Gold vs BTC (`SLOW_MA_PERIOD + 10`). No change needed. |
| Memecoin archived from the main menu | COMPLETED in this branch (not deployed) | section 3 |
| Signed Bybit / Binance providers, exchange-side stops | REMOVED in `7e87552`, recoverable | port into crypto-py (Phase 3/6) |
| Demo/testnet and live API profiles, encrypted keys | NOT IMPLEMENTED | the project's encryption approach exists (`yonixalpha_core/secretbox.py`, Fernet from `JWT_SECRET`); crypto-py needs its own copy, since it must not import core |
| Trade thesis, ATR / swing stops, partial exits, trailing stops, invalidation monitor | NOT IMPLEMENTED (only fixed stop / target in `runner.protect_positions`) | Phase 5 |
| Risk page for crypto (drawdown, consecutive losses, per-exchange limits) | PARTIAL | `risk.RiskLimits` has per-trade, position, exposure, daily loss, open positions, spread, age |
| Backtesting | IMPLEMENTED (local runner); import off until verified | runs locally on downloaded public candles, never on the VPS (`docs/LOCAL_BACKTESTS.md`) |
| Hyperliquid grid | NOT IMPLEMENTED | Hyperliquid info API reachable from the server; separate venue |
| Gold vs BTC | BLOCKED | gold source not chosen |
| Confluence (forex) | BLOCKED | no forex source; MT5 is Windows-only |
| Olymp Trade | BLOCKED | no official public trading API found (search 2026-10-16 shows only third-party browser extensions, which we must not use) |
| Research repos (Meta Muse `a163ff7`, grid `710cebb`, confluence `f3beff9`) | accessible, **no licence** | rules re-implemented only |

## 3. Phase 2: Memecoin archived from the main menu (this branch)

What changed (web only):
- `apps/web/components/archivedEngines.ts`: the Memecoin menu groups,
  unchanged, plus `MEMECOIN_ARCHIVED = true`.
- `apps/web/app/dashboard/layout.tsx`: the main menu shows Crypto Trading,
  System (Health, Notifications) and Archived Engines. The Memecoin groups
  appear only while a Memecoin page or Archived Engines is open. Memecoin
  pages get a banner saying the engine still runs and their controls act on
  it. The Memecoin balances, positions pill, global mode and live-lock
  pills in the top bar show only on Memecoin pages; the kill-switch pill
  and system status stay everywhere.
- `apps/web/app/dashboard/page.tsx` -> moved unchanged (title text only) to
  `dashboard/memecoin/page.tsx`. `/dashboard` now redirects to
  `/dashboard/crypto`.
- `apps/web/app/dashboard/archived/page.tsx`: read-only page with the live
  Memecoin state (`/api/summary`), the difference between hiding, disabling
  and stopping, and links to every Memecoin page.

Not changed: any API, worker, setting, risk limit, database row, `.env`,
`apps/web/lib`, any Memecoin page logic. The boundary check is empty.

Hide / disable / stop:
- Hide (done): menu only. Memecoin keeps trading in its mode and managing
  positions.
- Disable new entries (not done): Memecoin Settings, global mode or
  strategies OFF. Monitoring and exits keep running.
- Stop workers (not done): `docker compose stop`. Also stops position
  monitoring, so never with an open position.

Restore: set `MEMECOIN_ARCHIVED = false` and redeploy the web image
(`build web` + `up -d --no-deps web`). Rollback of this change: revert the
commit and redeploy web.

## 4. Exchange environments (DOC, to verify from the server)

| Connection | REST host | Notes |
|---|---|---|
| Bybit live | api.bybit.com | 403 from this server |
| Bybit demo | api-demo.bybit.com | key made on the mainnet account in Demo Trading; not every endpoint; public data comes from mainnet (also 403 here) |
| Binance live | api.binance.com | 451 from this server |
| Binance spot Demo Mode | demo-api.binance.com | own keys; mirrors live features |
| Binance spot Testnet | testnet.binance.vision | own keys; `/api` only, monthly reset |
| Binance.US live | api.binance.us | reachable; spot only |

Bybit and Binance.com refuse US locations, and their terms restrict US
users. YonixAlpha must not route around a geographic refusal (no proxy or
VPN). If the demo hosts also refuse the server, Bybit and Binance stay
BLOCKED on this server, and the only compliant options are the operator's
decision: a server region where the operator is permitted to use these
exchanges (an infrastructure change that needs approval), or Binance.US.

## 5. Decisions needed from the operator

1. Live trading: the previous rule was DEMO only. The new directive asks
   for a live capability that is built but never enabled automatically.
   Confirm that this replaces the DEMO-only rule for the crypto engine.
2. Where the operator is allowed to use Bybit and Binance.com (their terms
   exclude US persons), and whether to keep the server in NYC3.
3. Whether Binance.US counts as "Binance" for demo work in the meantime.

## 6. Order of the next phases

1. (operator) server read-only checks: demo / testnet reachability and
   resources.
2. Phase 3: connection profiles (DEMO/TESTNET vs LIVE, encrypted keys,
   connection test, no withdrawal permission) in crypto-py; live execution
   code path present but locked behind backend checks.
3. Phase 5: trade thesis and management (ATR stop sizing, partial exits,
   trailing, invalidation, reasons recorded), demo first.
4. Phase 4 / 12: research and local backtests, then a small strategy set.

## 7. Operator decisions (2026-10-16, second message)

1. Live trading: build it (secure API profiles, exchange-side protective
   orders, monitoring, stop-loss, take-profit, trailing, reconciliation) but
   keep it LOCKED. No real order until the operator reviews the code and
   authorizes activation in writing. Demo / testnet and live keys stay
   completely separate.
2. The operator is in Nigeria; the server stays in New York unless the
   operator approves a change. No proxy or VPN. Verify eligibility and test
   the documented demo / testnet hosts; document any limitation.
3. Binance.US is a separate provider, not a substitute for Binance.com; not
   enabled without approval.

## 8. Eligibility research (2026-10-16; NOT legal advice, NOT VERIFIED from official notices)

- **Server location versus the operator's eligibility are two separate
  questions.** The server's US IP is refused by Bybit (HTTP 403) and
  Binance.com (HTTP 451) whatever the account holder's country, so API
  calls from this server cannot work, and routing around that refusal is
  not allowed. The operator's own eligibility (a Nigerian resident) is
  decided by each exchange's terms for the operator's account.
- **Bybit and Nigeria (third-party sources only):** no Nigerian SEC
  registration found; one August 2026 review says Nigeria was not on
  Bybit's restricted-country list; accounts linked to Bybit were reportedly
  frozen by the EFCC in September 2024. Status: grey area, unlicensed
  locally.
- **Binance and Nigeria (third-party sources only):** not registered with
  Nigeria's SEC; naira deposits / withdrawals stopped in March 2024; the NCC
  ordered Nigerian ISPs to block Binance (reported still in place in late
  2025); litigation with the Nigerian government was ongoing in 2026.
- **What the operator should check directly:** the restricted-jurisdiction
  section of each exchange's current Terms of Use, and the account's own
  status page (KYC country, any restriction notices). I could not reach an
  official restricted-country list in this session.
- **Compliant options if the hosts stay refused from New York:**
  (a) keep Bybit / Binance as code-complete but BLOCKED on this server;
  (b) with the operator's approval, a server (or a second small server) in
  a region that both exchanges serve and where the operator's use is
  permitted; (c) a different, licensed provider (Binance.US only with
  approval and only if the operator's account is eligible there).

## 9. Implemented in this round: exchange connections and the live lock

Code (all in `packages/crypto-py/yonixalpha_crypto/private/`, isolated from
the strategy engine, which stays public-data-only):

| Part | What it does |
|---|---|
| `environments.py` | Fixed host per (exchange, environment): Bybit DEMO `api-demo.bybit.com`, TESTNET `api-testnet.bybit.com`, LIVE `api.bybit.com`; Binance DEMO `demo-api.binance.com`, TESTNET `testnet.binance.vision`, LIVE `api.binance.com`. No override, no fallback. Binance.US and Hyperliquid are not listed. |
| `secretstore.py` | Fernet encryption at rest with its own HKDF-derived key (from `CRYPTO_CREDENTIALS_KEY` or `JWT_SECRET`); a keyed fingerprint refuses the same key in two environments; only the last 4 characters are ever shown. |
| `signedhttp.py` | The only signing client. Allowlist of (method, path): account reads and order management only. Transfer, withdrawal, deposit-address, sub-account and key-management endpoints are refused before sending. Errors and `repr` are scrubbed of key, secret and signature. |
| `livelock.py` | `LIVE_ORDERS_UNLOCKED = False` in code, plus per-connection gates: LIVE environment, enabled, keys readable, tested OK within 24 h, withdrawal permission OFF (verified), IP-restricted (verified), trading permission present. All must pass. |
| `connections.py` | Save (disabled by default), view (never the key), enable / disable, delete, and a read-only test that records the permissions the exchange reports. |
| DB | Migration `c0002`: table `crypto.api_connections` (additive; downgrade drops only it). |
| API | New file `apps/api/app/api/routes/crypto_connections.py` (+2 router lines): saving needs the dashboard password and the environment typed twice; LIVE changes need the password; tests are throttled (10 s). |
| UI | Crypto Trading > Exchange Connections: add / test / enable / delete, permissions and every live gate shown with pass / fail. |

### What is verified, and how

| Requirement | Status | Evidence |
|---|---|---|
| Encrypted secret storage | VERIFIED (tests) | ciphertext never contains the key; another key cannot decrypt; rotated key -> NEEDS_REENTRY, never guessed |
| Keys never returned to the browser | VERIFIED (tests) | API responses and listing contain neither key nor secret |
| Demo and live keys separate | VERIFIED (tests) | fixed hosts per environment; env-var override ignored; same key refused in a second environment; typed confirmation |
| Withdrawals / transfers impossible | VERIFIED (tests) | 9 refused requests, none reached the (mock) network |
| Backend live-mode lock | VERIFIED (tests) | every LIVE order path refused before sending; each gate tested on its own |
| Request signing | VERIFIED against the documented scheme (tests) | NOT yet verified against a real exchange: needs a demo key and a reachable host |
| Least-privilege permission check | IMPLEMENTED, tested with the documented response shapes | NOT verified with a real key; Bybit demo and the Binance testnet may not report key permissions (shown as "not verified", which keeps the live gates closed) |
| Order sizing for live orders | NOT IMPLEMENTED | demo sizing exists (`risk.size_entry`) |
| Exchange-side protective orders | NOT IMPLEMENTED | the endpoints are on the allowlist only (Bybit `/v5/position/trading-stop`, Binance OCO / OTO) |
| Failure recovery and order reconciliation | NOT IMPLEMENTED | next round (port of the removed providers' `order_status` / `fills_since` logic) |
| Trailing stops, partial exits, trade thesis, invalidation monitor | NOT IMPLEMENTED | next rounds |
| Live risk limits | NOT IMPLEMENTED for live | demo `RiskLimits` exist |

Tests: crypto-py 81 passed (58 before + 23 new); crypto-trading 2 passed;
API crypto suites 10 passed; web typecheck, lint and build clean; browser
check of the new page with stubbed data.

## 10. Execution and trade management (2026-10-16, third round)

Everything is in `packages/crypto-py/yonixalpha_crypto/private/` (isolated
from the strategy engine and from Memecoin). `LIVE_ORDERS_UNLOCKED` stays
`False`: an entry on a LIVE connection is refused before anything is
written, and the signing client refuses every LIVE order request again.

| Part | File | What it does |
|---|---|---|
| Sizing | `sizing.py` | Size from equity, risk % (capped by a maximum loss), entry, stop-limit, taker fees on both legs and slippage; capped by the maximum position and the free balance; rounded DOWN to the step; planned loss re-checked after rounding. Rejects unknown precision, non-finite input, a stop closer than two ticks, a size below the exchange minimum. A wider stop always gives a smaller size. |
| Adapters | `adapters.py` | Separate Binance and Bybit spot account adapters (see the table below). |
| Executor | `executor.py` | Write-ahead orders, verified protection, reconciliation, recovery, the entry guard, exits, partial exits, stop moves. |
| Management | `management.py` | Pure decisions with reasons: HOLD, MOVE_STOP (only up), PARTIAL_EXIT, CLOSE (strategy exit / invalidation, time stop, engine-managed target), ALERT. Trailing: percent, ATR, swing low. |
| Worker pass | `service.py`, `engine.py` | Every loop, in every engine state: reconcile each connection with an active exchange trade, then manage its open trades. The worker does not exit while an exchange trade is active. |
| Practice test | `practice_lifecycle.py` | The operator's end-to-end test on a DEMO / TESTNET connection (refuses LIVE). |
| Tables | migration `c0003` | `exec_trades`, `exec_orders`, `trade_actions`, `exec_guard` (additive). |
| API / UI | `crypto_execution.py`, Crypto Trading > Exchange Trades | Trades, orders, every decision with its reason, blocked-entry alerts, Close (password). |

### Exchange differences (handled separately, not assumed identical)

| | Binance spot | Bybit spot |
|---|---|---|
| Entry / exit | `POST /api/v3/order` MARKET, FULL answer with fills and commission | `POST /v5/order/create` Market, `marketUnit=baseCoin` (otherwise a BUY quantity is read in USDT); answer has ids only, so the order is looked up |
| Exchange-side stop | STOP_LOSS_LIMIT | spot TP/SL order (`orderFilter=tpslOrder`) |
| Exchange-side target | yes, OCO `POST /api/v3/orderList/oco` (LIMIT_MAKER above, STOP_LOSS_LIMIT below) | no: the stop reserves the coins, so the target is ENGINE-MANAGED (market sell when the bid reaches it, checked every loop) |
| Moving the stop | cancel + place (an OCO cannot be amended): a few seconds with no stop, recorded | amended in place (`POST /v5/order/amend`): no gap |
| Order lookup | `GET /api/v3/order` by client id; -2013 = not found | `/v5/order/realtime`, then `/v5/order/history` |
| Fee asset on a buy | from the fills (commissionAsset) | assumed: the coin received; checked by the balance reconciliation |
| Client id reuse | allowed once the first order is no longer open, so a lost-answer market order is NEVER re-sent | duplicate orderLinkId is rejected |

### Failure handling (what the tests prove)

- **Lost answer** (the order executed, the answer was lost): the order is
  UNKNOWN, new entries are blocked, a fresh executor (restart) finds it by
  client id, the trade continues, exactly one buy exists.
- **Request never arrived**: stays blocked for 60 s, then NOT_FOUND and the
  trade FAILED; the block lifts.
- **Rejected entry**: FAILED, nothing else sent.
- **Partial fill**: the stop covers exactly what was bought (after the fee).
- **Stop rejected**: the position is closed and entries blocked.
- **Stop removed by the exchange**: detected, replaced, verified.
- **Coins missing from the account**: trade UNCERTAIN, entries blocked.
- **Exit with a lost answer**: resumed after restart, booked once.
- **Duplicates**: the same signal (entry_key) or a second trade on the same
  symbol and connection is refused, also after a restart.
- **Keys unreadable or exchange unreachable while a trade is open**: entries
  blocked, ERROR event, nothing guessed.

### Strategy research and selection (no profitability claim; nothing backtested yet)

Sources re-read in this round (no code copied; none of the three has a licence):

| Repository | Commit | What the source actually does | Findings |
|---|---|---|---|
| hoziertom44-arch/Meta_Muse_Crossover_strategy | a163ff7 | BTC and ETH 5m EMA 9/21 trends; "strong" = separation >= 0.15%; ETH is traded against BTC's divergent strong trend; SL 1% / TP 2% as exchange closePosition orders; early exit when either trend weakens or the signal changes; fixed 50 USDT margin at 10x | fixed-margin 10x sizing is replaced by risk-based sizing; futures both ways in the original, spot LONG only here |
| hoziertom44-arch/hyperliquid_grid_trading_bot | 710cebb | 7-level grid, auto range (config value 0.5 with a comment saying ±3%: inconsistent), 5x leverage, PAUSES (does not close) on 15% drawdown or a 1.5% range break | a pause leaves the inventory open in a trend; ships a `trades.jsonl` (134 lines) that cannot be verified; not selected |
| siriusforex-ai/confluence_matrix_forex | f3beff9 | M15 pivot breakout (lookback 12); SL beyond the opposite pivot + 5% of the wave; TP1 = +0.618 wave (close 50%, stop to breakeven), TP2 = +1.414 wave; 0.5% risk; V2 adds a confluence score (structure 30, liquidity 25, RSI 20, proximity 25, threshold 75) | its backtest charges no spread, commission or slippage, so its results are not evidence; MT5 only; forex blocked |

Selected for the first practice / backtest round (explicit rules; each uses
the executor above; all spot LONG):

1. **Meta Muse (v1, already running in simulated DEMO).** Entry: BTC and ETH
   both "strong" (EMA 9/21 separation >= 0.15%, closed 5m candles, 150
   candles) with BTC down and ETH up -> long ETH. Stop: 1% below entry
   (original). Target: 2% (original; exchange-held on Binance,
   engine-managed on Bybit). Invalidation / early exit: either trend no
   longer strong, or the signal no longer LONG (original rule). Trailing:
   none (the original has none). Sizing: risk-based (deviation from the
   original's fixed margin, documented).
2. **Trend pullback (rule idea from arwensorcenelli/trend-bot, re-implemented,
   to be backtested before any practice use).** 1h candles. Regime: SMA 10 >
   SMA 50 and close > SMA 50. Entry: a close back above SMA 10 after a close
   below it. Stop: entry - 3 x ATR(14). Target: none; trailing 3 x ATR from
   the highest price after +1 R. Invalidation: close below SMA 50. Time stop:
   48 bars below +0.5 R.
3. **Not selected now:** the grid (inventory risk, unverifiable record),
   Confluence on forex (no source, cost-free backtest), Gold vs BTC (gold
   source not chosen).

Next: a local backtest harness (never on the VPS) with fees, spread,
slippage, chronological train / test split and walk-forward, for 1 and 2,
before either trades on a practice account automatically.

### Verification status (honest)

| Capability | Status |
|---|---|
| Risk-based sizing | VERIFIED by unit tests |
| Protective orders confirmed on the exchange before a trade counts as protected | VERIFIED against simulated Binance and Bybit; NOT yet against the real practice hosts |
| Reconciliation, restart recovery, no duplicate orders | VERIFIED against simulated exchanges (lost answers, dropped requests, partial fills, rejections, removed stops, missing coins) |
| Trailing stops, partial exits, breakeven, time stop, invalidation exits, reasons recorded | VERIFIED by unit and simulated-exchange tests |
| Practice-account lifecycle on the real Bybit demo / Binance testnet | NOT RUN: this session's network refuses the practice hosts (HTTP 403 at its proxy), and no practice key exists yet. The tool is ready for the server. |
| Strategy-driven entries on practice accounts | NOT WIRED yet: practice trades are started only by the lifecycle tool. Strategies still run in simulated DEMO. |
| Live orders | LOCKED in code |

Resource: importing the execution code adds about 8 MB (52.7 -> 60.8 MB peak
RSS, measured locally); it is loaded only while an exchange trade is active.

## 11. Multi-strategy expansion (2026-10-16, fourth round)

The 20-module strategy library, Strategy Center, backtesting pipeline,
portfolio risk, paper / exchange-demo modes and comparison are documented in
`docs/STRATEGY_LIBRARY_REPORT.md` (research, rules, licences, tests,
migration c0004 and rollback, resources, open issues).
