# Deployment

This is the runbook for running YonixAlpha on a real server — a
DigitalOcean droplet at `yonixalpha.com` per the original spec, though
nothing here is DigitalOcean-specific beyond "a fresh Ubuntu 24.04 box you
have root on."

**Honesty check before anything else**: this codebase was built in a
network-restricted sandbox with no access to a real droplet, real DNS, or
even Docker Hub (image pulls are blocked here — see below). Every piece of
this phase that *can* be verified without those things has been —
`docker compose config` validated, the real nginx binary tested live
against the actual `nginx.conf` (TLS termination, the ACME challenge
location, and both proxy paths all confirmed working end-to-end), every
certbot flag checked against the real, current certbot CLI, every shell
script passed through shellcheck. What hasn't been verified is anything
that needs a real droplet, real DNS, or a real Docker Hub pull: actually
provisioning a server, actually pointing DNS at it, and actually obtaining
a Let's Encrypt certificate. Section "What's genuinely verified" below is
explicit about the line.

## 1. Prerequisites

- A droplet running Ubuntu 24.04, with a public IP, that you have root
  SSH access to.
- `yonixalpha.com` and `www.yonixalpha.com` DNS A records pointed at that
  IP (needed before the TLS bootstrap step, not before).
- A way for the droplet to read this repository. **It is private** —
  verified: `https://github.com/yonixnetwork/Yonix-Alpha` returns 404
  unauthenticated, and so does every `raw.githubusercontent.com` path
  under it. An earlier version of this document claimed the repo was
  public and no deploy key was needed; that was wrong, and it made the
  bootstrap step below impossible to run as written. Use a **read-only
  deploy key** (step 2.1).

## 2. First deploy

### 2.1 Bootstrap the server

Because the repository is private, there is no `curl … | bash` one-liner:
`raw.githubusercontent.com` will 404 without credentials. Give the droplet
a read-only deploy key first, then run the script from the clone.

```
ssh root@<droplet-ip>

# 1. Generate a key ON THE DROPLET (the private half never leaves it)
ssh-keygen -t ed25519 -C "yonixalpha-droplet" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Add that public key at **GitHub → the repo → Settings → Deploy keys →
Add deploy key**. Leave "Allow write access" **unchecked** — the droplet
only ever needs to read. Then:

```
ssh -T git@github.com          # accept the host key; "successfully authenticated" is expected
git clone git@github.com:yonixnetwork/Yonix-Alpha.git /opt/yonixalpha
cd /opt/yonixalpha
YONIXALPHA_REPO_URL=git@github.com:yonixnetwork/Yonix-Alpha.git bash scripts/bootstrap-server.sh
```

`scripts/bootstrap-server.sh` is idempotent, so running it against the
clone it is already sitting in is fine. `YONIXALPHA_REPO_URL` is what
keeps `scripts/deploy.sh` pulling over SSH later; `YONIXALPHA_DEPLOY_BRANCH`
(default `main`) and `YONIXALPHA_REPO_DIR` (default `/opt/yonixalpha`)
are overridable the same way.

This installs Docker Engine + the compose plugin, clones the repo into
`/opt/yonixalpha`, and opens only 22/80/443 in `ufw`. It does **not**
create `.env`, obtain TLS certs, or start anything — see
`scripts/bootstrap-server.sh` for exactly what it does and doesn't do.

### 2.2 Configure secrets

```
cd /opt/yonixalpha
cp .env.example .env
```

Fill in every value `.env.example` leaves blank. Where each one comes from:

| Variable | How to generate/obtain it |
|---|---|
| `JWT_SECRET` | `openssl rand -hex 32` |
| `ADMIN_PASSWORD_HASH` | `python3 -c "from passlib.hash import argon2; print(argon2.hash('your-password'))"` — **never** put the plaintext password itself in `.env` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` (or any strong random string — this database is never exposed to the host in production, see `docker-compose.prod.yml`) |
| `REDIS_PASSWORD` | `openssl rand -hex 24` |
| `SOLANA_RPC_URL` / `SOLANA_WS_URL` (+ backups) | A real RPC provider (Helius, Triton, etc.) — verify the endpoint against current provider docs before use, per `ARCHITECTURE_AUDIT.md` |
| `HELIUS_API_KEY` / `HELIUS_WEBHOOK_SECRET` | From your Helius account, if using Helius |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | A Binance API key scoped to Futures trading only — leave `BINANCE_TESTNET=true` until you've verified the integration end-to-end |
| `LETSENCRYPT_EMAIL` | Any address you control — Let's Encrypt uses it for expiry warnings |

