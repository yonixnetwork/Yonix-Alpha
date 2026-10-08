# Low-resource mode (2026-10-08)

The production droplet has 2 vCPU and 2 GB RAM, and the database is about 16 GB. This change keeps the trading core
reliable on that hardware. Copy trading is suspended, not removed. ML training and historical analytics yield. Execution,
open positions and Solana discovery are never paused. Nothing here deletes data or changes Solana execution.

Every number below comes from the server outputs of 2026-10-07 (`db_health`, `parity_report`). Anything not measured
says NOT MEASURED.

## 1. Report before deployment

### Current resource profile (2026-10-07 22:00, after PR #55)
| | Value |
|---|---|
| Load average 1 / 5 / 15 min | 9.82 / 7.95 / 7.30 (2 vCPU) |
| Memory available | 345 MB of 1 967 MB |
| Swap used | 1 298 MB |
| Postgres connections | 16 idle, 9 active, 2 idle in transaction (27 of 100) |
| Statements running > 2 s | 9 |
| Database | evm_trades 3.7 GB, risk_assessments 3.3 GB, opportunity_outcomes 2.8 GB, market_snapshots 1.8 GB, copy_events 0.9 GB, launch_buyers 0.9 GB (about 16 GB in all) |
| Redis | capped at 256 MB, `volatile-lru` (only keys with a TTL can be evicted; the kill switch has none) |

### Top memory consumers / top CPU consumers (`docker stats --no-stream`, 2026-10-08 13:27, after PR #56)
One reading, not an average.

| Container | CPU | Memory |
|---|---|---|
| copy-engine | 60.7 % | 147 MB |
| postgres | 37.6 % | 320 MB |
| data-evm | 27.4 % | 116 MB |
| api | 20.0 % | 63 MB |
| engine-solana-discovery | 10.2 % | 48 MB |
| redis | 9.3 % | 262 MB (its cap is 256 MB of data: it is evicting keys that have a TTL) |
| paper-trading | 3.2 % | 80 MB |
| data-solana | 2.0 % | 29 MB |
| decision-engine | 1.8 % | 45 MB |
| web, ml (between steps), reverse-proxy, certbot | 0 to 1.2 % | 2 to 18 MB |

The copy engine was the largest CPU user at that moment, ahead of Postgres. The Solana trading services (data-solana,
discovery, decision-engine, paper-trading) used about 17 % of one CPU together. Each service also reports `rss_mb` and,
with this change, `cpu_s` in its heartbeat; System Health and `db_health` print both.

