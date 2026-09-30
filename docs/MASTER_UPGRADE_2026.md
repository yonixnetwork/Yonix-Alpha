# Master multi-chain upgrade — audit and gap matrix (2026-09-30)

Scope: the "MASTER MULTI-CHAIN TRADING, SNIPING, COPY-TRADING, WALLET
INTELLIGENCE, ML & RELIABILITY UPGRADE" prompt (sections 0–84), audited
against the code on `main` at 84d6b86 and the production evidence pasted from
the server on 2026-09-30.

Status words:

| Word | Meaning |
|---|---|
| DONE | implemented and tested in code; production evidence where stated |
| PARTIAL | part of the requirement exists; the gap is named |
| MISSING | not implemented |
| NOT VERIFIED | implemented, but not proven on the real chain / with a real transaction |

Nothing here is LIVE VERIFIED on BSC or Robinhood Chain: no authorized EVM
transaction has been sent. Solana live execution exists (Pump.fun / PumpSwap)
and is unchanged by this upgrade.

## 1. Production evidence already collected (2026-09-30)

| Chain | Launchpad | Evidence (launchpad_verify, data-evm) |
|---|---|---|
| BSC | Four.meme | ACTIVE, DISCOVERY (807 launches / 1 h), EVENTS (1233 trades), QUOTE (buy+sell), LIQUIDITY PASS; discovery live, 730 trades / 30 min |
| BSC | Flap | discovery live, 19,129 trades / 30 min; verify run hit an RPC timeout (retry added in 84d6b86) |
| Robinhood | Pons V2 | ACTIVE, DISCOVERY (308), EVENTS (14,601), QUOTE, LIQUIDITY, MIGRATION_DETECTION PASS; 3,181 trades / 30 min |
| Robinhood | Pons V1, NOXA, Odyssey curve / instant / reflection | ACTIVE PASS; DISCOVERY FAIL: 0 launches in 1 h, and Odyssey reflection 0 in 24 h (855k blocks). Inactive venue or wrong event set: undecided (M1 raw-log check) |
| BSC / Robinhood | all | BUY, SELL, TX_MONITORING: NOT RUN (need a real authorized transaction) |

RPC: BSC logs served only by bsc-rpc.publicnode.com (bsc-dataseed* refuse
eth_getLogs); Robinhood has one public RPC that rate-limits (~1 req/s after
pacing). Section 51/52's "do not use public RPC as the sole production path"
is therefore **not met today** — a keyed provider is needed (Settings →
Providers), see M7.

## 2. Requirement matrix

