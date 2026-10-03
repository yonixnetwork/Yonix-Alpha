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
| `HELIUS_API_KEY` | From your Helius account, if using Helius (the RPC/WS URLs are derived from it when not set) |
| `WALLET_PRIVATE_KEY` / `WALLET_PUBLIC_KEY` | Only for live Pump.fun execution: a dedicated trading-only wallet holding only what you can lose. See `docs/CONFIGURATION.md` |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | A Binance API key scoped to Futures trading only — leave `BINANCE_TESTNET=true` until you've verified the integration end-to-end |
| `LETSENCRYPT_EMAIL` | Any address you control — Let's Encrypt uses it for expiry warnings |

**A real bug this project hit, worth repeating here**: `.env` values like
`ADMIN_PASSWORD_HASH` contain literal `$` characters. Never `source .env`
in a shell — bash tries to expand `$argon2id`, `$v`, etc. as variables and
silently corrupts the value.

The previous version of this doc claimed Docker Compose's own `env_file:`
directive was immune to this because "it reads the file literally, no
shell involved." That claim was **wrong** — verified false against a real
`docker compose config` run: Compose's `env_file:` parser performs the
same `$VAR`/`${VAR}` interpolation the shell does, so an unescaped
`$argon2id$v=19$m=65536,t=3,p=4$...` hash is silently mangled into garbage
(`argon2id`, `v`, `m`, and the salt are all undefined variables that
resolve to `""`) with only a warning, not an error — the API then loads a
broken hash and every login attempt fails with no indication why.

**The fix**: escape every literal `$` in `.env` as `$$` for any value that
contains one (in practice, only `ADMIN_PASSWORD_HASH` — `openssl rand
-hex` output never contains `$`). For example:
```
ADMIN_PASSWORD_HASH=$$argon2id$$v=19$$m=65536,t=3,p=4$$fq91zrn3PgfgXCtFaC3lHA$$6qC7uRla6Z7eINxUTjwqlYvc/V4/9osHwWL0p2x9IEo
```
Verify it round-tripped correctly before starting the stack:
```
docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml config | grep ADMIN_PASSWORD_HASH
```
The printed value should show `$$` at each escaped position (Compose's
own output re-escapes literal `$` this way) — if you see bare `argon2id`,
`v`, `m` with no leading `$`, or "variable is not set" warnings when you
ran `up`, it wasn't escaped and login will fail.

### 2.3 Bring the stack up

**A second real bug, found the first time this ever ran a real `docker
build`** (the sandbox that built this codebase has no Docker Hub access —
see "What's genuinely verified" below — so this specific step was never
actually exercised until a real droplet/PC ran it): `apps/web`'s
`NEXT_PUBLIC_API_URL` (the realtime WebSocket URL is derived from it;
there is no separate WS variable) is inlined into the client
JS bundle by `next build` itself, at *build* time — `env_file:` only
reaches the *running* container, too late to affect what already got
baked into the bundle. Compose's own `${VAR}` interpolation (what
`build.args` needs to thread them into the build) has a separate,
easy-to-miss gotcha: by default it resolves against a `.env` next to the
*first `-f` file* (`infra/docker/`), not the repo root where `.env`
actually lives — so without `--env-file`, `NEXT_PUBLIC_API_URL` silently
resolves to `""` at build time regardless of what's in the real `.env`,
and the deployed frontend calls relative/broken URLs instead of your real
domain. Verified against a real `docker compose config` run: the
`environment:` block (from `env_file:`) shows the correct value while
`build.args` showed `""` until `--env-file .env` was added. **Always
include `--env-file .env`**, run from `/opt/yonixalpha` (or wherever you
cloned to):

```
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --build
```

`apps/api`'s own container runs `alembic upgrade head` before starting —
migrations are automatic, not a separate step. `reverse-proxy` generates a
short-lived self-signed certificate on first boot if no real one exists
yet (see `infra/nginx/docker-entrypoint.sh`), so the whole stack comes up
immediately — the app is reachable over HTTPS right away, just with a
browser certificate warning until the next step.

Check everything is healthy:

```
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml ps
```

### 2.4 Obtain a real TLS certificate

Only after DNS for your domain and its `www.` subdomain actually point at
this server:

