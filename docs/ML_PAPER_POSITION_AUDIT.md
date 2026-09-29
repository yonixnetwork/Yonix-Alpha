# ML readiness, paper learning, position management, 24/7 operation — audit (2026-09-29)

Audit of the running system before implementing the "ML training readiness
+ paper learning + adaptive position management" request. Every claim below
names the code it comes from. Status words: EXISTS (implemented and covered
by tests), PARTIAL, MISSING, VERIFIED IN PRODUCTION (seen in the operator's
production output), NOT VERIFIED.

## 1. Architecture map (as running on the droplet)

| Service (container) | Role | Loop |
|---|---|---|
| data-solana | pump.fun program log stream → Redis (`solana/pump_stream.py`): creates, trades (last 400 / mint, 3 h), curve state, migrations | websocket, continuous |
| engine-solana-discovery | fresh-token observation window, momentum scan, migration candidates (`app/funnel.py`), observation ledger rows + intel | ~seconds |
| decision-engine | safety gate per candidate (`app/gate_eval.py` → `safety/gate.py`, `solana/assembler.py`), plans (`safety/planning.py`), paper entries / LIVE BUY orders | re-evaluates each candidate every 30 s |
| paper-trading | position management (`app/gate_manage.py` → `paper_engine.py`, `exit_intel.py`), LIVE order worker + reconciliation (`app/live_worker.py`, `live_trading.py`), opportunity ledger + wallet intel (`opportunities.py`, `wallet_intel.py`), follow-ups | main loop 15 s; live worker 1 s |
| ml | gate models (quality, challenger, drift: `app/gate_ml.py`), legacy model (`app/train.py`), multi-target shadow models (`app/shadow_ml.py`) | hourly |
| api / web | FastAPI + Next.js dashboard, auth, settings, controls, SSE events | on request / events |
| postgres, redis, reverse-proxy, certbot | storage, cache/event bus, TLS | — |
| legacy (not deployed): engine-solana-momentum, engine-solana-migration; futures/forex services run but are hidden in the UI | | |

Supervision: every service has `restart: unless-stopped` (`infra/docker/docker-compose.prod.yml`); postgres/redis have
healthchecks; every Python service runs `events.heartbeat_loop` (Redis heartbeat, System Health page); services
re-read dashboard settings on every configuration revision (`runtime_watch.run_watcher`).

## 2–20. Findings