| § | Requirement | Status | Evidence / gap | Phase |
|---|---|---|---|---|
| 1 | Preserve working Solana / Pump.fun / PumpSwap execution | DONE | no change to `solana/live_exec.py`, PumpSwap or the Solana gate in this upgrade | — |
| 2–3 | Research Jul–Sep 2026, repository records | PARTIAL | `MULTICHAIN_AUDIT_2026.md` §1, `PUMPFUN_EXECUTION_RESEARCH.md`, `SCANNER_INTELLIGENCE_2026.md`; the new repos in §7–12 not yet recorded | M9 |
| 4 | Only Solana, BSC, Robinhood in the active UI | DONE | legacy futures/forex/grid removed (archive branch) | — |
| 5 | Launchpad health: activity status, last launch/trade/migration, 7d counts, verified flags | DONE in code (M1), NOT VERIFIED in production yet | `chains/activity.py`, table `launchpad_activity` (migration 0024), rollup written in `evm/store.persist_scan`, `/api/launchpads`, Launchpads page; `tests/test_launchpad_activity.py`, `test_control_center` | M1 |
| 6 | 7-day inactivity → INACTIVE, hidden from active filter, adapter kept, auto-reactivation | DONE in code (M1) | INACTIVE needs 7 days without activity AND 7 days of monitoring (else UNVERIFIED); Active / Archived tabs; discovery keeps scanning, so activity returns the venue to ACTIVE; Solana trade counts are "not tracked" (None), never 0 | M1 |
| 7 | Solana launchpads beyond Pump.fun/PumpSwap (LetsBONK, LaunchLab, Meteora DBC, Bags, Moonshot, Jupiter Studio) | MISSING | registry has `pumpfun`, `pumpswap` only | M10 |
| 8 | BSC: Four.meme, Flap verified; Genius.fun etc. researched | PARTIAL | Four.meme / Flap adapters, discovery live, read-only checks PASS; others not researched | M10 |
| 9 | BSC mempool wallet copying | MISSING | copy engine reads confirmed trades only; no pending-tx stream | M8 |
| 10 | Robinhood: Pons, NOXA, Odyssey | PARTIAL | adapters exist; only Pons V2 proven active | M1 |
| 11 | Pons coordinated-launch safety (privileged / creator-linked / common-funder / simultaneous buyers) | MISSING on EVM (Solana has wallet graph, dump cluster, deployer intel) | | M5 |
| 12 | Robinhood reference repos inspected | MISSING | | M9 |
| 13 | Robinhood sequencer feed (+ delayed feed fallback), latency / gaps measured | MISSING | registry note only | M8 |
| 14–17 | Observation state machine for every token on all chains, windows T0..T+60, expiry, stored | PARTIAL | Solana: `token_observations`, T+5..T+60 snapshots, outcomes ledger; EVM: tokens are categorised and entered directly, no OBSERVING/QUALIFIED/EXPIRED states | M6 |
| 18–23 | Wallet performance model: 24H–180D windows, avg/median win and loss, profit factor, drawdown, FIFO ledger, INSUFFICIENT DATA | DONE in code for BSC / Robinhood (M3); Solana PARTIAL | `wallet_pnl.py` (FIFO lots, usually earns / usually loses, profit factor, drawdown, holds, best / worst), windows 24H / 7D (14D+ INSUFFICIENT DATA: 7-day profile history, 14-day trade retention); fees listed not subtracted (NOT VERIFIED per launchpad), gas not included; Solana profiles have no sells (launch_buyers) and say so | M3 |
| 24 | Nansen / MadeOnSol enrichment | MISSING | | M11 |
| 25–28 | Wallet discovery, validation gates, outlier test, regime test | PARTIAL | outlier test DONE (with / without best and top 3 trades, dependence level); validation gates and regime test still MISSING | M3b |
| 29 | Copy BUY ONLY / SELL ONLY / BUY+SELL | DONE (paper) | modes NOTIFY, BUY_ONLY, MIRROR (buy+sell), SELL_ONLY (M4); SELL ONLY exits PAPER positions only | M4 |
| 30–31 | Copy buy checks, chase guard; sell 20/50/100 % replication | DONE (paper) | `copy-engine`, partial sells on Solana (queued) and EVM | — |
| 32 | Copy position link fields | PARTIAL | `copy_positions` has target / token / target_tokens; source tx, displacement, slippage, latency live in `copy_events` | M4 |
| 33 | Copy latency stages on dashboard | PARTIAL | detection / analysis / risk / execution / total (ms); build / sign / submit / land are live-only (no live copy yet) | M4 |
| 34–35 | Copy safety never overridden; paper copy with would-have-won / missed | PARTIAL | safety enforced; missed-trade / would-have-won outcome not recorded | M4 |
| 36–44 | ML: wallet behaviour, mistake labels, frozen validation set, staged contribution, champion/challenger, no look-ahead, paper as training data | PARTIAL | Solana ML: multi-target shadow models, champion/challenger, labels, contribution 0 until validated, no-look-ahead audit; missing: wallet-behaviour labels (§37), EVM features, BUY/WAIT/REJECT/SELL/HOLD comparison (§41) | M12 |
| 45 | Manual BUY/SELL on all chains | PARTIAL | Solana only (`manual_trade.py`); EVM manual paper missing | M13 |
| 46–47 | Automatic-vs-manual sell diagnosis with stage-level evidence | DONE in code (M2); production result pending the server run | `tools/exit_diagnosis.py` (read-only report from `execution_orders` + position timeline + reconciliation); `tests/test_exit_diagnosis.py` | M2 |
| 48–53 | Provider dashboard, roles, plan health / UPGRADE REQUIRED | PARTIAL | `rpc_providers` (Solana, encrypted, failover, TEST); EVM via `BSC_RPC_URLS` / `ROBINHOOD_RPC_URLS`; TEST CONNECTION checks eth_getLogs; no roles, no plan-capability table | M7 |
| 54–55 | Token explorer all chains, explorer links per chain | PARTIAL | Solana token pages; EVM page lists tokens; link builder per chain not audited | M14 |
| 56–58 | Balances, gas reserve, INSUFFICIENT GAS, unified wallet (Solana + EVM accounts) | PARTIAL | Solana live wallet panel; EVM wallet module (`chains/evm/wallet.py`) read-only; gas-reserve NO_TRADE not wired for EVM paper | M13 |
| 59–61 | PnL always shown with colour, market cap $K/$M | PARTIAL | Solana positions show PnL; USD market cap done for Solana (G3); EVM positions page not audited | M14 |
| 60 | NO EMOJIS | DONE (this phase) | alert prefixes and the live page tick mark removed | M0 |
| 62–63 | 24/7 server-side workers | DONE | all engines are containers; dashboard is a viewer | — |
| 64–66 | GitHub / provider update monitor with Telegram + System Health | MISSING | | M15 |
| 67–70 | Multiple detection methods, source priority, NO_TRADE on provider failure | PARTIAL | NO_TRADE on unavailable data holds on both chains; single detection path per chain | M8 |
| 71–75 | Paper trading all chains feeding ML | PARTIAL | Solana complete; EVM paper entries exist, not yet ML features | M12 |
| 76–77 | Safety hierarchy, decision states EXECUTE / REDUCE_SIZE / WAIT / MANUAL_APPROVAL / REJECT / NO_TRADE | PARTIAL | Solana gate implements the hierarchy; decision words differ (PROMOTE/REJECT/...); MANUAL_APPROVAL not implemented | M6 |
| 78 | Preserve historical data | DONE | migrations are additive only | — |
| 79–81 | Test matrix, automatic-sell regression, 24/7 acceptance | PARTIAL | automatic-vs-manual exit regression added (`services/paper-trading/tests/test_exit_parity.py`); 24/7 acceptance (§81) is an operator procedure on the server, not automated | M2 |
| 82–83 | Final requirement audit and report | this document, updated per phase | | every phase |

