# Runtime control plane: the dashboard controls the running system

Ordinary dashboard settings take effect in the running services without:

- SSH or a shell command;
- editing `.env`;
- a restart, rebuild or redeploy.

This document covers:

- how that works;
- how to see that it worked;
- what still needs the server.

## 1. Audit: how settings flowed before this change

| Setting | UI → API | Stored in | Read by the runtime |
|---|---|---|---|
| Global mode, strategy modes (Fresh / Migrated / Momentum ON/OFF/PAPER/MANUAL/AUTO) | `PUT /api/control/modes/...`, `PUT /api/strategies/{name}/mode` (both write the same table) | `platform_settings`, `strategy_configs` | Each evaluation: `gate_eval` → `store.load_strategy_mode` |
| Risk / safety settings: duplicate names, liquidity, tax, slippage, observation, creator history, filters | `PUT /api/control/settings/{scope}` | `risk_settings`, versioned per scope | Each evaluation: `store.load_settings(engine)` |
| Blacklist, word filters, custom rules | `/api/control/blacklist`, `/api/control/rules` | tables | Each evaluation: `pipeline.load_controls` |
| Live execution settings | `PUT /api/live/settings` | `platform_settings` | Each order / worker tick |
| Strategy parameters | `PUT /api/strategies/{name}/config` | `strategy_configs` | Each evaluation |
| RPC / provider URLs and keys | Settings → keys (host helper writes `.env`, restarts services) | `.env` | Once, at process start |

Backup RPC endpoints now belong on **System → RPC & Data Providers** (database, hot reload). The Settings → keys panel no longer lists `SOLANA_RPC_BACKUP_URL[_2|_3]`; it links there instead. If the key helper does not pick up a request within about 4 minutes, the panel says **NOT APPLIED** and names the systemd unit to check. It no longer keeps saying "waiting".

**Finding.** The database-backed settings were already read fresh on every evaluation; nothing cached them. The mismatches came from four other places.

1. **Scope override (root cause of "duplicates allowed in the dashboard, still rejected").**
   - An engine that has its own saved risk settings ignores GLOBAL completely (`load_settings` falls back to GLOBAL only when the engine has no row).
   - So editing GLOBAL did nothing for that engine.
   - The Snipe panel also saved the name filters to `solana_fresh` and `solana_migration` only, not to `solana_momentum`.
2. **RPC endpoints were process-start configuration.**
   - A saved URL only took effect after the key helper rewrote `.env` and restarted the services.
   - Until then, nothing showed that the running services still used the old one.
3. **"Saved" meant "HTTP 200".**
   - The dashboard said "Saved. Engines pick it up on their next cycle." without checking any service.
   - A stopped, failing or stale service could not be seen.
4. **A hard-coded `PAPER` badge in the top bar** ("All execution is simulated") was shown even when the global mode was LIVE.

## 2. Architecture now

```
Dashboard ──PUT/POST/PATCH/DELETE──▶ API route (validates, writes DB, commits)
                                       │
                                       ▼
                    ConfigRevisionMiddleware (apps/api/app/config_revision.py)
                    bumps platform_settings["config_revision"]  (N → N+1)
                    publishes "configuration.updated" on yx:events
                    returns X-Config-Revision: N+1
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                     ▼
RuntimeConfigWatcher in every service               Dashboard RuntimeApply
(yonixalpha_core/runtime_config.py,                 polls /api/config/health until
 runtime_watch.py)                                  every reporting service acks N+1
 - wakes on the event (≤10 s poll fallback)         → "APPLIED — runtime rev N+1"
 - runs reloaders (RPC / WS endpoint lists)         or "SAVED — RUNTIME UPDATE PENDING"
 - writes yx:config:ack:<service>                     naming the service that is behind
   {revision, loaded_at, ok, error,
    effective settings it read, RPC health}
```

