# API

`apps/api` is a FastAPI backend behind JWT auth (see `docs/SECURITY.md`, or
`app/api/routes/auth.py`). Every route below requires a bearer
access token except `/api/health*` and `/api/auth/*`. All routes are
mounted under `/api`.

Every list endpoint returns the same paginated envelope:

```json
{ "items": [...], "total": 123, "limit": 50, "offset": 0 }
```

`limit` defaults to 50, maxes at 200; `offset` defaults to 0.

## Health

- `GET /health`, `GET /health/live` — liveness, no dependency checks.
- `GET /health/ready` — checks Postgres + Redis, returns 503 if either fails.

## Auth

- `POST /auth/login` — `{username, password}` -> token pair. Lockout after
  5 failed attempts in 15 minutes (429).
- `POST /auth/refresh` — rotates the refresh token (single use).
- `POST /auth/logout` — revokes the refresh token.
- `GET /auth/me` — the authenticated username.

## System

- `GET /system/status` — app env/trading flags, kill-switch state, and
  **real** per-service status (`running`/`stopped`/`unknown`) derived from
  each service's own `service_started`/`service_stopped` `SystemEvent`
  rows — not a live health check (this process has no channel to ping
  another container), and not the hardcoded `"not_implemented"` strings
  this endpoint returned from Phase 1 through Phase 7.
- `GET /system/events?service=&severity=&limit=&offset=` — the raw
  `system_events` audit/health log every service writes to.

## Candidates (Solana `trading_candidates`)

- `GET /candidates?state=&engine=&limit=&offset=`
- `GET /candidates/{id}` — adds `state_history`, `detail`, and the
  candidate's latest `StrategySignal`, latest `RiskEvent`, and any
  `PaperPosition`, joined server-side so the dashboard's detail view is
  one request.

## Signals

- `GET /signals?candidate_id=&decision=&limit=&offset=` — the
  `strategy_signals` audit trail from `services/decision-engine` (see
  `docs/ML.md`'s note that `decision` is structurally almost never `LONG`
  today).

## Risk

- `GET /risk/events?approved=&limit=&offset=` — the `risk_events` audit
  trail.
- `GET /risk/kill-switch` — `{engaged, reason}`, read from the same Redis
  key every service checks (`yonixalpha_core.kill_switch`).
- `POST /risk/kill-switch/engage` — `{reason}` (required, non-empty).
  Writes an `AuditLog` row (`kill_switch_engaged`) with the authenticated
  user, their IP, and the reason — this is the one write action a
  dashboard operator has over live trading state (spec section 37).
- `POST /risk/kill-switch/disengage` — writes `kill_switch_disengaged`.

## ML

- `GET /ml/models?name=&status=&limit=&offset=` — the `model_versions`
  registry, newest-trained first. Never serializes the `artifact` bytes.
- `GET /ml/stats` — `{total_features, labeled_features, unlabeled_features}`
  from `ml_features`. Per `docs/ML.md`, `labeled_features` is expected to
  be `0` until paper trading closes a position with a known outcome.

## Paper trading

- `GET /paper/positions?status=&limit=&offset=` — the `paper_positions`
  table. Per `docs/PAPER_TRADING.md`, expected to be empty today.

## Notes on what's real vs. what's honestly empty

Every list endpoint above returns real, live data — nothing is mocked or
stubbed for the dashboard's benefit. Several of them are expected to
return zero rows in this environment (no trained ML model, no paper
positions, no `LONG` signals) for reasons documented in `docs/ML.md` and
`docs/PAPER_TRADING.md`; the dashboard's empty states say so explicitly
rather than presenting placeholder data.
