# Memecoin Refocus: Phase 1 audit and preservation (2026-10-20)

> **Later the same day:** Phases 2-7 changed several rows of the gap matrix below. They added scanner pipeline health, Smart Wallets refreshed without copy trading, stop-gap position sizing and Strategy Comparison. The current status and what is still open are in `docs/MEMECOIN_UPGRADE_REPORT.md`.

Scope: the operator's "MEMECOIN INTELLIGENCE & PROFITABILITY RECOVERY UPGRADE" brief, Phase 1 (audit and preservation). This round touched two things:
- the dashboard navigation (section 2);
- one git archive branch.

No trading code, setting, worker or database row was changed. Evidence labels follow `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md` (the "recovery report"):
- VERIFIED: shown by code, git or server output;
- INFERRED: follows from verified facts;
- UNKNOWN: no evidence yet.

---

## 1. What the repository looked like at the start of this round

`origin/main` was at `6bf1cfe` (PR #85, 2026-10-17). Between Oct 16 and 17, other sessions merged PRs #82-#85 (VERIFIED, git log):

| PR | Change | Effect on Memecoin |
|---|---|---|
| #82 | Memecoin **archived from the main menu**; regular trading became the start page (`/dashboard` redirected to `/dashboard/crypto/center`). Also added exchange connections with encrypted keys and live orders locked in code (`LIVE_ORDERS_UNLOCKED = False`), a 20-strategy library, a Strategy Center, portfolio risk and local-only backtests. | menu only |
| #83 | handoff update | none |
| #84 | **Memecoin pause that survives deploys**: `scripts/memecoin.sh pause/resume/status`, marker file `runtime/memecoin-paused`, Memecoin workers behind the compose profile `memecoin` | if the operator ran `pause`, every Memecoin worker is stopped |
| #85 | Binance HTTP 451 diagnosis and a public market-data provider | none |

Server state that the repository cannot show (UNKNOWN until the operator runs the section 9 commands):
- whether Memecoin is paused right now;
- whether the Crypto Trading worker is still running Meta Muse in DEMO;
- whether `LIVE_TRADING_ENABLED=true` (reported by `/api/health/ready` on Oct 17) combines with the other two environment locks to permit live trading. Live trading needs all three: `TRADING_ENABLED`, `LIVE_TRADING_ENABLED` and not `PAPER_TRADING` (`safety/store.py live_trading_permitted`).

## 2. Preservation (done)

- **Archive branch:** `archive/crypto-exchange-trading-2026-10-20` at `6bf1cfe`. It holds the complete exchange-trading work as it was (VERIFIED: `git ls-remote`).
- **The earlier archive is unchanged:** `archive/legacy-futures-forex-grid-2026-09-29` (Binance futures, MT5/forex, grid services removed on Sep 29).
- **Nothing was deleted:**
  - code: `packages/crypto-py`, `services/crypto-trading`, `apps/api/app/api/routes/crypto*.py`, `apps/web/app/dashboard/crypto/*`;
  - data: the Postgres schema `crypto` and its tables, encrypted exchange keys, compose service, CI rows.
- **Navigation:**
  - Crypto / Exchange Trading is now under "Archived Engines". Its pages still work, so its engine can be turned off there.
  - Memecoin Trading is the main navigation again.
  - The start page is `/dashboard/memecoin`.
- **Restore:** set `CRYPTO_ARCHIVED = false` in `apps/web/components/archivedEngines.ts`. The full Oct 16 layout is on the archive branch.

**Isolation (VERIFIED, grep):**
- No Memecoin package or service imports `yonixalpha_crypto`.
- `yonixalpha_crypto` imports nothing from `yonixalpha_core`; a static test enforces this.
- The Memecoin system therefore runs independently of the archived exchange modules.

## 3. Exchange-trading inventory

| Surface | Where | Status after this round |
|---|---|---|
| Web pages | `apps/web/app/dashboard/crypto/{page,center,center/[id],backtests,compare,portfolio,trades,connections,alerts}` | archived (reachable from Archived Engines) |
| API routes | `crypto.py`, `crypto_center.py`, `crypto_connections.py`, `crypto_execution.py` (`/api/crypto/*`), included in `apps/api/app/api/router.py` | still served; only the archived pages call them |
| Worker | `services/crypto-trading` (compose service `crypto-trading`, 128 MB, restart on failure) | unchanged. It runs while its DB switch `crypto.engine_state.enabled` is ON or a demo position is open, and exits otherwise. |
| API image | `apps/api/Dockerfile` installs `packages/crypto-py` and runs `python -m yonixalpha_crypto.migrate upgrade` at start; a failure there never stops the API | unchanged |
| Exchange keys | `yonixalpha_crypto.private` (encrypted secret store, signed HTTP, live lock in code) | unchanged, locked |
| Dormant legacy names in Memecoin code | `execution_router.py` (`BINANCE_FUTURES`), `safety/settings.py` (`FUTURES_DEFAULTS`, engine names `binance_futures`, `bybit_futures`, `hyperliquid`, `mt5`), `safety/gate.py`, `safety/pipeline.py`, `paper_engine.py`, `db/models.py` (Binance tables) | inert: no running service produces those engines since the Sep 29 removal. They are left in place for now, because removing them means editing the risk gate and planner. A later cleanup needs its own regression tests. |

**Recommended operator action** (not done by this change): turn the Crypto Trading engine off on Archived Engines > Engine (Strategy Lab). Its worker then exits and frees about 60 MB on a 2 GB host.

## 4. The requested memecoin sections vs the pages that exist

| Requested section | Page (now in the main menu) |
|---|---|
| Dashboard | `/dashboard/memecoin` (start page) |
| Fresh / Early Tokens | `/dashboard/solana/fresh` (plus `/solana/observing`) |
| Migrated Tokens | `/dashboard/solana/migrated` |
| Momentum | `/dashboard/solana/momentum` |
| Token Scanner | `/dashboard/tokens` (search), token detail `/dashboard/tokens/[mint]`; scanner state also on `/dashboard/entry-intel` and `/dashboard/funnel` |
| Smart Wallets | `/dashboard/smart-wallets`. It is now shown without the copy-trading profile (it was hidden while copy trading is off). See gap W1. |
| Strategies | `/dashboard/strategies` (it existed but was missing from the menu) |
| Paper Trading | `/dashboard/paper` |
| ML Intelligence | `/dashboard/ml`, `/dashboard/ml/review` |
| Risk Management | `/dashboard/risk-settings`, `/dashboard/rules`, `/dashboard/risk` (kill switch) |
| Trade History | `/dashboard/trades`, `/dashboard/positions`, `/dashboard/decisions` |
| System Health | `/dashboard/health`, `/dashboard/rpc`, `/dashboard/config` |
| Settings | `/dashboard/settings` |

Verified in a browser on a local stack: after login the app opens on `/dashboard/memecoin`, the menu lists the sections above, and Archived Engines lists the eight Crypto pages with an "archived engine" notice on each.

## 5. The memecoin pipeline (code map)

Details: `docs/EXECUTION_PATH_MAP.md`, `docs/FINAL_AUDIT_ARCHITECTURE.md`.

```
data-solana        on-chain pump.fun program-log stream (pump_stream, RPC WebSocket) = trading data
                   + PumpPortal data WebSocket (pumpportal_ws): independent launch / migration view, coverage check
                   + migration_watch (pool-based migration detection, PR #74) + stream_guard (gap fill, reconnect)
engine-solana-discovery   candidates (fresh / migrated), funnel, entry_shadow (strategies F1-F3 / M1-M3 / P1-P3 in shadow)
decision-engine    gate_eval -> safety gate (safety/gate.py, pipeline.py, planning.py) -> ML scores (shadow) -> decision
paper-trading      paper entries (paper_execution: latency / drift / pool re-pricing), gate_manage exits, live_worker (locked)
ml                 gate / entry / shadow models, frozen validation sets, governance (no auto-activation)
copy-engine        wallet profiles + copy trading. OFF under SOLANA_ONLY, so Solana wallet profiles are not rebuilt (W1).
```

## 6. Gap matrix against the brief (sections 3-13)

Status:
- **EXISTS**: implemented, with the evidence shown.
- **PARTIAL**: some of it exists; what is missing is listed.
- **MISSING**: not implemented.

| Brief | Requirement | Status | Evidence / what is missing |
|---|---|---|---|
| §3 | Forensic investigation of the profitable period | PARTIAL | Recovery report (Oct 15): the "profitable period" was one +1389.9% trade; without it every strategy was net negative. Strategy code is unchanged since Sep 25. Timeline of sizing, cost and outage changes is in its section 2. **Missing:** results after the R2-R10 deploy (Oct 15 onward), and whether Memecoin was paused on Oct 17. Needs the section 9 commands. |
| §4 | Fresh and migrated discovery | EXISTS | `pump_stream`, `pumpportal_ws` (subscribeNewToken and subscribeMigration, no key), `migration_watch` (pool-confirmed migration), DexScreener, Jupiter quote and RPC adapters (`solana/market_data.py`, `token_market.py`). Separate engines and settings for `solana_fresh`, `solana_migration` and `solana_momentum`. |
| §5 | Reliable scanning, health panel | PARTIAL | Exists: reconnect with backoff (`pumpportal_ws.run`), stream coverage check and gap fill (`stream_guard`), RPC manager with failover and hot reload, the R7 outage alarm (5 min, hourly repeat), heartbeats, connection states on `/dashboard/health`. **Root cause of the Oct 13-14 stop, VERIFIED:** the only dashboard Helius provider was rate-limited, its replacement stayed disabled for about 19 h, and the env backup answered 429 (recovery report section 6). **Missing:** one panel with last token discovered, last candidate evaluated, last paper trade, queue depth and lag, and reconnect / rate-limit counts per provider. Some of these exist on separate pages. |
| §6 | Profitable-wallet intelligence | PARTIAL | Exists: FIFO wallet P/L (`wallet_pnl.py`), validation with minimum evidence, drawdown, best-trade share and profitable-period share (`wallet_validation.py`), launch-buyer reputation and dump clusters (`wallet_intel.py`, `wallet_graph.py`), behaviour labels (`ml/wallet_labels.py`), follower latency for copy trades (`copy_outcomes.py`). **W1:** Solana wallet profiles are rebuilt only by `copy-engine` (`rebuild_solana`), which is off under SOLANA_ONLY, so the Smart Wallets data is not refreshed. **Missing:** a follower simulation "entered N seconds after the wallet" for every qualified wallet trade, independent of copy trading. |
| §7 | Entry conditions of profitable traders | PARTIAL | Exists: decision-time launch features (`solana/launch_features.py`), entry timing (`entry_timing.py`), opportunity ledger with paths to 60 min and executable returns (`opportunities.py`). **Missing:** wallet-entry snapshots tied to qualified wallets, and behaviour clustering. |
| §8 | Separate entry models with reasons | PARTIAL | Exists: fresh / migration / momentum signal rules (`strategies/solana.py`), shadow strategies with reasons (`entry_shadow`), WAIT / REJECT reasons on every decision. **Missing:** a qualified-wallet confirmation strategy and a pullback strategy (both shadow first). |
| §9 | ML on verified outcomes, leakage-safe | EXISTS (shadow) | Decision-time feature rows, chronological frozen validation sets, champion / challenger, no auto-promotion (`ml/governance.py`, `ml/frozen.py`, `services/ml/app/validation.py`). Losses and rejections are included (`gate_ml.py`, `opportunity_features.py`). To check: baseline-rule comparison and walk-forward reports per strategy. |
| §10 | X optional | EXISTS | `x_narrative.py`: shadow only, never blocks, delays or vetoes; `narrative_weight` defaults to 0; no data gives NO_DATA. **Missing:** the with / without X comparison on the same windows (needs data). |
| §11 | Risk and execution safety | EXISTS | Per-trade SOL risk, daily and account limits, per-strategy limits, duplicate-exposure protection, cooldown (R10), kill switch, cost-aware planner, 10% stop floor (R3), paper latency, drift and pool re-pricing (R4 / R5), stop fill beyond the stop. To check on the server: live is locked (section 9). |
| §12 | Reproducible before / after paper comparison | PARTIAL | `strategy_recovery` (`--split` periods), `regression_report`, `parity_report`, `cost_report`. **Missing:** a replay harness that runs the old and new strategy rules on the same recorded snapshots. Limit: rejected decisions older than 3 days are pruned by retention; positions, orders, fills, ML rows and outcome labels are kept. |
| §13 | Dashboard | PARTIAL | Token detail page with timeline (`/dashboard/tokens/[mint]`), strategy performance (`/dashboard/strategies`), health. **Missing:** the unified panel in §5, wallet follower results on Smart Wallets, and a Strategy Comparison view across fresh / migrated / momentum with costs. |

## 7. Forensic questions (§3): what is established

| # | Question | Answer so far | Label |
|---|---|---|---|
| 1-2 | Strategy and code version in the profitable period | fresh / migration / momentum rules from `strategies/solana.py` (unchanged since Sep 25); code in force Oct 6 = `871e46a` (#43) | VERIFIED |
| 3 | Paper start, peak, end, drawdown | Start 10 SOL. On Oct 15: -0.7294 SOL realized, cash 8.69, risk-engine equity 9.33. Peak and drawdown curve not computed yet. | VERIFIED (peak and drawdown: to do) |
| 4-5 | Tokens, reasons, hold times, PnL | per trade in `strategy_recovery --section trades/top/deep` | VERIFIED (Oct 15 data) |
| 6 | When deterioration started | It did not "start": without trade `915561d1` (+0.139 SOL) every period is negative. Sizing changes (Oct 9 23:29) and outages changed the size of the losses. | VERIFIED |
| 7 | Discovery slowed, or trades unprofitable? | Both, at different times: the Oct 13-14 discovery stop (provider), the Oct 14 migration daily-limit blocks, and unprofitable trades throughout | VERIFIED |
| 8 | Stale prices, fees, slippage, exits | Paper overstated live in three places (take-profits at the first post-migration price, no latency on migrated entries, stops on quiet pools). Fixed by R3-R5. | VERIFIED |
| 9 | Unrealistic old fills | yes, as in #8; paper paid LIVE fixed costs only from Oct 7 (#53) | VERIFIED |
| 10 | Provider changes around the decline | pump.fun change on Oct 8 (migration detection fixed by #74, Oct 11); Helius provider rate limit Oct 13 | VERIFIED |
| new | Results after R2-R10 (Oct 15 onward) | not measured | UNKNOWN |

Cannot be established: whether a rejected candidate before about Oct 12 was a missed winner (deleted by retention). Info / warning system events older than 3 days are also gone; errors and criticals are kept.

## 8. Open decisions for the operator

1. **Memecoin paused or running?** If paused, paper trading, discovery and ML are stopped and no new evidence accumulates. Resuming is `bash scripts/memecoin.sh resume`, and only the operator decides it. Check first that live is locked (section 9, command B).
2. **`LIVE_TRADING_ENABLED=true` in `.env`:** with `PAPER_TRADING=true` live is still locked, but the brief asks for live disabled by default. Setting it to `false` is safe and also recreates every Memecoin container (they load `.env`).
3. **The Crypto Trading engine:** turn it off (Archived Engines > Engine) to free memory.
4. **Rotate the Chainstack key** (open since R8).

## 9. Read-only server checks for Phase 2

The setup line, then each check. Nothing is written.

```
cd /opt/yonixalpha && C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
```
- **A. Version and Memecoin state:** `git log --oneline -1 && bash scripts/memecoin.sh status`
- **B. Live locks:** `$C exec -T api python -c "from yonixalpha_core.config import Settings; s=Settings(); print('TRADING_ENABLED', s.TRADING_ENABLED, 'LIVE_TRADING_ENABLED', s.LIVE_TRADING_ENABLED, 'PAPER_TRADING', s.PAPER_TRADING)"`
- **C. Paper book and per-strategy results, split at the R2-R10 deploy:** `$C exec -T api python -m yonixalpha_core.tools.strategy_recovery --section book --section compare --split 2026-10-15T12:00:00+00:00`
- **D. Discovery activity and error kinds (the last days):** `$C exec -T api python -m yonixalpha_core.tools.strategy_recovery --section activity`

## 10. Plan (smallest safe steps first)

1. **(This round)** Navigation back to Memecoin, exchange UI archived, this audit. Deploy: web only.
2. **Phase 2:** with the section 9 output, extend the recovery report with the period from Oct 15. Add the paper equity curve (peak and drawdown) to `strategy_recovery`.
3. **Phase 3:**
   - one System Health panel with last event, last token, last candidate, last paper trade, queue depth and lag, and reconnect / 429 counts per provider, in the states CONNECTED / DEGRADED / DISCONNECTED / STALE;
   - a test that discovery continues when PumpPortal, DexScreener or X fail.
4. **Phase 4:**
   - W1: rebuild Solana wallet profiles without copy trading (a read-only job, no copy orders);
   - a follower-delay simulation per qualified wallet trade.
5. **Phase 5:** the replay harness on recorded snapshots, then qualified-wallet and pullback strategies in shadow, compared against the baseline with costs.
6. **Phase 6-7:** the Strategy Comparison page, then the full regression suite and verification.
