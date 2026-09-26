# Fresh-token observation, curve-aware liquidity, precise holder labels

Date: 2026-09-26. This improves the working YonixAlpha pipeline. It does not
replace it: the stream, safety gate, planner, paper and live execution,
position manager, dashboard and every existing API route still work, and
their tests still pass.

## 1. Baseline, recorded before any change

| Item | Baseline |
|---|---|
| Services | api, web, reverse-proxy, certbot, postgres, redis, data-solana, data-binance, engine-solana-discovery, decision-engine, paper-trading, execution-futures, engine-binance-futures, ml (legacy profile: engine-solana-momentum, engine-solana-migration) |
| API routes | 92 |
| Realtime events | balance.updated, migration.detected, ml.model.updated, ml.prediction.updated, position.updated, risk.updated, signal.created, strategy.updated, token.discovered, trade.closed, trade.created, trade.updated |
| Database | migration head 0011 |
| Tests | core 357, api 102, decision-engine 59, paper-trading 61, discovery 8, execution-futures 9, data-solana 10, data-binance 14, ml 23, engine-binance-futures 48, momentum 11, migration 11 |

## 2. Audit: why fresh tokens were rejected or disappeared

Traced from the pump.fun stream to a position.

1. **Silent prefilter.** The discovery funnel dropped a new token unless it
   was at least 60 s old, with at least 10 trades and 10 unique buyers in
   5 minutes. On the live droplet, 444 of 450 considered tokens failed in
   one run. Only a counter recorded this; no per-token reason was kept.
2. **Oldest first.** `recent_unpromoted` read the 500 *oldest* launches of
   the last 30 minutes. Failed tokens were never marked, and the stream sees
   about 1,000 launches per 30 minutes. So the funnel kept re-scanning the
   same old tokens, and the newest launches were often never looked at.
3. **The pool-liquidity minimum was mandatory for curve tokens.**
   `_check_liquidity` compared the curve's real SOL reserve (1–5 SOL early)
   with `min_liquidity_quote` (20 SOL, a DEX-pool figure). Result:
   `WAITING_FOR_LIQUIDITY`, then `INSUFFICIENT_LIQUIDITY` after 30 minutes.
   Almost no fresh token could pass.
4. **Migrated "pool age" was wrong.** `post_migration_signal` waits while the
   pool is younger than 2 minutes. The age it used was the oldest of the last
   25 pool trades. On a busy pool that is under a minute, so an active
   migrated token never qualified.
5. **No volatility, no stop, no trade.** Automatic stops are sized from
   1-minute realized volatility, which needs at least 3 one-minute returns.
   A young token, or a busy pool's 25-trade sample, has none, so the result
   was `AUTO_SL_NO_VOLATILITY` → NO_TRADE.
6. **Holder concentration mislabelled.**
   - What is measured: the largest token accounts from
     `getTokenLargestAccounts`, aggregated per owner, as a share of total
     supply, with the bonding curve or canonical pool excluded.
   - Pump.fun **Mayhem mode** (`create_v2` with `is_mayhem_mode`, from the
     official IDL) holds part of the supply in a vault of the Mayhem program
     (`MAyhSmzX…`). It was reported as "one wallet holds 50%" and rejected.
     The droplet's live check showed exactly `top1=0.5000`.
   - That vault is a protocol trading agent, not the developer.
7. **Holder owner lookup at the wrong commitment.** Fixed earlier in
   `f375088`. It is listed here because it caused the same symptom.

Thresholds that were already dashboard settings, and still are:

- `max_top1_share` 15% (reduce size)
- `reject_top1_share` 40%
- `max_top10_share` 45%
- `reject_top10_share` 80%
- `max_creator_share` 10% (approval)
- `min_unique_buyers` 10
- `min_trades_in_window` 10
- `max_buy_tax_pct` / `max_sell_tax_pct` 5%
- `max_slippage_bps` 300
- `max_entry_impact_bps` 300
- `max_exit_impact_bps` 500
- `max_round_trip_loss_bps` 1000
- `min_ml_confidence` (off)

