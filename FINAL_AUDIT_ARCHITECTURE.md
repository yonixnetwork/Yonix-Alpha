# YonixAlpha — Architecture Map (independent audit)

Built by reading the code and running it, not from the prior phases'
claims. Where a component is named but not wired to anything, this
document says so, because "exists" and "is reachable" are different facts
and only one of them protects capital.

Companion to `FINAL_PRODUCTION_AUDIT.md`, which records what was tested
and what was found.

---

## 1. Runtime topology

13 containers (`infra/docker/docker-compose.yml`): 2 infrastructure, 2
user-facing, 9 workers.

```
                        Browser (operator, single account)
                                    |
                                    | HTTPS :443
                                    v
                    reverse-proxy (nginx, infra/nginx/)
                    - TLS termination + self-signed bootstrap
                    - HSTS/CSP/X-Frame-Options/nosniff/Referrer-Policy
                    - limit_req 5r/m on /api/auth/login -> 429
                       |                          |
                 /  (everything else)      /api/  |
                 v                                v
        web (Next.js 15, apps/web)        api (FastAPI, apps/api)
        - App Router, client components   - JWT bearer auth
        - polls REST on mount             - 19 routes, 16 auth-gated
        - NO WebSocket client             - NO WebSocket server
                                                  |
                    +-----------------------------+------------------+
                    v                                                v
            PostgreSQL 16                                        Redis 7
            durable system of record                    ephemeral/volatile
            - 15 tables, 8 migrations                   - kill switch  <-- only copy
            - Numeric() for all money                   - login lockout counters
                    ^                                           ^
                    |                                           |
        +-----------+---------------------------------+---------+
        |           |           |            |        |
   data-solana  data-binance  3x solana   decision-  ml   paper-trading
                              engines     engine          engine-binance-futures
```

### Worker cadence (measured from source)

| Service | Loop | Interval |
|---|---|---|
| `data-solana` | RPC health + WS ingest | 30s health |
| `data-binance` | REST health + WS ingest | 30s health |
| `engine-solana-discovery` | new SPL mint detection | 30s health, WS-driven |
| `engine-solana-momentum` | transfer acceleration | 30s health, WS-driven |
| `engine-solana-migration` | AMM migration detection | 30s health, WS-driven |
| `decision-engine` | evaluate candidates | **15s** |
| `paper-trading` | open + manage positions | **15s** |
| `engine-binance-futures` | order reconcile / position sync | 60s / 30s |
| `ml` | train + maybe activate | **3600s** |

The 15s cadences matter for the ML audit: `decision-engine` writes one
`ml_features` row per candidate *per cycle*, so rows are heavily
duplicated per candidate. See finding H-3.

---

## 2. The trading pipeline, and where it stops

Nominal design:

```
Data -> Features -> Signal -> Risk -> Decision -> Execution Router -> Provider
```

Actually implemented and reachable:

```
Data ---> Features ---> Signal ---> Risk ---> Decision ---> [ STOPS HERE ]
 real      real          real       real       real
                                                 |
                                                 +-> execution_router.route()
                                                     returns an enum only.
                                                     Sends nothing.
```

**There is no code path from a decision to a live order.** Verified by
grep across the repository: `place_order_idempotent()` — the only
function that can reach an exchange — has *no production caller*. Its
sole non-test reference is a docstring mention in
`services/engine-binance-futures/app/events.py`.

This is a safety property established by construction rather than by
configuration, and it is the single most important fact in this map: the
system cannot autonomously place a trade even if every flag were turned
on. It is also a completeness gap — the execution layer is not finished.

The Solana side stops earlier still: `route()` returns `UNSUPPORTED` for
every Solana candidate unless a *verified* migration parser confirmed an
AMM pool, and `engine-solana-migration` ships with zero registered
parsers. No Solana price feed exists either, so `Decision.entry` is
always `None`, which `paper-trading` refuses to trade on.

---

## 3. Entry points

| Kind | Path | Notes |
|---|---|---|
| HTTP API | `apps/api/app/main.py::create_app` | FastAPI factory; lifespan seeds the admin user |
| Web | `apps/web/app/` | App Router; `/login`, `/dashboard/*` |
| Worker × 9 | `services/*/app/main.py::run` | each `asyncio.run(run())`, SIGTERM/SIGINT aware |
| Migrations | `apps/api/migrations/` | Alembic, applied by api container on boot |

Every worker is an independent process with its own dependency closure —
they share only `packages/core-py` and the database.

---

## 4. API surface (19 routes)

Auth-gated (16) — all verified to return 401 anonymously:

