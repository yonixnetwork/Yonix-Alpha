# Control Center: Safety Gate, Paper Pipeline, Operator Controls

This document covers what the control-center pass added, how it fits together, how to
deploy it, and **what has and has not been verified**. Design decisions and research
sources are in `IMPLEMENTATION_MATRIX.md`.

Live trading stays off. The server's `.env` keeps `TRADING_ENABLED=false`,
`LIVE_TRADING_ENABLED=false` and `PAPER_TRADING=true`. Live execution needs all three
flipped *on the server*; the dashboard can only display them. Live execution exists for
Pump.fun tokens only (fresh and PumpSwap-migrated): PumpPortal local transactions, checked
by a transaction guard, signed locally, confirmed and reconciled. It is **IMPLEMENTED —
AWAITING CREDENTIAL VERIFICATION**. See `docs/AUDIT_REPORT.md` and the dashboard's
Live Execution page. Futures venues remain paper-only.

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

On the droplet (`/opt/yonixalpha`). Build images **one at a time**: parallel builds ran
out of memory on the 2 GB server earlier.

```bash
cd /opt/yonixalpha
git pull
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"

# The three locks. Keep them exactly like this.
grep -q '^PAPER_TRADING=' .env || echo 'PAPER_TRADING=true' >> .env
grep -E '^(TRADING_ENABLED|LIVE_TRADING_ENABLED|PAPER_TRADING)=' .env   # expect false / false / true

# Every Python service changed (heartbeats, venue tracking), plus web and nginx (/api/ws).
for s in api web decision-engine paper-trading ml engine-solana-discovery data-solana \
         data-binance engine-binance-futures reverse-proxy; do $C build $s || break; done

# Superseded Solana engines stay in the 'legacy' profile (momentum now runs in discovery).
$C --profile legacy rm -sf engine-solana-momentum engine-solana-migration

$C up -d                        # api applies migrations 0009 and 0010 on start
$C restart reverse-proxy        # optional since nginx re-resolves api/web through Docker DNS; still reloads a renewed certificate
$C ps
$C logs --tail 50 api | grep -i alembic
```

Credentials, all optional except the Solana RPC pair. Put them in `.env` only, never in the
dashboard:

| Variable | Needed for | Without it |
|---|---|---|
| `SOLANA_RPC_URL`, `SOLANA_WS_URL` (+ `_BACKUP_`) | pump.fun stream, mint/holder reads | Solana engines idle and say so |
| `JUPITER_API_KEY` | paid Jupiter host | free `lite-api.jup.ag` is used |
| `BINANCE_API_KEY` / `_SECRET` | engine-binance-futures account sync (read side) | Binance account shows NOT CONNECTED; public data still works |
| `BYBIT_API_KEY` / `_SECRET` (read-only key) | Bybit account read | Bybit account shows NOT CONNECTED |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | Hyperliquid account read (public info API) | NOT CONNECTED |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery | notifications stay in-app only |

Outbound HTTPS from `api`, `decision-engine` and `paper-trading` to `fapi.binance.com`,
`api.bybit.com` and `api.hyperliquid.xyz` is required for futures, grid and venue pages.
Docker's default network allows it.

Memory: nothing new runs as a container. The futures runner is a loop inside
decision-engine, the grid a loop inside paper-trading, and champion/challenger training a
loop inside ml.

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

States as the spec defines them. "Unit/integration" means local Postgres + Redis with
synthetic or recorded inputs. Nothing marked VERIFIED has touched a live exchange or live
Solana data, because the build environment blocks every external API (proxy 403).

