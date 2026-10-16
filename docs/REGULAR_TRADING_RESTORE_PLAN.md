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
| Backtesting | NOT IMPLEMENTED | to run locally, never on the VPS |
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
