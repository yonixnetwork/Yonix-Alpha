# Security

YonixAlpha is a **private, single-operator** trading platform — there is
exactly one user account (`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`), and it is
never intended to be exposed to untrusted users. That shapes every decision
below: this document is a threat model and a set of controls for "how do I
stop an attacker who isn't the operator from doing damage," not a
multi-tenant authorization design (there are no roles, no other users, no
per-resource permissions to get wrong).

This document has been referenced from `config.py`, `db/models.py`, and
`docs/API.md` since Phase 1; this is the first phase it's actually written.

## 1. Authentication

- **Argon2** password hashing (`passlib`'s `argon2` scheme,
  `packages/core-py/yonixalpha_core/security.py`) for the single admin
  account. The plaintext password is never stored or logged — only
  `ADMIN_PASSWORD_HASH` lives in `.env`, generated once via
  `passlib.hash.argon2.hash(...)` (see `docs/DEPLOYMENT.md`).
- **JWT access + refresh tokens** (`HS256`, `JWT_SECRET` — a random 32+ byte
  secret enforced by a `pydantic` validator in `config.py`). Access tokens
  are short-lived (`ACCESS_TOKEN_TTL_MINUTES`, default 15); refresh tokens
  are long-lived (`REFRESH_TOKEN_TTL_DAYS`, default 7) and **rotate on every
  use** (`apps/api/app/api/routes/auth.py::refresh` revokes the used token
  and issues a new one), so a leaked refresh token is useful for at most one
  refresh cycle before rotation invalidates it.
- **Refresh token revocation** is tracked server-side (`Session` table,
  `refresh_token_jti` + `revoked_at`), not just by expiry — `logout` and
  rotation both revoke explicitly, so a compromised token can be cut off
  without waiting for it to expire.
- **Account lockout**: 5 failed attempts within 15 minutes locks the
  *username* (not IP) for 15 minutes, tracked in Redis
  (`auth:failed:{username}` / `auth:lockout:{username}` in `auth.py`).
  Failed attempts also don't leak whether the username exists — same 401
  either way.
- **Rate limiting** (Phase 10, `infra/nginx/nginx.conf`): `/api/auth/login`
  is additionally rate-limited per-IP at the nginx layer (5 requests/minute,
  burst of 5, HTTP 429 once exhausted) — defense-in-depth in front of the
  app-level lockout above, so a distributed attempt against many usernames
  from one IP is still throttled even though the app-level lockout is
  per-username.
- Every login, logout, and kill-switch action is written to the `audit_logs`
  table with the actor, IP, and a JSON detail blob (see section 4).

## 2. Authorization

There is one authenticated principal. Every route under `/api/` other than
`/api/auth/login` and `/api/auth/refresh` requires a valid access token
(`Depends(get_current_username)`); there is no separate admin/read-only
split because there is no second user to grant a lesser role to. If a
second operator account is ever added, this is the first thing that needs
to change — see "Known limitations" below.

## 3. Trading safety controls

Independent of authentication, `packages/core-py/yonixalpha_core/risk.py`
enforces:

- **`TRADING_ENABLED`** and **`LIVE_TRADING_ENABLED`** must both be `true`
  before any order reaches an exchange — they default `false` and are
  checked unconditionally on every decision, not just at startup.
- **The kill switch** (`yonixalpha_core/kill_switch.py`, backed by Redis) is
  checked first, ahead of every other risk check — engaging it blocks all
  new orders regardless of any other setting. It's engaged/disengaged only
  via authenticated `/api/risk/kill-switch/{engage,disengage}` calls (see
  `apps/api/app/api/routes/risk.py`), each of which writes an audit log
  entry and a `WARNING`-level log line.
- **Position/loss limits** (`MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`,
  `MAX_SLIPPAGE`, `MAX_OPEN_POSITIONS`) default unset (unenforced) — see
  `docs/DEPLOYMENT.md` section 7 for the pre-live-trading checklist that
  sets these deliberately rather than leaving them at defaults.

## 4. Auditing