## 3. Phase plan (smallest safe steps, evidence first)

| Phase | Content | Why this order |
|---|---|---|
| M0 | this audit; emoji removal | cheap, required by §60 |
| **M1** | Launchpad health + 7-day rule + raw-log check for the five quiet Robinhood adapters | the pasted evidence already shows five adapters with no activity; the dashboard must not present them as active |
| **M2** | Automatic-vs-manual sell diagnosis from existing `execution_orders` data; regression suite | §46 says diagnose before changing execution; the data already exists |
| M3 | Wallet P/L model: win/loss averages & medians, profit factor, drawdown, windows, outlier test, INSUFFICIENT DATA | the copy engine depends on it |
| M4 | Copy SELL ONLY, position link fields, missed/would-have-won outcomes | |
| M5 | Pons / EVM launch-window coordination checks | Pons is the most active Robinhood venue |
| M6 | EVM observation state machine | |
| M7 | Provider roles and plan-capability health | public RPCs are the current bottleneck |
| M8 | Robinhood sequencer feed, BSC pending-tx evaluation | |
| M9–M15 | research records, more launchpads, enrichment providers, ML extensions, EVM manual trading and gas, explorer/PnL UI audit, update monitor | |

EVM live execution (signing, nonces, submission) stays off and locked until
a launchpad is paper-verified and the operator explicitly authorizes a smoke
test.

## 4. M1 — launchpad health (2026-09-30)

- `activity_status` per launchpad: ACTIVE (activity in 24 h), QUIET (in 7 d),
  INACTIVE (none for 7 d after at least 7 d of monitoring), UNVERIFIED (no
  activity yet, under 7 d of monitoring), DEGRADED (EVM discovery cursor
  older than 15 min), DISABLED (registry inactive or operator OFF).
- Separate from the verification status; neither replaces the other.
- EVM activity comes from a daily rollup written in the same transaction as
  the stored events; only newly inserted launches / trades / migrations are
  counted, so re-scans after a restart do not double-count, and the rollup
  survives the 14-day `evm_trades` pruning.
- Solana: Pump.fun launches from `token_observations`, PumpSwap migrations
  from `token_events`; Solana trades per venue are not stored and are shown
  as "not tracked".
- The rollup starts empty on deploy: a Robinhood venue with no activity shows
  UNVERIFIED until 7 days of monitoring have passed, then INACTIVE.

## 5. M2 — automatic vs manual sells (2026-09-30)

Audit (code, before any change):
- Automatic and manual exits use ONE path. Dashboard SELL
  (`/api/trade/.../sell`), paper/position exit, CLOSE POSITIONS /
  EMERGENCY EXIT and the copy engine only set `exit_requested` and write a
  timeline event (`operator_exit` / `copy_exit_requested`); the Solana
  position loop (every 2 s; RPC-priced positions every 5 s, except that a
  requested exit is always due) calls `live_trading.manage_live_position`,
  which queues the SELL through `request_live_exit` exactly like a stop loss.
  The order worker (`process_order`) treats every SELL identically.
- Per order the system already records: decision time, created / signed /
  confirmed times, attempts, slippage (raised by `exit_slippage_step_pct`
  per failed exit), minimum output, route, final stage, error, RPC calls.