- **One revision covers every settings route**, current and future. It is bumped centrally in the middleware.
- **Actions are not configuration.** Approvals, exits, manual trades and connection tests do not bump the revision.
- **Database settings take effect on each service's next evaluation cycle**, as before.
- **The acknowledgement proves the service is alive**, has read that revision and has reloaded its in-memory configuration. It also reports the effective settings it read, such as each strategy's mode and which risk scope and version applies to each engine.
- **Services that acknowledge:**
  - decision-engine;
  - engine-solana-discovery, engine-solana-momentum, engine-solana-migration;
  - paper-trading;
  - data-solana;
  - execution-futures.

### Configuration Health (System → Configuration Health, `GET /api/config/health`)

This page shows:

- The database revision, the last change and who made it.
- Each service's acknowledged revision, marked:
  - **SYNCED**;
  - **OUT OF SYNC** (older revision or failed reload);
  - **NOT REPORTING** (stopped, crashed, or disabled);
  - **NOT DEPLOYED** (the legacy engine-solana-momentum / engine-solana-migration services, which the production compose does not run; discovery and the decision engine do that work). Expected, not an error.
- Each module's stored setting against its runtime state:
  - **OFF**, **RUNNING**;
  - or **BLOCKED** with the reason, e.g. a configuration error or "no healthy RPC provider".
- The effective settings in the database side by side with what the decision engine last read: modes, the risk-settings source per engine, and the duplicate-name policy.

### Risk settings scopes (overrides, migration 0015)

