# YonixAlpha full system audit (2026-10-07)

Status vocabulary: IMPLEMENTED + VERIFIED (a test or server output shows it), IMPLEMENTED BUT NOT VERIFIED (code exists,
no production evidence yet), PARTIALLY IMPLEMENTED, UI ONLY, BACKEND ONLY, BROKEN, MISSING, DEPRECATED, UNKNOWN.
"Verified" here never means "live verified": no live transaction was sent for this audit.

## Dependency map

```
browser (Next.js apps/web, polls with useApi + WebSocket /api/ws)
  -> nginx reverse-proxy (60 s read timeout on /api/)
    -> api (FastAPI, ONE uvicorn process, pool 10+10 connections)  -> Postgres, Redis
workers (each its own container, Postgres + Redis + RPC):
  data-solana        Pump stream / RPC ingestion            -> Redis stream, tokens, launch features
  engine-solana-discovery  candidates                        -> trading_candidates
  decision-engine    safety gate + risk plan + paper/live entry -> risk_assessments, paper_positions, execution_orders
  paper-trading      position management (paper + live exits), live worker, rent reclaim, opportunity ledger
  data-evm           BSC + Robinhood discovery, safety, observation, EVM paper entries / exits, wallet sync
  copy-engine        copy targets (Solana / EVM), copy events, wallet profiles (background rebuild)
  ml                 Solana training, gate models, shadow models, ablation, frozen validation, EVM / wallet ML (shadow)
external: Solana RPC (db:alchemy / Helius), PumpPortal, Jupiter, BSC / Robinhood RPC, Etherscan-family, GitHub API,
          Telegram, optional Nansen / MadeOnSol (not connected)
```

## Components

| Component | Location | Status | Problems found in this audit | Risk | Fix / recommendation |
|---|---|---|---|---|---|
| API request handling | apps/api/app/main.py | IMPLEMENTED + VERIFIED | No request IDs; no statement limit, so a slow query outlived the proxy's 504 and held a pooled connection | High (all pages) | Request ID + timing middleware, slow-request list, 25 s statement limit, 503 QUERY_TIMEOUT / DB_POOL_EXHAUSTED (this audit) |
| ML Review: ledger review / comparison / opportunity lists | core opportunities.py, routes/ml.py | IMPLEMENTED + VERIFIED | Loaded a week of rows into memory; unbounded counts (504s) | High | SQL aggregation, date bounds, 60 s cache (this audit) |
| EVM / wallet ML page | routes/ml.py `/evm` | IMPLEMENTED BUT NOT VERIFIED at production size | ~10 scans of 14 days per request; copy_events count unindexed | Medium | Cache + index (this audit); `db_health` times it on the server |
| Wallets overview | routes/wallets.py | IMPLEMENTED + VERIFIED | Failed only as collateral of the above | Low | — |
| Dashboard polling | apps/web/lib/useApi.ts | IMPLEMENTED + VERIFIED | Stacked a new poll on a still-running request | Medium | One request per panel; late answers of an old filter dropped (this audit) |
| Solana live execution (Pump curve, PumpSwap, Jupiter) | solana/live_exec.py, tx_builders.py | IMPLEMENTED; LIVE VERIFIED by earlier confirmed orders (101 sells on 2026-10-06) | 7wmm sells failed with 6053 for 9 h; cause NOT VERIFIED (not Pump-side: no upgrade / config change; same RPC) | High for that position | Unsigned tx now kept on simulation failure; `pumpswap_window_check` pending on server |
| Live sell-failure handling | live_trading.py | IMPLEMENTED + VERIFIED | Retries every few seconds without backoff on a deterministic error | Medium | Proposed: backoff after N identical program errors (not done; needs operator agreement) |
| SOLD OUTSIDE close | live_trading.close_sold_outside, trade.py | IMPLEMENTED + VERIFIED | — | — | — |
| Paper engine (Solana) | paper_engine.py, paper-trading | IMPLEMENTED + VERIFIED | Did not charge LIVE fixed costs; no latency model | High for paper/live comparison | Fixed costs (this audit); latency: open (PAPER_VS_LIVE_PARITY_AUDIT.md) |
| EVM paper (BSC / Robinhood) | chains/evm/paper.py, data-evm | IMPLEMENTED + VERIFIED | Pons V2 tokens graduating to Uniswap V4 cannot be priced or exited; reason hidden | High for those positions | Reason + since + alert (this audit). Uniswap V4 quoting: MISSING |
| EVM paper BUY / SELL | EvmManualTrade.tsx, trade.py | IMPLEMENTED + VERIFIED | BUY only on the Explorer page | Low | BUY button in the chain token table (this audit) |
| EVM LIVE execution | task #178 | MISSING by decision (locked, watch-only) | — | — | Stays locked |
| Wallet manager | balances.py, chains/evm/wallet.py, solana wallet | PARTIALLY IMPLEMENTED | Solana key (WALLET_PRIVATE_KEY) and an optional EVM key / watch-only address; no seed phrase | Medium | Seed phrase support NOT implemented on purpose (see report §F) |
| Copy trading | copy-engine, copy_trading.py | IMPLEMENTED (paper) + VERIFIED by tests | Every copy event has decision, reason, latency stages, outcome; no single view of why copies were skipped | Medium | `parity_report` section 4 (this audit) |
| Wallet profiles | wallet_profiles.py | IMPLEMENTED + VERIFIED | OOM fixed 2026-10-07 (batches, caps) | — | — |
| ML (Solana, gate, shadow, EVM, wallet) | services/ml, core ml/ | IMPLEMENTED + VERIFIED (governance tests) | Contribution 0 % / SHADOW; model fitting runs in the service event loop | Medium | Unchanged; System Health names a stale ml heartbeat |
| ML leakage controls | gate_features, frozen.py, opportunity ledger | IMPLEMENTED + VERIFIED (earlier audits, tests) | — | — | — |
| Update monitor | update_monitor.py, System Health | IMPLEMENTED + VERIFIED | — | — | REVIEW ONLY for third-party code; nothing auto-deploys |
| System Health | routes/system.py, health page | IMPLEMENTED + VERIFIED | No API latency view; top-bar badge did not name the failing part | Low | Slow-request section + named badge (this audit) |
| Telegram alerts | notify.py | IMPLEMENTED + VERIFIED | — | — | New alert: EVM position without a sell quote for 15 min |
| Database | Postgres 16, migrations 0001-0041 | IMPLEMENTED + VERIFIED (alembic upgrade + check) | One missing index (copy_events target_at with outcome) | Low | Migration 0041 (CONCURRENTLY) |
| Infrastructure | docker compose, 2 vCPU / 2 GB droplet | IMPLEMENTED | Memory and CPU are tight; ml and copy-engine run heavy periodic jobs | Medium | Measure with `db_health` (load, memory) before adding limits |

## Diagnostics added in this audit (all read-only)

| Tool | Answers |
|---|---|
| `yonixalpha_core.tools.db_health` | host load / memory, DB connections, statements > 2 s, lock waits, table sizes, timings of the page queries, API slow requests, ml steps |
| `yonixalpha_core.tools.parity_report` | paper vs live results, LIVE order outcomes, refusals by code, copy dispositions and latency |
| `yonixalpha_core.tools.pumpswap_window_check` | whether others sold the 7wmm pool while our sells failed |
| `yonixalpha_core.tools.pump_change_history` | Pump program upgrades and GlobalConfig admin changes |