Added (no execution change, because no evidence points at a component yet):
- `tools/exit_diagnosis.py`: per origin (MANUAL / COPY / AUTOMATIC) the
  confirmed share, latency split (trigger → order, order → signed, signed →
  confirmed, total; median / p90 / max), positions that needed a second
  sell, failure stages and errors, slippage, minimum-output use, routes, and
  the count of positions whose tokens left the wallet outside YonixAlpha
  (a sell made in another wallet app has no order to compare).
- Regression (§80): `test_exit_parity.py` runs a stop-loss exit and a
  dashboard exit under identical conditions; both must create one full SELL
  with the same route, slippage and limits, close the position from the
  fill, update PnL and never need a second sell. Both pass.

Result: in code the two paths are identical, so a production difference
must come from runtime conditions (timing, quote age, slippage after
failures, RPC). The server run of `exit_diagnosis` decides which; only then
is a component changed.

### M2 production result (server, 2026-09-30, last 30 days)

| Origin | Sells | Confirmed | Median total | Needed a 2nd sell |
|---|---|---|---|---|
| AUTOMATIC | 72 (33 positions) | 97.2 % | 3.1 s | 1 |
| MANUAL | 3 | 100 % | 10.1 s | 0 |

Automatic sells are not slower: manual ones wait for the next position-loop
tick after the click (trigger → order median 1.1 s, max 10.5 s vs 13 ms).
Signing (0.3 s) and landing (2.4 s median) are the same for both.

The two failed automatic sells, traced with the orders, timeline and
reconciliation rows:
- MCASH, `Custom 3012` in simulation (17:43:40): reconciliation found the
  tokens gone from the wallet 27 s later (sold outside YonixAlpha).
  Simulation correctly refused a sell that could not fill; nothing sent.
- NEAR, `Custom 6005` on chain (22:24:58): a take-profit went to the Pump
  bonding curve after it completed (Pump BondingCurveComplete). The position
  moved to PumpSwap only at 22:25:15, when its price source changed; the
  PumpSwap sell then confirmed. **Fixed**: that rejection now moves the
  position to PumpSwap at once (`live_trading.curve_complete_rejection` /
  `switch_to_pumpswap`), the retry keeps the same slippage, and a migrated
  curve position is priced from its pool, never from the stale curve
  (`gate_manage.price_position`). Tests: `test_exit_parity.py`.

## 6. M3 — wallet profit and loss (2026-09-30)

- `wallet_pnl.py`: FIFO cost basis per token; a closed trade is one token's
  matched lots. Wins and losses are reported separately ("usually earns" /
  "usually loses": average, median, % and largest), with win rate, realized
  PnL, ROI, profit factor, max drawdown (cumulative realized PnL in exit
  order), average / median hold and best / worst trade.
- Outlier test (§27): total PnL with and without the best trade and the top
  3 trades, the best trade's share of gains, and a dependence level.
- Never shown as zero when missing (§23): fewer than 5 closed trades,
  windows longer than the retained history, open holdings (not valued),
  sells with no recorded buy (not counted as profit) and the fee / gas
  limits are all named in `reasons` / `notes`.
- Rebuild is bounded: the 2000 most active wallets per chain, their trades
  loaded 100 wallets at a time (it used to load every trade of 7 days at
  once; BSC records close to a million launchpad trades a day).
- Smart Wallets page: a detail row per wallet with the P/L blocks and the
  window table.

## 7. M4 — copy SELL ONLY (2026-09-30)

- New mode `SELL_ONLY`: a target's buy is never copied (`SELL_ONLY_TARGET`).
  When the target sells a token we already hold, the same fraction of our
  own open position is queued for exit (a target selling 40% of its bag
  queues 40% of ours; the fraction is capped at 100%).
- The fraction needs the target's holding before the sell. EVM: the sum of
  its recorded buys minus sells of that token; Solana: its trades in the
  observed stream. If the holding was never observed (tokens received by
  transfer, or bought before the retained history) nothing is guessed: the
  event is skipped with `TARGET_HOLDING_UNKNOWN`.
- The copy engine only queues the exit (plan key `copy_partial_exit`); the
  service that owns the position fills it on its next management pass
  through its normal exit path (data-evm `manage_pass` for EVM paper
  positions), then clears the plan and records `copy_partial_exit_filled`.
- PAPER only. LIVE positions are never selected by SELL ONLY; the copy
  controls (`blocked_by(..., "copy")`) still apply; the kill switch does not
  block it because it only reduces exposure.
- NOT VERIFIED in production yet: needs a SELL_ONLY target whose wallet
  sells a token we hold in paper.