- **An engine scope stores only the keys it overrides** (`{"__overrides__": {...}}`). Its effective settings are GLOBAL (or the engine's code defaults when GLOBAL was never saved) with those keys on top. A GLOBAL change therefore reaches every engine, except for the keys that engine deliberately sets differently.
- **Why:** before, saving an engine scope once (the Snipe panel did this for every Pump.fun engine) stored a full copy, and that engine ignored GLOBAL from then on. GLOBAL edits were saved and shown, but had no effect on that engine.
- **Migration 0015** converts each existing full copy into overrides of the current GLOBAL. The effective settings are identical before and after (verified on a copy of legacy rows); it only changes what later GLOBAL edits reach.
- **The page opens on GLOBAL.** Before, it opened on `solana_fresh`, so a change saved without switching the scope reached only Fresh. Momentum and Migrated kept rejecting, e.g. duplicate names.
- **Save for ALL engines** (the main button, `POST /api/control/settings-all`, one transaction):
  - writes the changed keys into GLOBAL;
  - removes those keys from every engine that had its own value;
  - so every engine uses the new value. Other engine-specific values stay.
- **Save for &lt;engine&gt; only** gives that engine its own value. The page then lists it in red.
- **GLOBAL page:** the table "Engines that use a different value than GLOBAL" shows engine, setting, engine value and GLOBAL value, with **Use GLOBAL** per row (`POST /api/control/settings/{scope}/follow-global` with `{"keys": [...]}`) and per engine.
- **Rejection alerts** end with the settings they used, e.g. `[risk settings: solana_momentum v3 over GLOBAL v7; this engine's own values: skip_duplicate_names]`.
- **Snipe panel:** saves the name filters for all engines (same endpoint). AUTO_BUY shows what each strategy mode actually does under the current global mode (PAPER strategy mode = automatic but simulated, even in global LIVE).
- **Configuration Health:** the risk source reads `GLOBAL vN`, `solana_fresh vM over GLOBAL vN`, or `… (full copy, ignores GLOBAL)`.

## 3. RPC & data providers (System → RPC & Data Providers)

- **Add:**
  - Fields: name, type, chain, RPC URL, optional WebSocket URL, priority, timeout, rate limit and notes.
  - Adding or changing a URL requires the admin password.
  - The URL is validated (`https://` / `wss://`, real host) and tested with a real `getSlot`.
  - It is stored **encrypted** in `rpc_providers` (Fernet; key from `CONFIG_ENCRYPTION_KEY` or derived from `JWT_SECRET`), and is only ever shown as `scheme://host`.
- **Edit:** URL, priority, enable/disable, timeout and rate limit. You cannot disable or delete the last enabled endpoint.
- **.env endpoints:**
  - `SOLANA_RPC_URL` and `SOLANA_RPC_BACKUP_URL[_2|_3]` appear in the same table.
  - They can be disabled or reordered from the dashboard; their URLs stay in `.env`.
  - Default priorities: `.env` primary 100, dashboard providers 150, `.env` backups 200–400.
- **Runtime:**
  - On each revision, every service's RPC manager swaps in the new ordered list with no restart. Statistics are kept for unchanged URLs.
  - WebSocket URL changes apply on the next reconnect.
- **Failover:**
  - Each request goes to the first usable endpoint. On failure or HTTP 429, the same request moves to the next one.
  - A rate-limited endpoint (429) cools down for 30 s.
  - An endpoint at its own requests-per-second limit is tried last.
  - Every change of the serving endpoint is recorded: the failover log on the page, the `rpc.failover` event, and Telegram.
- **Health is real:**
  - **CONFIGURED:** the URL decrypts.
  - **CONNECTED:** a successful request by a service in the last 5 minutes, or a successful test.
  - **HEALTHY:** the services' live view (not disabled, not rate-limited, recent success).
  - **ACTIVE:** the endpoint serving the latest successful call, per service.
  - The page also shows success and error rates, latency, the last success and failure, and the last error.
- **TEST CONNECTION** returns CONNECTED (with latency), AUTHENTICATION_FAILED, TIMEOUT, RATE_LIMITED, INVALID_CONFIGURATION or UNAVAILABLE.

## 4. Manual trading

- **BUY buttons:**
  - on every row of the safety-gate decisions for Fresh, Migrated and Momentum;
  - on both Observation tables;
  - via a "Manual BUY" mint box on each Solana strategy page.
- **Confirm Purchase** shows:
  - the token, and whether execution is LIVE or PAPER;
  - the route and migration state;
  - the current price with its source and time;
  - the available balance (live wallet or paper account);
  - the estimated amount and quantity, and the slippage limit;
  - the latest risk status and any safety blockers;
  - the maximum potential loss.
  Nothing is sent before CONFIRM BUY.
- **Execution:**
  - The request is queued (`yonixalpha_core/manual_trade.py`). The decision engine evaluates it immediately through the **same** `evaluate_with_gate` and the same `enter_live` / `paper_engine` path as automatic trading.
  - `operator_request` replaces **only** the strategy entry signal and the ML confidence floor, and your confirmation counts as the MANUAL-mode approval.
  - Every other finding still blocks: token risk, sellability, liquidity, route, stale data, wallet, sizing, kill switch and global limits.
  - The size is the gate's plan and never more.
  - A blocked buy shows the exact finding codes and messages.
- **Route:**
  - A PumpSwap pool is known → the migration engine and the pool route.
  - Otherwise → the Pump.fun bonding curve.
  - If the token migrates while the request is queued, it is refused with "press BUY again".
- **Status:**
  - QUEUED → EVALUATING → then either BLOCKED (with reasons) or PAPER POSITION OPEN.
  - For LIVE: SUBMITTING → CONFIRMING → CONFIRMED / POSITION OPEN, derived from the real order row. A failure shows its stage (e.g. SIGNING_FAILED, TRANSACTION_REJECTED).
- **SELL:**
  - On every open live position (Live Execution page) and through the existing exit control on the positions tables.
  - It uses the normal exit on the position's **current** route. `gate_manage` switches it to PumpSwap after a migration.
  - It works when no automatic exit is triggering, and the status follows the SELL order.
- **Candidates:**
  - Manual-only candidates are never picked up by the automatic loop.
  - A manual request never changes the automatic trading of other tokens.

## 5. What still needs the server

| Change | Why |
|---|---|
| Wallet private key, exchange API keys/secrets, Telegram token | Secrets stay in `.env` / server only. Provider keys can be set from Settings → keys; the host helper writes `.env` and restarts only the affected services. |
| `TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `PAPER_TRADING` locks | Deliberately server-only safety locks. |
| Database / Redis URLs, `JWT_SECRET`, admin password | Infrastructure. `JWT_SECRET` also derives the encryption key: rotating it makes stored provider URLs unreadable (they are flagged "re-enter"). |
| A service whose `.env` has no `SOLANA_RPC_URL` | It starts idle and does not run the watcher; RPC endpoints added in the dashboard reach it only once it runs. |

## 6. Verification

**Automated** (mocks / paper; no real money):

| Test file | What it checks |
|---|---|
| `packages/core-py/tests/test_runtime_config.py` | Revision and event; watcher reload and ack with effective settings; wake on event; SYNCED / OUT OF SYNC / NOT REPORTING; encryption; env + dashboard merge and priority; hot swap into a running `RpcManager`; failover log; test classification |
| `packages/core-py/tests/test_operator_request.py` | Manual BUY replaces only the signal / ML floor; token risk, stale data, liquidity model and wallet still block |
| `services/decision-engine/tests/test_manual_buy.py` | Request and manual-only candidate; paper buy executes even with the strategy OFF; token risk blocks with the reason; no data blocks; migrated route |
| `apps/api/tests/test_config_control.py` | Fresh Tokens OFF → ON through the API to the watcher ack and effective runtime value; duplicate policy and scope override; RPC add/test/edit/refusals with encryption; manual buy/sell endpoints |

**End to end (local, real services):**

- Setup: API, decision-engine, paper-trading and data-solana against Postgres and Redis, a built dashboard in Chromium, and HTTPS mock RPC servers.
- Fresh Tokens OFF → APPLIED rev 1 (3 services) in about 0.2 s; the decision engine's effective mode read OFF. Back to PAPER → rev 2, PAPER.
- ADD RPC → APPLIED rev 3; TEST CONNECTION → CONNECTED. The `.env` primary (returning HTTP 429) showed RATE LIMITED, and the new provider became ACTIVE in data-solana with no restart.
- Manual BUY:
  - with stale seeded trades → BLOCKED `NO_RECENT_ACTIVITY` (safety kept);
  - with fresh trades → PAPER POSITION OPEN.
- Manual SELL → `manual_exit`, position closed.

## 7. Settings audit (every control on Settings and Risk)

| Control | Read by the runtime | Result |
|---|---|---|
| Global / strategy modes, Snipe mode | Each evaluation (`load_global_mode`, `load_strategy_mode`) | Works |
| Risk settings | Each evaluation (`load_settings(engine)`) | Fixed: scope trap and stale engine values (above) |
| Word filters (blacklist) | Each evaluation (`load_blacklist`) | **Fixed:** ALLOW (exemption) rules were loaded as BLOCK, because the action was not read |
| Custom rules | Each evaluation (`load_custom_rules`) | Works |
| Kill switch | Each evaluation (gate `KILL_SWITCH`) | **Fixed:** a live BUY already queued when it was engaged could still be sent. The worker now cancels queued BUYs while it is engaged; SELLs (exits) continue |
| Buy amount (strategy config) | Each evaluation | Works |
| Slippage / priority fee / reserve (live settings) | Each worker loop / order | Works |
| Notification preferences | Each notification | Works |
| Fresh observation settings | Each discovery funnel run (`solana_fresh` scope) | Works |
| TRADING_ENABLED etc. | `.env`, the same file for every container | Display only. Changed on the server, never from the dashboard |

## 8. RPC load under rate limits

Symptom: `rpc.all_endpoints_failed method=getSignaturesForAddress (+149 more in 5 min)`. Causes and fixes:

- **Funding lookups.** The early-buyer funding check made up to 6 wallet lookups per candidate every 30 s. Failed lookups were not remembered, and while every endpoint was rate-limited, the emergency fallback still sent each one to every endpoint.
  - Fix: optional lookups (`call_optional`) are not sent while every endpoint is cooling down after HTTP 429, and the funding check stops at the first "all endpoints failed" instead of trying the next wallet.
  - A missing funding check was already non-blocking (`FUNDING_UNCHECKED`, reported only).
- **429 cooldown.** A provider that keeps answering 429 now rests 30 → 60 → 120 → 240 → 300 s, instead of being hit again every 30 s. The first success resets it.
- **401/403.** HTTP 401/403 for a method (wrong key, or a plan without that method, e.g. Chainstack's answer to `getSignaturesForAddress`) is remembered per endpoint and method for 10 minutes. The provider keeps serving its other methods.
- **Unchanged:** calls a trade depends on (balance, blockhash, send, confirm) still try every endpoint.