| # | Area | What exists | Status |
|---|---|---|---|
| 2 | ML implementation | Per-engine gate models with data-quality quarantine, forward-in-time challenger training, AUC lower bound, Brier, champion-vs-challenger on the same holdout, PSI drift + accuracy drop → `MODEL_DRIFT_DETECTED` (model ignored), operator-only promotion (`services/ml/app/gate_ml.py`, `ml/registry.py`). Multi-target SHADOW models on the ledger, time split with label-availability purge, PR/ROC/calibration/precision@K/segments (`app/shadow_ml.py`) — trained in production 2026-09-29 05:47 on 8,354 rows. | EXISTS; shadow VERIFIED IN PRODUCTION |
| 2a | ML contribution | ML can only make the gate WAIT, and only if a champion was promoted AND `min_ml_confidence` is set (`safety/gate.py` ~L795). No champion is promoted in production ("RULES ONLY"). | contribution effectively 0 — correct, but **no formal readiness lifecycle** (INSUFFICIENT_DATA → … → PRODUCTION_CONTRIBUTOR) and no single place that says *why* ML is not contributing | PARTIAL |
| 3 | Paper trading | Same gate/plan as LIVE; fills against the real bonding curve / PumpSwap pool reserves or a real Jupiter quote (fees, impact, slippage allowance), simulated entry/exit failure rates, TP1–3 partial exits, breakeven move, ratcheting trailing stop, exit intelligence, migration re-routing (`paper_engine.py`, `gate_manage.py`, `paper_execution.py`). PaperExecution vs LIVE separated by `execution_mode`; LIVE only through the order worker. | EXISTS |
| 4 | Rejected-token data flow | Every observation rejection and gate rejection/expiry/entry → `opportunity_outcomes` with decision-time snapshot + causal intel; tracked T+5s/10s/30s/60s/5m/15m/30m/60m; counterfactual (TP/stop rule, no hindsight), labels with `available_at`, wallet outcomes (`opportunities.py`, `opportunity_analysis.py`). 14k rows/night in production. | EXISTS, VERIFIED IN PRODUCTION; **T+20s horizon missing** |
| 5 | Risk engine | `safety/planning.plan_trade`: SL (manual → strategy → AUTO from volatility, bounded by min/max stop), max loss (equity × risk_per_trade), size (risk / loss fraction incl. costs and fixed costs, capped by balance, exposure, token exposure, pool fraction, impact), TP1–3, trailing — each with provenance MANUAL / STRATEGY / AUTO, method and inputs; any missing input → NO_TRADE. | EXISTS |
| 6 | Position/exit management | `paper_engine.manage_step`: stop, TP partials, breakeven, trailing ratchet (never loosens), pause keeps the stop, exit now; exit intelligence HOLD/REDUCE/EXIT from flow, liquidity, creator, holders (`exit_intel.py`). | EXISTS; **cadence gap, see §G1** |
| 7 | Trailing / TP / SL values | TP at R-multiples (1R/2R/3R) of the stop distance; exit fractions 40/30/30; trailing distance max(1.5×vol, 5%) capped at the stop, activates at TP1. All in dashboard settings. | EXISTS; **default fractions sum to 100%: the whole position is sold by 3R (+30% at the 10% minimum stop) — no runner** |
| 8 | P&L | Realized from simulated/confirmed fills incl. fees; unrealized marks each management pass; LIVE from on-chain fills (`live_trading.py`), MFE/MAE from the market entry price. | EXISTS |
| 9 | Market-cap formatting | Shown in SOL everywhere (`TokenTerminal.tsx`, `ManualTrade.tsx`, `TokenIntel.tsx`, `LedgerReview.tsx`, `OpportunityOutcomes.tsx`); no central formatter; a cached SOL/USD exists (`solana/sol_price.py`). | **MISSING (USD)** |
| 10 | Token details | `/dashboard/tokens/[mint]`: terminal (price, curve, activity, manual buy/sell), launch intelligence, decision history with T0→T+60m path, gate decisions, positions, candidates. | EXISTS |
| 11 | Server-side workers | All trading, observation, management, reconciliation, ML jobs run in containers; nothing depends on the browser. | EXISTS; 24/7 acceptance test **NOT VERIFIED** (needs the operator) |
| 12 | Frontend | Next.js 15, lucide-react icons, SSE `useApi` reloads; Solana-only navigation, futures/forex pages hidden. | EXISTS |
| 13 | Solana execution | Build (PumpPortal trade-local / local builders) → transaction guard → sign → simulate → submit → confirm (75 s) → parse fill (`solana/live_exec.py`, `txguard.py`, `live_trading.py`); idempotency keys, needs_review on unknown outcome. LIVE buys/sells VERIFIED IN PRODUCTION earlier. | EXISTS |
| 14 | Migration routing | Curve position whose curve completed is priced from the PumpSwap pool and its LIVE route switches to `pump-amm` (`gate_manage._note_migration`). | EXISTS |
| 15 | RPC providers | DB-stored encrypted providers, method-aware health, 403/429 states, Retry-After, priority classes, failover (`solana/rpc.py`, `rpc_registry.py`). | EXISTS |
| 16 | Latency data | Per order: discovery→decision, eval, queue, quote, build, guard, simulation, submission, submit→seen/confirm, RPC time (`execution_analysis.timing`), trade_report. | EXISTS for orders; **position-management cadence not measured** |
| 17 | Supervision | restart policies, heartbeats, per-row isolation, alert on failures. | EXISTS |
| 18 | Restart / reconciliation | LIVE worker reconciles DB vs wallet on start-up before touching orders, then periodically (`live_worker.py`). | EXISTS |
| 19 | Tests | core 631, decision-engine 75, paper-trading 76, discovery 17, ml 28, api 130; CI on every push. | EXISTS |

## G. Gap matrix (ordered by risk to money, then by value)