```
GET  /api/auth/me
GET  /api/system/status         GET  /api/system/events
GET  /api/candidates            GET  /api/candidates/{id}
GET  /api/signals
GET  /api/risk/events           GET  /api/risk/kill-switch
POST /api/risk/kill-switch/engage
POST /api/risk/kill-switch/disengage
GET  /api/ml/models             GET  /api/ml/stats
GET  /api/paper/positions
```

Public (3): `/api/health`, `/api/health/live`, `/api/health/ready`.
Unauthenticated (2, by necessity): `/api/auth/login`, `/api/auth/refresh`.

`POST /api/risk/kill-switch/{engage,disengage}` are the **only write
endpoints in the entire API**. Everything else is read-only. The
dashboard cannot start, stop, size, or modify a trade.

### WebSocket

**None.** No server endpoint, no client. `NEXT_PUBLIC_WS_URL` is
configured and unused; the nginx CSP carried a `connect-src ... wss:`
allowance justified by a comment describing WebSocket subscriptions that
do not exist. The dashboard is poll-on-mount only. See finding L-1.

---

## 5. Data model (15 tables)

| Group | Tables | Money columns |
|---|---|---|
| Identity/audit | `users`, `sessions`, `audit_logs`, `system_events` | — |
| Market data | `tokens`, `token_events`, `market_snapshots` | `Numeric(38,18)` |
| Candidates | `trading_candidates` | — |
| Binance execution | `orders`, `fills`, `positions`, `pnl_records` | `Numeric(28,8)` |
| Decision trail | `strategy_signals`, `risk_events` | `Numeric(38,18)` |
| ML | `model_versions`, `ml_features` | — |
| Paper trading | `paper_positions` | `Numeric(38,18)` |

Every monetary quantity is `Numeric`/`Decimal` end to end. The only
`float()` in the codebase is an ML probability
(`ml/sklearn_model.py:27`), which is not money. Verified by grep and by
independent PnL recomputation — see finding V-2.

`Numeric(38,18)` cannot represent a price below 1e-18, which rounds to
exactly 0. That is reachable for a Solana memecoin quoted per raw unit
and was the root of finding C-1.

### State machine

`trading_candidates.state` + `state_history` (JSONB), transitions
enforced by `yonixalpha_core/state_machine.py::apply_transition`, which
raises on an illegal jump rather than silently allowing it:

```
DISCOVERED -> OBSERVING -> QUALIFIED -> ENTRY_PENDING -> ENTERED
           -> MANAGING -> EXIT_SIGNAL -> EXITING -> CLOSED
           (REJECTED reachable from the early states)
```

---

## 6. Where state lives, and how durable it is

| State | Store | Durability |
|---|---|---|
| Positions, orders, fills, PnL, candidates, signals, ML | PostgreSQL | durable, transactional |
| **Kill switch** | **Redis only** | **weak — see H-4** |
| Login lockout counters | Redis | acceptable to lose (fails closed: counter resets) |
| Access tokens | stateless JWT | not revocable before expiry (≤15 min) |
| Refresh tokens | PostgreSQL `sessions` | revocable, rotated per use |

The kill switch is the emergency stop the risk engine checks first and
unconditionally — and it is the one piece of safety-critical state with
no durable copy. Losing it fails *open*.

---

## 7. Risk and ML authority

`yonixalpha_core/risk.py::evaluate` is a pure function returning
`RiskVerdict(approved, reasons)`. It collects every violated check rather
than stopping at the first. Kill switch and both enable flags are checked
first and unconditionally.

ML never overrides risk: `decision-engine` blends an active model's score
into confidence, re-applies the `DEGRADED`-data cap *after* blending, and
then the risk verdict alone decides `NO_TRADE`. A malfunctioning or
missing model degrades to rule-based confidence (`NullModel`) rather than
crashing the engine.

Since the audit, a configured limit whose input the caller cannot measure
is a **rejection**, not a pass — previously `Decimal(0)` defaults made
three of the four documented limits silently unenforceable (finding H-2).

---

## 8. External dependencies

| Provider | Used by | Reachable from this sandbox |
|---|---|---|
| Solana RPC/WS (Helius etc.) | `data-solana`, 3 solana engines | No |
| Binance Futures REST/WS | `data-binance`, `engine-binance-futures` | No |
| Jupiter | nothing — no executor exists | n/a |
| Telegram Bot API | `yonixalpha_core/notify.py` | No (egress proxy returns 403) |
| Let's Encrypt | `certbot` container | No |

Every one of these is unreachable here, so all external integrations are
verified by unit tests against mocked transports and by contract
inspection — never by live traffic. That boundary is stated in the audit
report rather than papered over.
