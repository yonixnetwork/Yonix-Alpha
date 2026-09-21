# YonixAlpha

Private, automated trading intelligence and execution platform. See
`ARCHITECTURE_AUDIT.md` and `REUSE_MATRIX.md` for the Phase 0 audit of the
reference repositories this platform draws patterns from.

## Status

**Phase 2 — Data infrastructure.** Phase 1 (foundation: config, auth, DB,
Docker, dashboard shell) is done. Phase 2 adds: a shared Python package
(`packages/core-py`) so every service uses the same DB models instead of
drifting copies; two new background workers, `services/data-solana` and
`services/data-binance`, that ingest raw market data into a normalized
`market_snapshots` table; and Solana RPC/WebSocket infrastructure with
primary/backup failover and health scoring.

**Important limitation, disclosed rather than glossed over:** this codebase
was built in a network-restricted sandbox with no outbound access to Solana
RPC endpoints or Binance's API (only npm/pypi/github were reachable — see
`ARCHITECTURE_AUDIT.md`). Every piece of client logic — RPC failover,
WebSocket reconnect/resubscribe, message parsing — was verified against
real local mock servers (see each service's `tests/`), which is genuine
verification of the *code*, but **live connectivity to the actual Solana and
Binance endpoints has not been verified** and must be checked in an
environment with real network access before this is trusted in production.
Two related, deliberate scope decisions:
- `services/data-solana` does not decode any launch-platform-specific
  program data (e.g. pump.fun instruction layouts) — that requires a
  verified, current program ID and instruction format this environment
  couldn't check. It ingests generic, unambiguous Solana WS notifications
  (`slotSubscribe` always; `logsSubscribe` for an operator-configured
  address list) and stores them raw. Program-specific interpretation is
  Phase 3's job, once built against verified data.
- `services/data-binance` targets USDT-M Futures **public market data**
  only (klines/trades/depth/mark price) — no API key needed. Authenticated
  account/order endpoints are Phase 4's scope.

No trading engine exists yet — `TRADING_ENABLED` and `LIVE_TRADING_ENABLED`
are hardcoded false-by-default and nothing in this codebase can place an
order. See the phase list in the original spec for what comes next (the
three Solana engines, the Binance Futures engine, the shared risk/decision
system, ML, paper trading, the full dashboard, deployment, hardening).

## Repository layout

```
apps/
  api/            FastAPI backend (Python 3.12, SQLAlchemy async, Alembic, Redis)
  web/            Next.js dashboard (TypeScript, App Router)
services/
  data-solana/    Solana RPC/WS ingestion worker
  data-binance/   Binance Futures market-data ingestion worker
packages/
  core-py/        Shared config, logging, security, DB models/schemas (yonixalpha_core)
infra/
  docker/         docker-compose.yml + dev/prod overrides
  nginx/          reverse proxy config (HTTP only until TLS is provisioned)
```

Every Python service depends on `packages/core-py` via an editable pip
install (`-e ../../packages/core-py` in each `requirements.txt`) — this is
why `apps/api`, `services/data-solana`, and `services/data-binance` all
build from the **repo root** as their Docker context (see each Dockerfile's
first line) rather than their own directory.

## Local development

Requires Docker + Docker Compose. Postgres and Redis run in containers; the
API and web app hot-reload from your working tree.

1. `cp .env.example .env`
2. Fill in `JWT_SECRET` (`openssl rand -hex 32`), `POSTGRES_PASSWORD`, and
   `ADMIN_PASSWORD_HASH` (see below). Leave `SOLANA_RPC_URL` /
   `SOLANA_WS_URL` / `BINANCE_SYMBOLS` blank to keep the two data-ingestion
   workers idle (they check for these and no-op if unset, logging why,
   rather than crashing); fill them in once you've confirmed the endpoint
   URLs against current docs (see the Status section above).
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

### Running a data-ingestion service without Docker

Same pattern for both `services/data-solana` and `services/data-binance`:

```
cd services/data-solana                 # or services/data-binance
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app tests && pytest tests/ -v   # no live network needed — mocked transports/local WS server
python -m app.main                         # the actual worker; needs real SOLANA_RPC_URL etc. in the environment
```

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
