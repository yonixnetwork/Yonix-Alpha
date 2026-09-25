# Control Center: Safety Gate, Paper Pipeline, Operator Controls

This document covers what the control-center pass added, how it fits together, how to
deploy it, and **what has and has not been verified**. Design decisions and research
sources are in `IMPLEMENTATION_MATRIX.md`.

Live trading stays off. Nothing in this pass places a real order, signs a transaction, or
reads a wallet key. The server's `.env` keeps `TRADING_ENABLED=false`,
`LIVE_TRADING_ENABLED=false` and `PAPER_TRADING=true`. Live execution needs all three
flipped *on the server*; the dashboard can only display them. Even with the locks open,
live Solana execution is not implemented: a LIVE target is refused and logged.

## 1. How a pump.fun token flows through the system

```
Solana WS (logsSubscribe, pump.fun program only)
  └─ engine-solana-discovery
       ├─ decodes Create/Trade/Complete/Migration events from "Program data:" logs
       ├─ Redis stream store: meta, curve reserves + fee bps, per-mint trades (capped, TTL)
       └─ funnel (every 10 s)
            prefilter on the token's own trades (age ≥ 60 s, min trades, min unique buyers)
            budget: ≤ 25 candidates under assessment at once
            migration events → migration-engine candidates
                 │
decision-engine (every 15 s; each candidate re-evaluated at most every 30 s)
  ├─ controls from DB: settings version, global/strategy mode, blacklist, custom rules,
  │   paper account state, kill switch, pending approval
  ├─ assembler: RPC mint (authorities, Token-2022 extensions), bonding curve account,
  │   largest holders (curve/pool excluded), stream trade flow + volatility
  │   (migrated tokens: DexScreener pool + 4 Jupiter quotes for impact and round trip)
  ├─ safety gate → EXECUTE / REDUCE_SIZE / WAIT / REQUIRE_MANUAL_APPROVAL / NO_TRADE / REJECT
  ├─ every assessment stored (risk_assessments), including all rejections
  └─ executable + PAPER → paper position opened from the gate's own plan, same transaction
                 │
paper-trading (every 15 s)
  ├─ marks at the live curve price (exact curve simulation on exit),
  │   or a real Jupiter sell quote once migrated; stale stream → no action
  ├─ stop → TP1/TP2/TP3 partial exits → trailing stop (never loosens), MFE/MAE
  ├─ on close: realized PnL, fees, ML label backfill, candidate CLOSED
  └─ opportunities not taken: records the price 15 min–6 h later (review only)
```

Operator surfaces (dashboard, all audit-logged):

| Page | What it does |
|---|---|
| Decisions | Every gate evaluation with reasons; approve or decline pending items |
| Decision detail | Findings by category, full plan with MANUAL/AUTO provenance, data freshness, raw evidence, settings used, timeline, later outcome |
| Risk Settings | Versioned thresholds per scope (GLOBAL / engine), hard limits, history |
| Rules & Blacklist | Name/symbol/mint blacklist (exact or glob) and custom feature rules |
| Strategy Center | Global and per-strategy modes, environment locks (read-only), pipeline health |
| Paper Trading | Paper accounts (equity, PnL, fees, reset when flat) and positions |

Approval semantics: approving a REQUIRE_MANUAL_APPROVAL item lets the *next* evaluation
proceed within 10 minutes, and that evaluation re-runs every check on fresh data. An
approval can never lift a WAIT, NO_TRADE or REJECT finding.

## 2. Deploying this to the server

On the droplet (`/opt/yonixalpha`). Images are built one at a time because parallel builds
ran out of memory on the 2 GB server earlier.

```bash
cd /opt/yonixalpha
git pull
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"

# Optional but recommended: make the third lock explicit (defaults to true if absent).
grep -q '^PAPER_TRADING=' .env || echo 'PAPER_TRADING=true' >> .env

for s in api decision-engine paper-trading engine-solana-discovery web; do $C build $s; done

# The superseded Solana engines are in the 'legacy' profile now; stop and remove them.
$C --profile legacy rm -sf engine-solana-momentum engine-solana-migration

# Redis picks up its new memory cap; api runs migration 0009 on start.
$C up -d
$C restart reverse-proxy     # nginx caches upstream IPs of recreated containers
$C ps
$C logs --tail 50 api | grep -i alembic
```

`SOLANA_RPC_URL` and `SOLANA_WS_URL` must be set for the pipeline to run. Without them,
discovery and the gate stay idle and say so in their logs. `JUPITER_API_KEY` is optional:
without it, the free `lite-api.jup.ag` host is used.

## 3. Verifying against live data (do this first)