The `audit_logs` table (`AuditLog` in `db/models.py`) records login,
logout, kill-switch engage/disengage, and is the intended home for every
other security-relevant action as engines gain the ability to act (order
placement, config changes, model deployment) — see the model's own
docstring. `GET /api/system/events` and the dashboard's System Events page
surface this without needing direct DB access.

## 5. Secrets handling

- No secret is ever committed — `.env` is gitignored, `.env.example` ships
  with every value blank.
- **The `$`-in-secrets shell-sourcing bug** (hit for real in Phase 8): never
  `source .env` in bash — `ADMIN_PASSWORD_HASH` contains literal `$`
  characters (`$argon2id$v=19$...`) that bash tries to expand as variables,
  silently corrupting the value. Every script in this repo either uses
  Docker Compose's `env_file:` (reads the file literally) or reads secrets
  from a container's already-populated environment via `docker compose
  exec` — see `scripts/backup-db.sh` and `docs/DEPLOYMENT.md` section 2.2.
- `SOLANA_WALLET_PRIVATE_KEY` and `BINANCE_API_SECRET` are the two highest-
  value secrets in `.env` — a leak of either means real funds are at risk
  the moment `TRADING_ENABLED`/`LIVE_TRADING_ENABLED` are set. Rotate both
  immediately if `.env` is ever exposed (e.g. committed by accident, or a
  server is compromised), and treat the droplet's `.env` file as the single
  highest-value target on the box.

## 6. Transport security

- TLS termination happens at the `reverse-proxy` (nginx) container —
  internal service-to-service traffic (postgres, redis, the FastAPI/Next.js
  containers) stays on the Docker Compose bridge network, never exposed to
  the host or internet directly (see `infra/docker/docker-compose.yml`).
- HTTP (port 80) only ever serves the ACME HTTP-01 challenge and redirects
  everything else to HTTPS — see `infra/nginx/nginx.conf`.
- As of Phase 10, every HTTPS response carries `Strict-Transport-Security`
  (2-year max-age, `includeSubDomains`), `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy:
  strict-origin-when-cross-origin`, and a `Content-Security-Policy` scoped
  to `'self'` for scripts/default-src (`'unsafe-inline'` only for
  `style-src`, needed by Next.js's inlined critical CSS — nothing here
  serves attacker-controlled markup that a stricter policy would meaningfully
  protect against, since this is a single-operator dashboard, not a
  multi-tenant app rendering third-party content). Live-verified against
  the real nginx binary — see "What's verified" below.

## 7. CORS

`packages/core-py/yonixalpha_core/config.py::cors_origins` returns only
`PUBLIC_DOMAIN` and `NEXT_PUBLIC_API_URL` in production (plus
`localhost:3000`/`127.0.0.1:3000` in development), never a wildcard, even
though `allow_credentials=True` is set (`apps/api/app/main.py`) — a wildcard
origin combined with credentials is a classic misconfiguration this avoids
by construction (FastAPI's `CORSMiddleware` would in fact reject
`allow_origins=["*"]` with `allow_credentials=True` outright, but the config
here never attempts it). No code change was needed for Phase 10; this
section exists so the reasoning is written down somewhere, not just correct
by accident.

## 8. Dependency scanning

- **Frontend** (`apps/web`): `npm audit` — as of this phase, 2 findings
  (`next` moderate, `postcss` high, both transitively the same root cause).
  The only available fix is `next@16.3.5`, a major-version bump
  (`npm audit fix --force`); not applied in this phase — see "Known
  limitations" below for why and what to do about it.
- **Backend** (11 Python projects): `pip-audit`, run against each project's
  actual installed dependency set (not just `requirements.txt` parsed
  in isolation, which misses what versions transitive deps actually
  resolve to). See the Phase 10 commit for the exact findings at the time;
  run `pip-audit` locally in a fresh venv per project for the current
  state, since this list ages the moment any dependency's advisory
  database updates.
- **CI**: `.github/workflows/ci.yml` runs `npm audit --audit-level=high` and
  `pip-audit` for every project on every push, so a newly-disclosed CVE in
  an existing pinned dependency surfaces the next time CI runs against that
  commit, not just when someone remembers to check manually.

## 9. Server hardening

`scripts/bootstrap-server.sh` (Phase 9, extended Phase 10):
- `ufw` open only for 22 (SSH), 80, 443.
- `unattended-upgrades` for automatic security patches.
- `fail2ban` watching SSH, banning after repeated failed auth.
Neither addition can lock out a legitimate operator: `fail2ban`'s default
SSH jail only bans failed-*password* attempts, and this script's own SSH
connection (the one running it) is never itself banned by its own actions —
see the script's comments for the exact jail config chosen and why.

## 10. Known limitations

- **The `next`/`postcss` npm audit finding has no fix that isn't a major
  version bump.** `postcss <=8.5.22`'s vulnerabilities (XSS via unescaped
  `</style>` in stringified CSS output, and arbitrary local file read via
  attacker-controlled `sourceMappingURL` comments in CSS files it processes)
  are both about *processing CSS from an untrusted source at build/runtime*.
  This project's CSS is 100% first-party (`apps/web/app/**/*.css`,
  authored in this repo, never user-supplied), and PostCSS here only ever
  runs at *build time* via Next.js's own toolchain — not at runtime against
  request data — so neither advisory's actual attack surface (untrusted CSS
  reaching PostCSS) exists in this codebase today. Upgrading to `next@16` is
  deferred rather than done blind in this sandbox (untested App Router
  breaking changes across a major version, no way to verify a Next 16 build
  end-to-end here beyond `npm run build` succeeding) — tracked as a
  follow-up to apply and fully re-verify (`typecheck`/`lint`/`build`, then a
  real browser pass per the Phase 8 dashboard verification) rather than
  applied speculatively.
- **Single account, no RBAC.** Acceptable for a private single-operator
  platform; would need real design work (roles, per-route authorization) if
  ever opened to more than one operator.
- **No WAF / DDoS protection** in front of nginx — a determined volumetric
  attacker can still exhaust the droplet's bandwidth or connection count
  before nginx's own rate limiting ever engages. Out of scope for a
  single-operator private tool on a single droplet; would matter if this
  were ever made multi-tenant or public-facing.
- **CSP `style-src 'unsafe-inline'`**: Next.js inlines some critical CSS;
  a stricter nonce-based policy is possible but adds real complexity
  (nonce generation/threading through Next's SSR pipeline) for a
  single-operator dashboard that renders no third-party or user-supplied
  content — judged not worth it today, revisit if that ever changes.

## What's verified vs. not (Phase 10)

**Verified for real, in this session:**
- Every nginx security header and the `/api/auth/login` rate limit, tested
  live against the real nginx binary (via apt, same technique as Phase 9):
  confirmed all five headers (`Strict-Transport-Security`,
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Content-Security-Policy`) present on a real HTTPS response, confirmed
  `/api/` proxying still works unaffected, and confirmed the rate limit
  (5r/m, burst 5) returns HTTP 429 on the 7th rapid request to
  `/api/auth/login` specifically while `/api/system/status` in the same
  burst pattern is unaffected.
- `npm audit` and `pip-audit` findings above, from real audit runs against
  this repo's actual pinned dependencies (Python: a fresh venv per project
  with `requirements-dev.txt` actually installed, not just the requirements
  file parsed statically).
- Every shell script change passed `shellcheck` clean.

**Not verified, and cannot be from this sandbox:**
- CI actually catching a real CVE end-to-end (the scanning steps run for
  the first time on GitHub's infrastructure when this phase's commit is
  pushed — see the Actions run for this phase for the result, per the
  Phase 9 precedent of watching a real run rather than assuming the YAML
  is correct).
- `fail2ban`/`unattended-upgrades` actually running correctly on a real,
  internet-facing droplet under real attack traffic — verified here only
  via package installation and config syntax, same category of limitation
  as the rest of `scripts/bootstrap-server.sh` (see `docs/DEPLOYMENT.md`).