**A third real bug, found running this against a real droplet for the
first time**: the `certbot` service in `docker-compose.prod.yml` sets a
fixed `entrypoint:` (the `certbot renew` auto-renewal loop, for the
`docker compose up` case) that does not forward `"$@"` — so a plain
`docker compose run --rm certbot certonly ...` silently ignores the
`certonly ...` command entirely and just starts another copy of the
renewal loop instead (confirmed with `docker ps`: the resulting
container's own command was the loop, not `certonly`). Verified fix:
override the entrypoint back to the plain `certbot` binary for this one
invocation with `--entrypoint certbot` — this does not affect the
persistent `certbot` service's own renewal loop from `docker compose up`,
only this one-off `run`.

First, remove the stray container from any earlier `run` attempt made
before this fix (harmless — it's just another idle renewal loop, but no
reason to leave it around):
```
docker ps -a --filter "name=yonixalpha-certbot-run" --format '{{.ID}}' | xargs -r docker rm -f
```

**A fourth real bug, hit immediately after fixing the third**: `certbot
certonly` then fails with `live directory exists for yonixalpha.com`.
This is `infra/nginx/docker-entrypoint.sh` — it writes a temporary
self-signed placeholder to `/etc/letsencrypt/live/yonixalpha.com/` on
`reverse-proxy`'s very first boot (so nginx has *something* to bind to
before a real certificate exists), at the exact path Let's Encrypt uses.
Certbot sees that directory already populated and, since there's no
matching `/etc/letsencrypt/renewal/yonixalpha.com.conf` proving it's a
lineage certbot itself manages, refuses to touch it rather than guess.
Delete the placeholder first — it's disposable, not a real certificate:
```
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml \
  run --rm --entrypoint sh certbot -c "rm -rf /etc/letsencrypt/live/yonixalpha.com /etc/letsencrypt/archive/yonixalpha.com /etc/letsencrypt/renewal/yonixalpha.com.conf"
```
(If your domain isn't literally `yonixalpha.com`, note `docker-entrypoint.sh`
currently hardcodes that name for the placeholder regardless of your real
domain — substitute `yonixalpha.com` in the command above with whatever
that file's `DOMAIN_PRIMARY` says, not your own domain.)

Then:
```
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml \
  run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
  -d yourdomain.com -d www.yourdomain.com \
  --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email

docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml restart reverse-proxy
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

Images are built one service at a time, each retried up to three times:
on the 2 vCPU / 2 GB server a parallel build of all ten images ran past
BuildKit's deadline (`failed to solve: Internal: context deadline
exceeded`) and nothing was deployed. A build that still fails leaves the
running stack untouched, and the script says so; the deploy is complete
only when it prints `==> Deploy complete: <commit>`. On a bigger server,
`DEPLOY_PARALLEL_BUILD=1 scripts/deploy.sh` builds in parallel again.

### 3.1 Keeping up to date (update notifications)

The update monitor (System Health -> Research / Updates, and Telegram)
only reports. A deploy installs exactly what the repository pins, so
running `scripts/deploy.sh` again does **not** pick up a newer release.
The "What to do" column of each update tells which of four cases it is:

| What to do | Meaning | How it reaches the server |
|---|---|---|
| APPLIED | the server already runs that version | nothing; acknowledge |
| PIN BUMP | a newer release of a pinned Python dependency | a dependency-update pull request (the pin changes and every test runs against the new version), merged after CI is green, then `scripts/deploy.sh` |
| INTEGRATION CHECK | a repository whose IDL / ABI / API YonixAlpha reads changed a file it uses | the change is reviewed against the integration; a code change, if needed, comes as a pull request, then a deploy |
| REVIEW ONLY | a repository YonixAlpha does not install | read and acknowledge; nothing to deploy |

Operating-system security patches in the base images (Python, Node,
nginx, Postgres 16, Redis 7) are picked up with:

```
DEPLOY_PULL=1 scripts/deploy.sh
```

It pulls the newest image of the same major version and rebuilds without
the cached base layers. Once a month is enough; it changes no Python or
npm dependency.

A routine that works:
- **When a notification arrives:** open Research / Updates and read its
  "What to do". SECURITY UPDATE or ACTION REQUIRED goes first: ask for
  the pull request the same day.
- **Once a week:** ask for a dependency-update pull request covering every
  open PIN BUMP. Major versions are trialled against the whole test suite
  first and held back, with the reason, when a behaviour change cannot
  be cleared in tests (SQLAlchemy 2.1 was held back on 2026-10-03 for this
  reason).
- **After merging:** deploy. The update's watch then reads UP TO DATE.
- **Once a month:** `DEPLOY_PULL=1 scripts/deploy.sh`.

Nothing is ever upgraded or deployed automatically (master §66).

## 4. Rollback

```
cd /opt/yonixalpha
git fetch origin
git checkout <previous-known-good-sha>
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --build
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