None of the live data paths could be exercised from the build environment, where every
external API was blocked. This read-only tool checks each one from the server:

```bash
$C run --rm decision-engine python -m yonixalpha_core.tools.verify_live --seconds 60
```

It listens to pump.fun for 60 s, decodes what it sees, reads one sampled mint, its holders
and its bonding curve over RPC, and requests Jupiter and DexScreener quotes for well-known
tokens. It prints provider URLs as scheme://host only and writes nothing. The most important
line is `pump_stream`:

- **VERIFIED**: trade events decode from the live logs, so the whole Solana pipeline has real input.
- **FAILED, "no notifications"**: the RPC provider rejects or doesn't serve `logsSubscribe`
  for this program. Use a provider that supports it (e.g. Helius).
- **FAILED, "no TradeEvent decoded"**: pump.fun is emitting events only via self-CPI. The
  decoder already understands that form (`EVENT_IX_TAG`), but ingestion would then have to
  read transaction inner instructions. That is a code change, not a configuration change.

After deploying, Strategy Center → *Solana pipeline health* should show the last event
within seconds and growing trade counters.

## 4. Verification status

| Component | Status | Evidence |
|---|---|---|
| Safety gate, sizing, SL/TP/trailing with provenance | VERIFIED (unit) | 54 gate + rule/settings tests (spec §87 matrix) |
| pump.fun event and bonding-curve decoding | PARTIALLY VERIFIED | Byte-exact tests against the official IDL layout; not yet run on live logs |
| Mint authority / Token-2022 extensions / holders parsing | PARTIALLY VERIFIED | Tests on the documented jsonParsed shapes; not yet run on live RPC |
| Jupiter quotes, DexScreener pool | IMPLEMENTED — AWAITING LIVE VERIFICATION | Mocked-HTTP tests only; sandbox got 403 |
| Stream store → funnel → gate → paper entry → TP/trailing exit | VERIFIED (integration, synthetic data) | End-to-end tests on local Postgres + Redis |
| Migration 0009 | VERIFIED | upgrade → downgrade → upgrade; `alembic check` clean |
| Control API | VERIFIED (integration) | 10 API tests incl. auth, validation, audit, LIVE refusal |
| Dashboard pages | VERIFIED (browser) | Chromium at 1440 px and 390 px against a DB seeded by the real pipeline |
| Live pump.fun ingestion on the server | NOT VERIFIED | Run §3 |
| Entry heuristics (`fresh_launch_flow`, `post_migration_flow`) | NOT VERIFIED (no edge shown) | Transparent rules, no backtest; they only narrow what the gate allows |
| Live Solana execution | NOT IMPLEMENTED | LIVE targets are refused |
| Bybit, Hyperliquid, Meta Muse, Gold vs BTC, champion/challenger | NOT STARTED | See `IMPLEMENTATION_MATRIX.md` §6 |
| Confluence Matrix | BLOCKED | MetaTrader5 is Windows-only |

## 5. Behaviour worth knowing before reading the results

- **Most launches will be NO_TRADE or WAIT, and that is intended.** A 1-SOL-class trade on a
  young curve typically pays ~1.3% fee per side plus impact, and the default 3% slippage
  allowance is counted as an exit cost. The Solana engines start with a 10% minimum stop
  (`ENGINE_DEFAULTS`) so the stop sits outside those costs; sizing shrinks the position so the
  loss at the stop stays at 1% of paper equity.
- **Post-migration trades always need approval**: DexScreener transaction counts carry no
  wallet identities, so manipulation checks can't run (`NO_WALLET_DATA`).
- **Equity marks open positions at the last price**, before exit costs. Realized PnL is net of
  every simulated fee and impact.
- **"What happened afterwards"** on rejected items ignores costs and whether an exit was
  possible. It is for reviewing the gate's rejections, not evidence of missed profit.
- **Paper results are simulations.** The fill model is exact for the curve's math but cannot
  model latency, competing transactions in the same slot, or MEV.

## 6. Security fixes in this pass

- RPC health snapshots were logged, stored in `system_events` and sent to Telegram with the
  full provider URL. Helius-style URLs carry the API key in the query string. WS and RPC error
  logs had the same problem because httpx embeds the URL in error text. Now every one of these
  paths prints scheme://host only (`yonixalpha_core/redact.py`). If a keyed RPC URL was
  configured before this deploy, rotate that key: it may be in old logs, `system_events` rows
  or Telegram history.
- Redis runs with `maxmemory 256mb` and `volatile-lru`, which evicts only keys with a TTL.
  The stream buffers have TTLs; the kill switch never does, so memory pressure can never
  disengage it.