### Top database queries (pg_stat_activity, 2026-10-07)
| Query | Time | Owner | Status |
|---|---|---|---|
| missed-winner tokens × first-hour trades | 108 s, 3 parallel workers | ml (wallet labels) | every 6 h (PR #56); paused while copy trading is suspended (this change) |
| evm_tokens entry pass (fresh safety, newest trade) | 49 s | data-evm | index `ix_evm_tokens_chain_safety_at` (PR #56) |
| evm_tokens restore of every launchpad venue | 15 s, every minute | copy-engine | incremental (PR #56); only while copy positions are open when suspended (this change) |
| per-wallet 7-day evm_trades scans | 44 s each | ml (wallet labels) | one scan per chain (PR #55) |
| copy poll on `lower(trader)` | full table every second | copy-engine | index `ix_evm_trades_chain_at` (PR #54); stopped while suspended |
| ML Review aggregates | 372 s / 621 s / 436 s | api | background job, never in a request (PR #53 / #54); deferred while CRITICAL (this change) |

### Top workers / slow API requests (before PR #55)
- `/api/chains/solana`: 32–48 s on every refresh (fixed in PR #55; not seen after 16:42).
- `/api/copy/positions`, `/api/copy/events`, `/api/copy/targets`: 12–15 s.
- `/api/copy/outcomes`, `/api/control/execution-funnel`: 503 after about 30 s.
- `/api/evm/streams`: 12 s.
- `/api/summary`: 2–10 s.

### Resource cost by area
- **Copy trading.** The copy engine watches targets on 3 chains every 1–2 s, re-reads EVM venues every minute,
  evaluates outcomes every minute, and rebuilds wallet profiles every 10 minutes over about 1.3 M launch buyers. Its
  wallet ML is part of the ml service's EVM cycle. Its tables are copy_events (600 k rows, 928 MB) and launch_buyers.
  Measured symptoms: TOO_LATE on 43 451 of 44 849 BSC buys; detection median 111 s on BSC and 64 s on Robinhood.
- **ML.** Solana shadow models took 101.7 s per hour. The EVM / wallet cycle ran for over 50 minutes before PR #55.
  The missed-winner query took 108 s. ML Review took 6–10 minutes per result. Inference runs in the decision engine and
  is cheap. Training is the cost.
- **Solana (discovery, gate, paper and live trading, positions).** NOT MEASURED per service. It is priority 1 and is not
  changed here. Its latency is now printed by `db_health` ("Solana pipeline, last hour": decisions, median decision
  time, median token age at decision, pump stream heartbeat) for the before / after comparison.

## 2. Changes made

| Area | Change |
|---|---|
| Copy trading status | `COPY_TRADING_STATUS` = ACTIVE / THROTTLED / SUSPENDED (default SUSPENDED). Stored at runtime in `platform_settings.operating_mode`; `yonixalpha_core.operating_mode` |
| Copy engine, SUSPENDED | Stops target watching (Solana, BSC, Robinhood), copy entries and mirrored sells, outcome evaluation, wallet profiles, enrichment. Still manages the copy positions already open (stop loss, trailing, exits), and registers their venues only while any is open. Nothing is deleted. Tested |
| Copy engine, THROTTLED | EVM watching every 10 s (was 2 s), outcomes every 10 minutes, no profiles or enrichment. Drops to protection only while the host is CRITICAL. Tested |
| Resource mode | `SYSTEM_RESOURCE_MODE` = NORMAL / LOW_RESOURCE / EMERGENCY (default LOW_RESOURCE). EMERGENCY suspends copy trading and all ML training and review refreshes |
| Resource level | `yonixalpha_core.resources`: NORMAL / WARNING / CRITICAL from /proc (available memory, load per CPU, Linux memory pressure). Thresholds are `RESOURCE_*` settings. CRITICAL pauses priority-3 work only |
| ML training | Training, not inference. In LOW_RESOURCE it runs once per `ML_TRAINING_INTERVAL_LOW_RESOURCE_H` (24 h) and never while CRITICAL; the step shows SKIPPED with the reason. Wallet labels, missed winners and wallet models pause with copy trading; EVM entry / exit learning continues. Tested |
| ML Review | Background job with `review_status` CURRENT / STALE / RUNNING / PENDING / FAILED. HTTP 202 while there is no result. Automatic refreshes wait while CRITICAL; the page's Refresh button computes anyway (the operator's decision) except in EMERGENCY. Fresh for 30 minutes. The page shows last calculated, age and status. Tested |
| Resume control | Copy Trading page: status banner, then "Resume Copy Trading" shows RAM, CPU, swap and database round trip, and the recommendation (WAIT / OK). The server refuses ACTIVE / THROTTLED (409) unless `COPY_RESUME_MIN_FREE_RAM_MB`, `COPY_MAX_CPU_LOAD`, `COPY_MAX_SWAP_USAGE_MB` and `COPY_MAX_DB_LATENCY_MS` all hold. Auto resume is off. Changes are audited. Tested |
| System Health | "Server resources": mode badge (LOW RESOURCE MODE), level and reasons, RAM / swap / load / CPU / swap rate / pressure / disk, Postgres connections, slow statements, size and settings, Redis memory and keys, memory and CPU per service, what is paused now |
| Database connections | Worker pools default to 5 kept + 10 burst (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`; were 10 + 10). The API keeps 10 + 10, the review pool 1 + 1. No worker opens more than about 10 sessions at once (audited: one per loop) |
| Postgres (prod compose) | `shared_buffers=128MB`, `work_mem=4MB`, `maintenance_work_mem=64MB` (defaults, now explicit); `effective_cache_size=512MB` (the default assumes 4 GB of cache); `max_parallel_workers_per_gather=0` (one query had 3 parallel workers on 2 CPUs); `jit=off`. Deploying restarts Postgres for a few seconds |
| Measurement | `db_health` prints mode, copy status, level, per-service memory / CPU, Redis, and Solana pipeline latency |

Not changed (needs measurement first, because they produce trading or ML data): fresh-token observation horizons,
rejected-token outcome tracking (opportunity ledger), market-data sharing between components. Paper trading is not
changed.

## 3. PR #56 (already on main, not yet deployed)
- **Copy engine:** `refresh_adapters` does a full read on start and hourly; otherwise only tokens created or traded since
  the last refresh (both columns indexed). Tested.
- **ml:** missed winners every 6 hours.
- **Migration 0043:** `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_evm_tokens_chain_safety_at`. Downgrade drops it
  (CONCURRENTLY). No table is rewritten.
- **db_health:** EVM feed age.
- **Rollback:** redeploy the previous commit; the index can stay (unused indexes cost only writes).

## 4. Deploy and measure (baseline, change, measure, compare)
1. Baseline: the 2026-10-07 22:00 outputs above.
2. Deploy PR #56 alone (main). Build its index first (command in the PR notes), deploy, wait 1 hour, run `db_health
   --skip-timings`, `parity_report --days 1` and `docker stats --no-stream`.
3. Deploy this change. Wait 1 hour and run the same three commands.
4. Compare in section 5.

```
cd /opt/yonixalpha && docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
```

Rollback of this change: set the mode NORMAL and copy trading ACTIVE in the dashboard (or `.env`), or redeploy the
previous commit. Removing the `command:` lines restores the Postgres defaults.

## 5. After deployment (to be filled from the measurements; nothing estimated)
| | Before (2026-10-07 22:00) | After PR #56 (2026-10-08 13:27) | After low-resource mode |
|---|---|---|---|
| RAM available | 345 MB | 431 MB | |
| Swap used | 1 298 MB | 1 459 MB | |
| Load 1 / 5 / 15 | 9.82 / 7.95 / 7.30 | 6.82 / 7.34 / 7.27 | |
| Statements > 2 s | 9 | 2 | |
| EVM / wallet ML step | RUNNING for over 50 min | 1 028 s | |
| Dashboard slowest request | 48.5 s (`/api/chains/solana`, before PR #55) | 27.0 s (`/api/evm/tokens`); `/api/chains/solana` 4.9 to 8.2 s | |
| 503 count (last 20 slow requests) | 2 at 15:54 | 3 (`/api/control/execution-funnel`, `/api/evm/coordination/summary`, `/api/evm/observations`) | |
| EVM trade feed age (BSC / Robinhood) | NOT MEASURED | 59 s / 60 s | |
| Solana median decision time | NOT MEASURED | NOT MEASURED (the pipeline line ships with this change) | |
| Copy detection median BSC / Robinhood | 111 s / 64 s | 95 s / 62 s | n/a (suspended) |

Notes on the PR #56 reading. About 60 s of the copy delay is the EVM feed itself: data-evm stores trades about a minute
after they happen, so copying cannot be faster than that. The Odyssey cursors (Robinhood) are about 4.9 days old; the
operator marked that venue inactive. Redis is at its memory cap. New slow pages to look at next: `/api/evm/tokens`,
`/api/evm/observations`, `/api/evm/coordination/summary`, `/api/control/execution-funnel`.

## 6. For 8 GB later (reassess after measuring)
`shared_buffers=2GB`, `effective_cache_size=5GB`, `work_mem=16MB`, `maintenance_work_mem=256MB`,
`max_parallel_workers_per_gather=1` with 4 vCPU (0 with 2), `jit=off`. Copy-trading resume thresholds as they are; the
mode can go back to NORMAL once the level stays NORMAL for a day.