## 3. What changed

### Fresh pump.fun lifecycle (`solana/observation.py`, discovery funnel)

1. **FRESH_OBSERVING.** Every launch is observed for
   `fresh_observation_seconds` (default 10 s). The stream keeps every trade
   with its on-chain timestamp, so each funnel run rebuilds the whole window.
   Nothing is decided from a single moment.
2. **Re-evaluation at the end of the window.** The funnel compares:
   - cumulative checkpoints **T0 / T+5 s / T+10 s**: trades, buyers,
     sellers, volume, price;
   - the two half-windows: trades, new buyers, sellers, buy and sell volume,
     price change.

   It derives transaction, volume, buyer and seller growth, the buy/sell
   ratio, sell pressure, the drawdown from the observed peak, and whether
   the creator sold. The trend is **INCREASING**, **STABLE**,
   **DETERIORATING** or **NO_ACTIVITY**, and the positive and negative
   signals are listed.
3. **Outcome** (all thresholds are settings):
   - **PROMOTE**: at least 8 trades, 6 buyers and 0.5 SOL since launch, and
     the trend is not deteriorating. The token is handed to the safety gate,
     which still runs every check.
   - **CONTINUE_MONITORING**: interesting but not qualified yet, or the gate
     budget is full.
   - **REJECT**: the price is ≥ 35% below its observed peak, or the creator
     sold while sellers dominated.
   - **NO_TRADE** (expired): inactive for 120 s, fewer than 3 trades in the
     latest window, past 900 s of monitoring, monitoring disabled, or the
     monitoring capacity (300 tokens) reached. When over capacity, the least
     active token is dropped first.
   - **MIGRATION_DETECTED**: the curve completed or a pool appeared.
4. **Liquidity is never a precondition before migration.** The curve's real
   reserve, its progress and a liquidity-state label (`NO DEX POOL YET —
   bonding curve market`) are recorded as context.
5. **Every final outcome is stored** in `token_observations` (migration
   0012, kept 3 days): reasons, trend, checkpoints, halves, metrics and curve
   state. Tokens are processed newest first.

### Safety gate

- **Curve engines** (`solana_fresh`, `solana_momentum`) judge the curve as a
  market: finding `BONDING_CURVE_MARKET`. Executability comes from the
  existing exact fill simulation, both directions, within the impact and
  round-trip limits. Without a curve model the execution-data check still
  blocks.
  - An optional `min_curve_liquidity_quote` (default 0) adds a curve-reserve
    minimum.
  - Migrated pools keep `min_liquidity_quote` unchanged.
- **Activity trend** for fresh tokens over the latest 30 s:
  `ACTIVITY_DETERIORATING` → WAIT, and `NO_RECENT_ACTIVITY` → WAIT.
- **Holder labels.** Owners are classified as a *wallet* (ed25519 curve
  point), a *program-derived account*, or the *Mayhem agent vault*.
  `top1`/`top10` now count wallets only. Findings read:
  - `TOP HOLDER (wallet)` or `TOP HOLDER (creator wallet)`, with the
    address;
  - `CREATOR WALLET holding`;
  - `PUMP.FUN MAYHEM AGENT VAULT` (reduce size above
    `max_protocol_agent_share`);
  - `PROGRAM-CONTROLLED ACCOUNT` (approval above
    `max_program_controlled_share`).

  No protection was removed. The wallet thresholds are unchanged.
- **Volatility.** When 1-minute returns are too few, 10-second returns over
  the last 5 minutes are used, scaled to 1 minute. Only measured data is
  used, and the source is recorded in the decision. A young token becomes
  plannable after about 40 s of trading. A token too volatile for
  `max_stop_pct` is still refused, correctly.

### Migration