| ID | Gap | Evidence | Plan | Risk of the change |
|---|---|---|---|---|
| G1 | **Open positions are managed only every ~15 s + the time of all other work in the same loop** (ledger tracking of ~2,000 rows, follow-ups, grid). A stop/trailing/TP trigger can wait 15 s+ on tokens that move 20–30% in seconds. | `services/paper-trading/app/main.py` LOOP_INTERVAL_SECONDS=15, `manage_gate_positions` inside `_paper_trading_loop` | Separate position loop every 2 s; RPC-priced (PumpSwap) positions at most every 5 s each; slow loop no longer manages positions (never two managers at once); record the actual cadence | low: same management code, only when it runs |
| G2 | No formal ML readiness lifecycle / reason ML is not contributing | §2a | `ml/readiness.py`: state per model from real counts (labelled samples, time span, classes, holdout AUC lower bound, calibration, drift, champion, paper validation), contribution flag; API + ML page panel; the gate keeps using ML only as today (never more) | none to trading: read-only status |
| G3 | Market cap in SOL | §9 | central `formatUsdCompact`, API returns SOL/USD with its source/age; USD primary, SOL secondary; "USD unavailable" when no fresh SOL/USD | UI only |
| G4 | T+20s horizon missing | §4 | add T+20s to the ledger horizons | low |
| G5 | TP fractions sum to 100% → no runner | §7 | operator decision (changes live trading): propose 35/30/20 leaving 15% on the trailing stop; not changed in code | changes exits — needs operator approval |
| G6 | Trailing activation / max giveback not separately configurable (activation is TP1) | §7 | settings `trailing_activation_r`, `trailing_max_giveback_pct` (defaults reproduce today's behaviour) | medium — next batch |
| G7 | P_MIGRATE never positive | production report | report now shows ledger vs stream migrations; fix after data | — |
| G8 | 24/7 acceptance and restart recovery not verified on the droplet | §11 | operator runs the documented test (browser closed, container restart) with read-only checks | — |

Not gaps (already correct, preserved): risk provenance, NO_TRADE on unknown risk inputs, paper reset keeps
history (`POST /control/paper/accounts/{name}/reset` changes only balances and requires a flat account),
pause/resume/exit-now controls (`POST /paper/positions/{id}/pause|resume|exit`), blacklists/rules, tax gate,
creator-history actions, Mayhem handling, migrated liquidity minimum, hidden futures UI.

## Implementation status (2026-09-29)

| ID | Status | What changed | Verified |
|---|---|---|---|
| G1 | IMPLEMENTED | `paper-trading/app/main.py::_position_loop` manages open gate positions every 2 s; PumpSwap (RPC-priced) positions re-priced at most every 5 s (`gate_manage.due`); the main 15 s loop no longer manages positions. Last pass (time, duration, counts) in `yx:pm:last_pass`, shown on System Health with a STALE flag after 15 s. | unit test (`test_fast_loop_reprices_pool_positions_at_most_every_rpc_interval`), API test, browser; **production cadence NOT VERIFIED** until deployed |
| G2 | IMPLEMENTED | `ml/readiness.py` + `GET /api/ml/readiness` + "ML readiness" panel on the ML Engine page: state per model, why, samples, holdout AUC and lower bound, champion/challenger, drift, dataset counts. Read-only; the gate's use of ML is unchanged. | unit + API tests, browser (empty system → INSUFFICIENT_DATA, rules only) |
| G3 | IMPLEMENTED | `formatUsdCompact` / `formatSol` (`lib/format.ts`), `useSolUsd` (one shared request per page), `MarketCap` component: USD first, SOL second, "USD unavailable" without a fresh (≤ 5 min) rate. `GET /api/tokens/sol-usd`; paper-trading refreshes the cached Jupiter SOL→USDC quote about once a minute. Used in the token terminal (market cap, liquidity), manual trade preview, launch snapshots and the ledger path (historical rows say in the tooltip that the current rate is used). Opportunity-outcome buckets stay in SOL (their boundaries are SOL). | API test, browser (38.67 SOL × 152.40 → "$5.9K"; no rate → "38.67 SOL · USD unavailable"); **production rate source NOT VERIFIED** (depends on the Jupiter quote working on the droplet) |
| G4 | IMPLEMENTED | `T+20s` added to `opportunities.HORIZONS` and the path views. Only rows still TRACKING get it; completed rows are unchanged. | unit test |
| G5 | OPERATOR DECISION | Not changed. Proposal: `tp_exit_fractions` 0.35 / 0.30 / 0.20 leaves 15% on the trailing stop as a runner. | — |
| G6 | IMPLEMENTED | Settings `trailing_activation_r` (0 = at TP1, default) and `trailing_max_giveback_pct` (0 = no cap, default); defaults reproduce the previous plan exactly; validated; shown under "Stops, targets & trailing". | unit test (defaults give an identical plan) |
| G7 | OPEN | needs production data | — |
| G8 | OPERATOR | acceptance procedure in the report | — |
