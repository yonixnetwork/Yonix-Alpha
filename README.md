# YonixAlpha

Private, automated trading intelligence and execution platform. See
`ARCHITECTURE_AUDIT.md` and `REUSE_MATRIX.md` for the Phase 0 audit of the
reference repositories this platform draws patterns from.

## Status

**Phase 4 — Binance Futures engine.** Phases 1-3 (foundation, data
infrastructure, three Solana engines) are done. Phase 4 adds
**`services/engine-binance-futures`**: authenticated USDT-M Futures
account/order/position management, separate from Phase 2's
`data-binance` (which stays public-data-only — keeping trading
credentials confined to one service). It implements:

- **Idempotent order placement** (`app/orders.py`): a client order ID is
  generated and the order's intent committed to Postgres *before* the
  exchange is ever called (spec section 21) — if the process dies between
  submitting and hearing back, `reconcile_pending_orders()` asks Binance
  what actually happened by that client order ID on the next pass, rather
  than guessing or resubmitting.
- **Position sync + the authenticated user-data WebSocket stream**
  (`app/positions.py`, `app/user_stream.py`, `app/events.py`): REST
  reconciliation of `positions` against `GET /fapi/v2/positionRisk`, plus
  a WS client that never assumes the connection or its listenKey stay
  valid forever — a disconnect or a failed keepalive discards the cached
  listenKey and gets a fresh one on reconnect. `ACCOUNT_UPDATE` and
  `ORDER_TRADE_UPDATE` events update positions/orders and record fills
  idempotently.