| Component | Status | Evidence |
|---|---|---|
| Safety gate, sizing, SL/TP/trailing with provenance (AUTO/MANUAL/STRATEGY), LONG + SHORT | VERIFIED (unit) | core tests incl. `test_short_and_book.py` |
| Order-book fill model (walk the book, fees, max slippage, SHORT loss formula) | VERIFIED (unit) | same |
| pump.fun event and bonding-curve decoding | PARTIALLY VERIFIED | Byte-exact tests against the official IDL; not run on live logs |
| Mint authority / Token-2022 / holders parsing | PARTIALLY VERIFIED | Tests on documented jsonParsed shapes; not run on live RPC |
| Wallet indicators (sniper share, sync clusters, round-trips, serial creator) | VERIFIED (unit) | synthetic trade sets |
| Jupiter, DexScreener adapters | IMPLEMENTED — AWAITING LIVE VERIFICATION | mocked HTTP only |
| Binance futures public, Bybit V5 public, Hyperliquid info adapters | IMPLEMENTED — AWAITING LIVE VERIFICATION | request shapes and parsing checked against the official SDK sources; mocked HTTP tests |
| Bybit signed read-only account calls | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | HMAC signature tested against the documented algorithm |
| Hyperliquid account read (by address) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | mocked |
| Binance account (from engine-binance-futures tables) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | engine's own tests; no key configured |
| Meta Muse → gate → paper (LONG/SHORT, exit rule, closed-candle dedupe) | VERIFIED (integration, synthetic candles) | `services/decision-engine/tests/test_futures_eval.py` |
| Confluence Matrix scoring on XAUUSDT | VERIFIED (unit + integration, synthetic) | strategy + runner tests; MT5/forex execution BLOCKED |
| Hyperliquid grid (paper): build, maker fills, breakers, worst-case refusal, start/stop | VERIFIED (integration, synthetic mids) | `services/paper-trading/tests/test_engines.py` |
| Gold vs BTC analytics | VERIFIED (unit); live data NOT VERIFIED | API returns 502 in the sandbox, as designed |
| Solana momentum signal and scan | VERIFIED (unit/integration, synthetic) | discovery funnel tests |
| Exit intelligence (HOLD/REDUCE/EXIT, two-evidence rule) | VERIFIED (unit) | |
| Realtime bus, WebSocket auth/origin check, heartbeats, notifications | VERIFIED (integration) | `test_events.py`, `test_ws.py`; live in the browser check (indicator "Live") |
| ML quality quarantine, challenger training, no auto-promotion, drift flag | VERIFIED (integration, synthetic) | `services/ml/tests/test_gate_ml.py` |
| Champion inference: explained, advisory only, ignored on drift | VERIFIED (integration) | `services/decision-engine/tests/test_ml_champion.py` |
| Control-center API (analytics, strategies, venues, health, ML review, notifications, controls) | VERIFIED (integration) | 91 API tests |
| Migrations 0009 + 0010 | VERIFIED | upgrade → downgrade base → upgrade; `alembic check` clean |
| Dashboard (23 routes) | VERIFIED (browser) | Chromium 1440 px + 390 px, DB seeded by the real pipeline; no page overflow |
| Live data on the server | NOT VERIFIED | run §3 and `yonixalpha_core.tools.verify_live` |
| Any strategy's profitability | NOT VERIFIED | no backtest; paper results will be the first evidence |
| Live execution (Pump.fun) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION | mocked-boundary tests only; no real transaction sent |
| Live execution (futures venues) | NOT IMPLEMENTED | LIVE targets are refused |
| Confluence on MT5 / forex | BLOCKED | MetaTrader5 is Windows-only |

## 5. Behaviour worth knowing before reading the results

- **Most launches will be NO_TRADE or WAIT, and that is intended.** A 1-SOL-class trade on a
  young curve typically pays ~1.3% fee per side plus impact, and the default 3% slippage
  allowance is counted as an exit cost. The Solana engines start with a 10% minimum stop
  (`ENGINE_DEFAULTS`) so the stop sits outside those costs; sizing shrinks the position so the
  loss at the stop stays at 1% of paper equity.
- **Post-migration trades are assessed from the canonical PumpSwap pool**: reserves and fee
  are read on chain and trader wallets come from the pool's Buy/Sell events, so the same
  wallet-level checks run as for bonding-curve tokens. No pool yet means `MIGRATION_PENDING`.
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