- A curve candidate whose token gets a PumpSwap pool moves to the new
  **MIGRATED** state, with timeline event `migration_detected`. The
  migration engine re-evaluates it with pool rules, as its own candidate
  (MIGRATION_DETECTED → MIGRATED_ANALYSIS).
- Pool age now comes from the migration event timestamp. The old
  oldest-trade figure is used only as a labelled fallback.

### Momentum

- Established tokens older than `momentum_min_age_seconds` (30 min) are
  handled as before.
- **New:** a younger token approaching migration (curve progress ≥
  `momentum_near_migration_progress`, 70%) is considered too, if the fresh
  engine neither promoted it nor is still monitoring it. Lifecycles stay
  separate: fresh → observation, curve momentum → momentum,
  migrated → migration.

### Exit intelligence

- The existing constants became settings with the same defaults.
- New **EXIT_NOW**, where one signal is enough:
  - real liquidity down ≥ 60% since entry;
  - price down ≥ 35% from the high since entry;
  - a creator dump: holding halved and selling now.
- New **REDUCE**: volume collapsed to ≤ 25% of the previous window, with
  buyers stopped or sellers dominating.
- Profit management was already configurable and is unchanged:
  - TP1/TP2/TP3 as R-multiples with partial-exit fractions;
  - optional breakeven after TP1;
  - a never-loosening trailing stop;
  - a manual %-based plan per strategy.

### Settings center and connection tests

- `GET /api/settings/overview`: every provider's configuration. Secrets are
  shown only as *configured* / *not configured*; non-secret values are
  shown; RPC URLs are reduced to `scheme://host`.
- `POST /api/settings/providers/{name}/test`: one real, **read-only**
  request, classified CONNECTED / AUTHENTICATION FAILED / TIMEOUT /
  RATE LIMITED / INVALID CONFIGURATION / UNAVAILABLE. It is audited, has a
  5 s cooldown, and the last result is remembered.
  - Providers: solana_rpc, solana_rpc_backup, solana_ws, helius, jupiter,
    pumpportal, binance, bybit, hyperliquid, mt5, telegram.
  - Telegram uses `getMe` and sends no message.
- Dashboard: **Settings → Providers & connections** with TEST buttons and a
  settings-center map; **Solana → Fresh Observation**, showing live
  observations and "why didn't the bot trade this token?" with the full
  report and gate findings.
- Secrets are **not** editable from the dashboard. See the security section
  of the report.

## 4. New settings

All are edited on **Risk Settings**, per engine scope. The fresh-token and
momentum-funnel values are read from the `solana_fresh` scope.

| Setting | Default |
|---|---|
| `fresh_observation_seconds` | 10 |
| `fresh_continue_monitoring` | true |
| `fresh_max_monitoring_seconds` | 900 |
| `fresh_max_monitored_tokens` | 300 |
| `fresh_min_trades_to_continue` | 3 |
| `fresh_inactivity_timeout_seconds` | 120 |
| `fresh_promote_min_trades` | 8 |
| `fresh_promote_min_unique_buyers` | 6 |
| `fresh_promote_min_volume_quote` | 0.5 SOL |
| `fresh_max_sell_pressure` | 2 (sell/buy volume) |
| `fresh_max_price_drawdown_pct` | 0.35 |
| `max_active_candidates` | 25 |
| `momentum_min_age_seconds` | 1800 |
| `momentum_near_migration_progress` | 0.70 |
| `min_curve_liquidity_quote` | 0 (off) |
| `max_protocol_agent_share` | 0.60 |
| `max_program_controlled_share` | 0.15 |
| `exit_liquidity_drop_exit` | 0.40 |
| `exit_liquidity_drop_warn` | 0.20 |
| `exit_reduce_fraction` | 0.5 |
| `exit_volume_collapse_ratio` | 0.25 |
| `exit_emergency_liquidity_drop` | 0.60 |
| `exit_emergency_price_drop` | 0.35 |

All of them are validated, and bounded by hard limits where one applies.