- **A hard safety gate**: `place_order_idempotent()` refuses outright
  unless `TRADING_ENABLED` and `LIVE_TRADING_ENABLED` are *both*
  explicitly true — both default false. Phase 5 owns the real risk
  engine, but shipping order placement with no safety check at all would
  be irresponsible regardless of phase boundaries. Nothing in this
  codebase currently calls this function autonomously — there is no
  strategy yet (deliberately: spec section 8 requires the strategy layer
  stay independent from execution, and the shared decision system is
  Phase 5's job). This engine is the mechanical capability only.

Phase 3 added a persisted candidate state machine (`trading_candidates`,
DISCOVERED through CLOSED/REJECTED — see `yonixalpha_core.state_machine`)
and the three Solana engines the spec calls for, each an independent
service:

- **`services/engine-solana-discovery`** (Engine A) — genuinely detects
  every new SPL token mint on Solana, using the SPL Token Program's
  `initializeMint`/`initializeMint2` instructions (decoded via Solana
  RPC's `jsonParsed` transaction encoding — no hand-rolled binary
  parsing). Creates a `Token` + `TokenEvent` + a DISCOVERED
  `TradingCandidate` for each one, idempotently.
- **`services/engine-solana-momentum`** (Engine C) — tracks
  transaction-count acceleration (current 5-minute window vs. the prior
  one) for tokens already known to the system, via the Token Program's
  `transferChecked` instruction, and opens a momentum `TradingCandidate`
  when the ratio crosses a threshold.
- **`services/engine-solana-migration`** (Engine B) — **scaffolding only,
  honestly.** See "Why Engine B doesn't detect anything yet" below.

**Important limitation, disclosed rather than glossed over:** this codebase
was built in a network-restricted sandbox with no outbound access to Solana
RPC endpoints or Binance's API (only npm/pypi/github were reachable — see
`ARCHITECTURE_AUDIT.md`). Every piece of client and parsing logic — RPC
failover, WebSocket reconnect/resubscribe, transaction/HMAC-signature
parsing, state-machine transitions, acceleration math — is verified
against real local mock servers and a real local Postgres (see each
service's `tests/`), which is genuine verification of the *code*,
including the Binance signature verified against an independent HMAC
computation. But **live connectivity to the actual Solana and Binance
endpoints has not been verified** and must be checked in an environment
with real network access before this is trusted in production — that
goes double for `engine-binance-futures` given it holds real trading
credentials once configured. Two more scope notes from Phase 2 still
apply: `data-solana` stores raw, undecoded Solana notifications (Engine
A/C now do the decoding, using the universal SPL Token Program, not any
launch-platform-specific one); `data-binance` stays public-data-only, no
API key needed — Phase 4 added a *separate* service for authenticated
calls rather than adding credentials to that one.

### Why Engine B doesn't detect anything yet

Engine A and C can parse SPL Token Program instructions generically because
*every* Solana token uses that one, single, foundational interface. There
is no equivalent for "a liquidity pool was created" — each AMM (Raydium,
Orca, Meteora, ...) has its own program ID and its own, incompatible
instruction layout. Writing a parser for one without being able to verify
its current program ID and instruction format against live documentation
would mean fabricating trading-relevant logic, which the spec is explicit
must never happen. So `engine-solana-migration` ships a real, tested
dispatch mechanism (`app/detect.py`: `register_parser(program_id, parser_fn)`)
with an empty registry — an operator who has verified a specific AMM's
details registers a parser for it and detection activates with no other
code changes. Until then, the service runs, reports its health, and
correctly does nothing else. See `services/engine-solana-migration/README.md`.

No order has ever been placed by this codebase and none will be by
default: `TRADING_ENABLED`/`LIVE_TRADING_ENABLED` default false, the three
Solana engines only discover and persist candidates, and nothing calls
`engine-binance-futures`'s order-placement function autonomously — there
is no strategy or decision layer wired up to call it yet. See the phase
list in the original spec for what comes next (the shared risk/decision
system that actually evaluates candidates and would be the first thing to
call it, ML, paper trading, the full dashboard, deployment, hardening).

## Repository layout

```
apps/
  api/                       FastAPI backend (Python 3.12, SQLAlchemy async, Alembic, Redis)
  web/                       Next.js dashboard (TypeScript, App Router)
services/
  data-solana/               Solana RPC/WS ingestion worker (public data)
  data-binance/              Binance Futures market-data ingestion worker (public data)
  engine-solana-discovery/   Engine A: new SPL mint detection
  engine-solana-momentum/    Engine C: transfer-acceleration detection
  engine-solana-migration/   Engine B: scaffolding, no live detection yet (see Status)
  engine-binance-futures/    Authenticated account/order/position engine (Phase 4)
packages/
  core-py/                   Shared config, logging, security, DB models/schemas,
                              Solana RPC/WS transport, SPL Token Program parsing
                              (yonixalpha_core)
infra/
  docker/                    docker-compose.yml + dev/prod overrides
  nginx/                     reverse proxy config (HTTP only until TLS is provisioned)
```

Every Python service depends on `packages/core-py` via an editable pip
install (`-e ../../packages/core-py` in each `requirements.txt`) — this is
why every Python service builds from the **repo root** as its Docker
context (see each Dockerfile's first line) rather than its own directory.
`packages/core-py` now also owns the Solana RPC manager, WebSocket client,
and SPL Token Program parsing (`yonixalpha_core.solana.*`) — introduced
there rather than duplicated once the three Solana engines all needed the
same transport and parsing logic `data-solana` already had.

## Local development

Requires Docker + Docker Compose. Postgres and Redis run in containers; the
API and web app hot-reload from your working tree.

1. `cp .env.example .env`
2. Fill in `JWT_SECRET` (`openssl rand -hex 32`), `POSTGRES_PASSWORD`, and
   `ADMIN_PASSWORD_HASH` (see below). Leave `SOLANA_RPC_URL` /
   `SOLANA_WS_URL` / `BINANCE_SYMBOLS` / `BINANCE_API_KEY` blank to keep
   `data-solana`, `data-binance`, all three `engine-solana-*` workers, and
   `engine-binance-futures` idle (they check for these and no-op if unset,
   logging why, rather than crashing); fill them in once you've confirmed
   the endpoint URLs against current docs (see the Status section above).
   `engine-solana-migration` additionally needs `MIGRATION_AMM_PROGRAM_IDS`
   and a registered parser before it does anything at all — see its own
   README. `engine-binance-futures` additionally needs
   `TRADING_ENABLED=true` and `LIVE_TRADING_ENABLED=true` before it will
   place a real order, even with valid API credentials configured — see
   the Status section above.
3. From the repo root:
   ```
   docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.dev.yml up --build
   ```
4. API: http://localhost:8000/api/health · Web: http://localhost:3000

### Generating an admin password hash

```
cd apps/api && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -c "from yonixalpha_core.security import hash_password; print(hash_password('your-password'))"
```

Paste the output into `ADMIN_PASSWORD_HASH` in `.env`. The plaintext password
is never stored anywhere. Changing the hash and restarting the API rotates
the admin password (see `app/main.py::_seed_admin_user`).

### Running the backend without Docker

```
cd apps/api
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head          # requires a running Postgres; see DATABASE_URL
uvicorn app.main:app --reload
```

### Running the backend test suite

Tests run against a real Postgres and Redis (not sqlite/fakeredis, so the
schema's JSONB/UUID/INET types are exercised as written):

```
cd apps/api && . .venv/bin/activate
pytest tests/ -v
```

`tests/conftest.py` expects `postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test`
and `redis://localhost:6379/15` by default — override via `DATABASE_URL` /
`REDIS_URL` env vars, or create that role/database locally.

### Running a data-ingestion or engine service without Docker

Same pattern for `services/data-solana`, `services/data-binance`, all
three `services/engine-solana-*`, and `services/engine-binance-futures`:

```
cd services/engine-solana-discovery     # or any of the other five
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app tests && pytest tests/ -v   # no live network needed — mocked transports/local WS server/real Postgres
python -m app.main                         # the actual worker; needs real SOLANA_RPC_URL etc. in the environment
```

### Running packages/core-py's own tests

```
cd packages/core-py
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check yonixalpha_core tests && pytest tests/ -v
```

Covers the state machine and SPL Token Program parsing directly — these are
pure-logic tests (no DB/network needed) since the shared package owns the
logic every consuming service's own tests then build on.

### Running the frontend without Docker

```
cd apps/web
npm install
npm run dev
```

## Production deployment

Not yet documented — this lands with Phase 9 (`docs/DEPLOYMENT.md`), once
there's an engine worth deploying. The `docker-compose.prod.yml` override and
`infra/nginx/nginx.conf` are a starting skeleton (no TLS cert provisioning
wired up yet — that requires the live `yonixalpha.com` DNS pointed at the
droplet first).

## Security notes

- `.env` is never committed (see `.gitignore`); only `.env.example` is.
- Login is rate-limited: 5 failed attempts locks the account out for 15
  minutes (`apps/api/app/api/routes/auth.py`).
- Refresh tokens rotate on every use and are individually revocable
  (`sessions` table) — a leaked refresh token is usable exactly once.
- Passwords are hashed with argon2 (`passlib`), never stored or logged in
  plaintext.
- See `ARCHITECTURE_AUDIT.md` §3 for what was verified clean (and what
  wasn't) in the five reference repositories this codebase draws patterns
  from.