**A real bug this project hit once, worth repeating here**: `.env` values
like `ADMIN_PASSWORD_HASH` contain literal `$` characters. Never `source
.env` in a shell — bash tries to expand `$argon2id`, `$v`, etc. as
variables and silently corrupts the value. Docker Compose's own
`env_file:` directive does NOT have this problem (it reads the file
literally, no shell involved), which is why every script here goes
through `docker compose exec`/`env_file` rather than sourcing `.env`
directly (see `scripts/backup-db.sh`'s own comment on this).

### 2.3 Bring the stack up

```
docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --build
```

`apps/api`'s own container runs `alembic upgrade head` before starting —
migrations are automatic, not a separate step. `reverse-proxy` generates a
short-lived self-signed certificate on first boot if no real one exists
yet (see `infra/nginx/docker-entrypoint.sh`), so the whole stack comes up
immediately — the app is reachable over HTTPS right away, just with a
browser certificate warning until the next step.

Check everything is healthy:

```
docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml ps
```

### 2.4 Obtain a real TLS certificate

Only after DNS for `yonixalpha.com`/`www.yonixalpha.com` actually points
at this server:

```
docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml \
  run --rm certbot certonly --webroot -w /var/www/certbot \
  -d yonixalpha.com -d www.yonixalpha.com \
  --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email

docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml restart reverse-proxy
```

The `certbot` service defined in `docker-compose.prod.yml` then renews
automatically (checks every 12h; Let's Encrypt certs are valid 90 days,
so this is a no-op the overwhelming majority of the time). Renewal alone
doesn't reload nginx — run the same `restart reverse-proxy` above after a
renewal if you're not comfortable waiting for the next natural restart
(nginx keeps serving the old cert fine until then; it isn't expired).

## 3. Subsequent deploys

```
cd /opt/yonixalpha
scripts/deploy.sh
```

Fast-forward-pulls the current branch, rebuilds changed images, brings
the stack up, and waits for `api` to report healthy before exiting.
Fails loudly (non-zero exit) rather than silently leaving a broken
deploy running — see the script for exact behavior.

## 4. Rollback

```
cd /opt/yonixalpha
git fetch origin
git checkout <previous-known-good-sha>
docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --build
```

A migration that ran as part of the bad deploy does not automatically
roll back — check `alembic history` and `alembic downgrade` manually if
the previous version's schema assumptions actually changed (most
migrations in this codebase only ever add tables/columns, so this is
rarely necessary — check the specific migration file to be sure).

## 5. Backups

```
scripts/backup-db.sh
```

Dumps Postgres (via `docker compose exec`, reading credentials from the
container's own environment — never by parsing `.env` on the host, for
the `$`-in-values reason above) to a gzipped, timestamped file under
`/opt/yonixalpha-backups` (override with `YONIXALPHA_BACKUP_DIR`), and
prunes anything older than 14 days (override with
`YONIXALPHA_BACKUP_RETENTION_DAYS`). Run it daily via cron:

```
0 3 * * * /opt/yonixalpha/scripts/backup-db.sh >> /var/log/yonixalpha-backup.log 2>&1
```

Restoring: `gunzip -c <file>.sql.gz | docker compose ... exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"`.

## 6. Monitoring

- **The dashboard itself** (Phase 8): `/dashboard` shows real per-service
  status, the kill switch, and every candidate/signal/risk/ML/paper-trading
  table — this is the first place to look, not raw logs.
- **Container logs**: `docker compose -f ... -f ... logs -f <service>`.
- **`GET /api/system/events`**: the same data the dashboard's System
  Events page shows, queryable directly.

## 7. Enabling live trading

`TRADING_ENABLED` and `LIVE_TRADING_ENABLED` in `.env` both default
`false` and must be independently, deliberately set `true` — the risk
engine checks both unconditionally before anything executes (see
`packages/core-py/yonixalpha_core/risk.py`). Before ever setting either
to `true` on a production `.env`:

1. Set `MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`, `MAX_SLIPPAGE`,
   `MAX_OPEN_POSITIONS` to real, deliberately-chosen limits — they default
   unset (unenforced), which is fine for observing the system but not for
   risking real capital.

   **Know which of these can actually fire today** (a limit that silently
   cannot enforce is worse than no limit, so this is stated plainly
   rather than left to be discovered):

   | Limit | Status | Why |
   |---|---|---|
   | `MAX_OPEN_POSITIONS` | **Enforced** | `decision-engine` counts real open positions. |
   | `MAX_POSITION_SIZE` | **Blocks all trading if set** | No position-sizing algorithm exists, so the proposed size is unknown. Since the audit, an unknown input to a *configured* limit is a rejection, not a pass (`risk.py`) — so setting this makes every decision `NO_TRADE` until sizing is implemented. That is deliberate: it fails closed and visibly, instead of appearing to cap size while capping nothing. |
   | `MAX_DAILY_LOSS` | **Blocks all trading if set** | Same reason: no realized-PnL feed, so today's PnL is unknown. |
   | `MAX_SLIPPAGE` | **Not wired** | Deliberately not mapped into `RiskConfig` — it is a fraction, not basis points, and no Solana quote/slippage feed exists to compare against, so a unit conversion here would be unverified. It has no effect either way. |

   In other words: on today's codebase the only limit that both applies
   and permits trading is `MAX_OPEN_POSITIONS`. The rest are honest
   blockers until the missing measurements exist.
2. Confirm `BINANCE_TESTNET=false` only once you've watched a full
   `engine-binance-futures` cycle succeed against testnet.
3. Know where the kill switch is (`/dashboard` → Overview → Kill Switch)
   before you need it, not after.
4. Read `docs/ML.md` and `docs/PAPER_TRADING.md` — as of this phase, no
   code path in this codebase reaches a `LONG` decision or opens a
   position at all (Solana has no price feed to act on), so enabling
   trading today has no practical effect until that changes. This is
   disclosed, not hidden.

## 8. Continuous integration

`.github/workflows/ci.yml` runs on every push and PR: lint + test for
every Python project (in a matrix, against real Postgres/Redis service
containers — not mocked), an Alembic migration round-trip check for
`apps/api`, and typecheck/lint/build for the frontend. It runs on GitHub's
own infrastructure, which has full internet access unlike this sandbox —
image pulls that fail here (see below) work fine there.

## What's genuinely verified vs. not

**Verified for real, in this session:**
- `docker compose config` for every combination of base/dev/prod overlays.
- `nginx.conf`'s actual logic: built + ran the real nginx binary (via apt,
  which — unlike Docker Hub — is reachable in this sandbox) against the
  exact config file, with fake upstreams standing in for `api`/`web`.
  Confirmed live: the ACME HTTP-01 challenge location serves a file, port
  80 redirects everything else to HTTPS, and port 443 terminates TLS and
  correctly proxies both `/api/` and `/` — using the same self-signed-cert
  bootstrap `docker-entrypoint.sh` generates for real.
- Every certbot flag in this doc and in `docker-compose.prod.yml`
  (`certonly --webroot -w ... -d ... --email ... --agree-tos
  --no-eff-email`, `renew --webroot -w ... --quiet`) checked against the
  real, current certbot CLI (installed via pip, which is reachable here).
- Every shell script in `scripts/` and `infra/nginx/docker-entrypoint.sh`
  passed `shellcheck` clean.

**Not verified, and cannot be from this sandbox:**
- Actually pulling `nginx:1.27-alpine`, `postgres:16-alpine`,
  `redis:7-alpine`, `certbot/certbot`, or `python:3.12-slim` — Docker Hub
  is unreachable here (every pull attempt returns a `403 Forbidden` from
  the registry's CDN), so no container in this repo has ever actually
  been built or run as a container in this environment. `.github/workflows/ci.yml`
  running on GitHub's own infrastructure is the first real test of that —
  watch its Actions run for this phase's commit to see the result.
- Actually provisioning a droplet, pointing real DNS at it, or obtaining a
  real Let's Encrypt certificate — `scripts/bootstrap-server.sh` and the
  certbot bootstrap command in section 2.4 are correct as written and
  individually verified where possible, but the end-to-end sequence on a
  real box is, honestly, untested by this session.
