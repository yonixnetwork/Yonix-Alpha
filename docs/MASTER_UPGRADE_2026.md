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
| 2–3 | Research Jul–Sep 2026, repository records | DONE for every repository named in §7–12 (M9) | §3 records in section 18 (and bsc-mempool in section 17), code inspected at a pinned commit; earlier research in `MULTICHAIN_AUDIT_2026.md` §1, `PUMPFUN_EXECUTION_RESEARCH.md`, `SCANNER_INTELLIGENCE_2026.md` | M9 |
| 4 | Only Solana, BSC, Robinhood in the active UI | DONE | legacy futures/forex/grid removed (archive branch) | — |
| 5 | Launchpad health: activity status, last launch/trade/migration, 7d counts, verified flags | DONE in code (M1), NOT VERIFIED in production yet | `chains/activity.py`, table `launchpad_activity` (migration 0024), rollup written in `evm/store.persist_scan`, `/api/launchpads`, Launchpads page; `tests/test_launchpad_activity.py`, `test_control_center` | M1 |
| 6 | 7-day inactivity → INACTIVE, hidden from active filter, adapter kept, auto-reactivation | DONE in code (M1) | INACTIVE needs 7 days without activity AND 7 days of monitoring (else UNVERIFIED); Active / Archived tabs; discovery keeps scanning, so activity returns the venue to ACTIVE; Solana trade counts are "not tracked" (None), never 0 | M1 |
| 7 | Solana launchpads beyond Pump.fun/PumpSwap (LetsBONK, LaunchLab, Meteora DBC, Bags, Moonshot, Jupiter Studio) | PARTIAL: activity monitored, observe only (M10a); launch sites split by on-chain config (M10c) | Raydium LaunchLab, Meteora DBC and Moonshot in the registry as OBSERVE ONLY; activity from a 5-minute probe (section 19); per-site split from each sampled instruction's platform / pool config, LaunchLab sites named from their own PlatformConfig (section 22); StonkFun identified: it runs on LaunchLab (seen as one of its platform configs); trading NOT IMPLEMENTED | M10 |
| 8 | BSC: Four.meme, Flap verified; Genius.fun etc. researched | PARTIAL | Four.meme / Flap adapters, discovery live, read-only checks PASS; Four.meme X Mode detected by a plain-buy simulation (revert "A") and FAILS safety; AntiSniperFeeMode / template layout pending `tools.fourmeme_modes` on the server (section 22); Genius.fun researched and added OBSERVE ONLY (Pons V2 event decoder, two factories); Four.meme curves quoted in tokenized stocks (32 of 40 newest) kept out of BNB volume, wallet P/L and regimes (section 23) | M10 |
| 9 | BSC mempool wallet copying | DONE (measurement) | `evm.streams.PendingTxStream`: eth_subscribe newPendingTransactions (full bodies) over a dashboard WSS endpoint; matches copy targets / launchpads; REFUSED / LIMITED become UPGRADE REQUIRED; copy decisions stay on confirmed trades (§17) | M8 |
| 10 | Robinhood: Pons, NOXA, Odyssey | DONE | Pons V1 / V2 active and traded on paper; NOXA (paused since 2026-07) and The Odyssey (curve, instant, reflection; no activity seen, operator confirmed 2026-10-03) marked inactive: adapters, decoders and tests kept, discovery skips them, never traded (section 25) | M10 |
| 11 | Pons coordinated-launch safety (privileged / creator-linked / common-funder / simultaneous buyers) | DONE (paper; on-chain assumptions NOT VERIFIED until coordination_check runs on the server) | launch_coordination: 13 detections, configurable NO_TRADE / REDUCE_SIZE / MANUAL_APPROVAL / NONE, data-evm entries + EVM copy buys; see section 13 | M5 |
| 12 | Robinhood reference repos inspected | DONE (M9) | all seven inspected plus the official Pons contract source (section 18): Pons events match the official source, sequencer decoder matches 143 real transactions, feed signatures verified, router attribution measured by `tools.trader_attribution` | M9 |
| 13 | Robinhood sequencer feed (+ delayed feed fallback), latency / gaps measured | DONE | `evm.streams.SequencerFeed` in data-evm: Nitro broadcast decoding, resume by sequence number, delayed-feed fallback, gaps / duplicates / delay / matches, stream lead on copy events (§17). Real feed NOT VERIFIED from this environment | M8 |
| 14–17 | Observation state machine for every token on all chains, windows T0..T+60, expiry, stored | DONE (EVM, paper); Solana PARTIAL (own state names, see section 15) | EVM: `evm_observations`, full state machine, T0/T+5/T+10/T+20/T+30/T+60 snapshots with the §16 fields, adaptive MIGRATED / MOMENTUM windows, EXPIRED_NO_ENTRY, entries only while observed; Solana: `token_observations` + follow-ups, T+20m added | M6 |
| 18–23 | Wallet performance model: 24H–180D windows, avg/median win and loss, profit factor, drawdown, FIFO ledger, INSUFFICIENT DATA | DONE in code for BSC / Robinhood (M3; router / bot contracts excluded since M10b, section 20); Solana PARTIAL | `wallet_pnl.py` (FIFO lots, usually earns / usually loses, profit factor, drawdown, holds, best / worst), windows 24H / 7D (14D+ INSUFFICIENT DATA: 7-day profile history, 14-day trade retention); fees listed not subtracted (NOT VERIFIED per launchpad), gas not included; Solana profiles have no sells (launch_buyers) and say so | M3 |
| 24 | Nansen / MadeOnSol enrichment | DONE in code (M11), NOT VERIFIED against the real APIs (no keys here) | `enrichment.py`: Nansen labels + P/L summary (Solana, BSC), MadeOnSol FIFO P/L + KOL profile (Solana); off by default, daily call budget, refresh window; provider-reported, shown next to the own ledger, never a signal (section 25) | M11 |
| 25–28 | Wallet discovery, validation gates, outlier test, regime test | DONE for BSC / Robinhood (M3b); Solana INSUFFICIENT DATA (no sells recorded) | outlier test (M3); `wallet_validation` (12 configurable checks, per-day consistency, INSUFFICIENT DATA vs NOT VALIDATED); `market_regimes` (hourly volume / net flow / price range, migration 0026; CONSISTENT / REGIME_DEPENDENT); discovery stage COLLECTING_HISTORY → VALIDATED → PAPER_FOLLOWED / REJECTED, never auto-copied; Smart Wallets UI + rules editor. External sources (M11): Nansen smart-money traders and the MadeOnSol KOL leaderboard become CANDIDATES (once a day, off by default), listed for the operator and never copied; a candidate is validated only through its own trades here | M3b, M11 |
| 29 | Copy BUY ONLY / SELL ONLY / BUY+SELL | DONE (paper) | modes NOTIFY, BUY_ONLY, MIRROR (buy+sell), SELL_ONLY (M4); SELL ONLY exits PAPER positions only | M4 |
| 30–31 | Copy buy checks, chase guard; sell 20/50/100 % replication | DONE (paper) | `copy-engine`, partial sells on Solana (queued) and EVM | — |
| 32 | Copy position link fields | DONE (paper) | `copy_outcomes.link`: source wallet / tx / position, our position, ratio, mode, target vs our entry and exit, latency, displacement, PnL; slippage None for paper (measured on live fills only); on `/api/copy/positions` and the Copy page | M4b |
| 33 | Copy latency stages on dashboard | DONE (paper) | detection / analysis / risk / decision / execution / total (ms) on the Copy page; build / sign / submission / landing / confirmation are None and labelled live only (no live copy); the target's own submit time is not observable from confirmed trades | M4b |
| 34–35 | Copy safety never overridden; paper copy with would-have-won / missed | DONE (paper), NOT VERIFIED in production | safety enforced; every target buy (copied, skipped, notify-only) gets a paper outcome after 60 min (`copy_events.outcome`, migration 0025): simulated entry / exit, result, best / worst move, class COPIED / MISSED / BLOCKED_BY_SAFETY / FILTERED_BY_SETTINGS / NOT_COPYABLE / NOTIFY_ONLY; NO_PRICE_DATA instead of 0 % | M4b |
| 36–44 | ML: wallet behaviour, mistake labels, frozen validation set, staged contribution, champion/challenger, no look-ahead, paper as training data | PARTIAL: EVM samples, wallet labels and BUY / WAIT / REJECT comparison DONE in code (M12), NOT VERIFIED on real data until enough samples exist | Solana ML unchanged (multi-target shadow models, champion/challenger, contribution 0 until validated). M12: every BSC / Robinhood observation becomes a sample at T+5 (`ml/evm_samples.py`, table `evm_ml_samples`), labels from the following hour; wallet entries labelled SUCCESSFUL / FAILED / LATE ENTRY / PREMATURE EXIT / LATE EXIT, winners missed (`ml/wallet_labels.py`, table `wallet_trade_labels`, migration 0034); shadow models `shadow_evm_*` / `shadow_wallet_*` with time split and purge; §41 comparison of deterministic / risk / final / ML verdicts, in-sample ML verdicts excluded; ML contribution stays 0 % (section 26). Still missing: SELL / HOLD comparison (exits), a frozen validation set beyond the time-split holdout, staged contribution | M12 |
| 45 | Manual BUY/SELL on all chains | DONE (EVM paper; Solana unchanged), NOT VERIFIED on the server yet | Solana: `manual_trade.py` (unchanged). BSC / Robinhood: Manual BUY queues a request for the chain's data-evm worker, which runs `evaluate_entry(operator=True)`: every entry check except the strategy signal (switches, launchpad status, fresh safety, liquidity, coordination, limits, cooldown, gas, risk plan); BLOCKED lists every reason; observe-only venues refused; paper only (EVM live locked). Manual SELL: the existing operator exit, now with a SELL button on EVM positions (section 24) | M13 |
| 46–47 | Automatic-vs-manual sell diagnosis with stage-level evidence | DONE in code (M2); production result pending the server run | `tools/exit_diagnosis.py` (read-only report from `execution_orders` + position timeline + reconciliation); `tests/test_exit_diagnosis.py` | M2 |
| 48–53 | Provider dashboard, roles, plan health / UPGRADE REQUIRED | DONE (routing + reporting); mempool / sequencer streaming is M8 | roles per endpoint (dashboard, .env and public), role-preferred routing on Solana and EVM with counted fallbacks, operator-stated plan, WSS stored for BSC / Robinhood, plan health from observed limits on RPC / Data Providers and System Health; see section 16 | M7 |
| 54–55 | Token explorer all chains, explorer links per chain | DONE in code (M14) | one search across Solana / BSC / Robinhood (name / symbol prefix, mint / contract, creator, wallet; migration 0032 indexes); EVM token page with every §54 field (holders: not tracked on EVM, stated, never 0; ML: NOT_AVAILABLE on the token page); `explorer_links` builds every link for the token's own chain from confirmed URL formats only; Robinhood has no confirmed DEX page, shown unavailable with the reason (section 24) | M14 |
| 56–58 | Balances, gas reserve, INSUFFICIENT GAS, unified wallet (Solana + EVM accounts) | DONE in code (M13); EVM live balance NOT VERIFIED on the server yet | YonixAlpha Trading Wallet (`/api/wallets/overview`, Wallets page): Total / Available / Reserved / Gas reserve / Trading balance per chain, LIVE and PAPER separate, USD from SOL / BNB / ETH rates; EVM entries need gas for the buy and the sell plus the gas reserve (INSUFFICIENT_GAS / GAS_PRICE_UNAVAILABLE, NO_TRADE); Solana LIVE entries need the fee reserve (INSUFFICIENT GAS in the gate); Solana paper unchanged (section 24) | M13 |
| 59–61 | PnL always shown with colour, market cap $K/$M | DONE in code (M14) | `position_pnl.view` on every positions list (paper, live, copy, EVM, overview, token pages): PROFIT / LOSS / BREAKEVEN with %, PNL_UNAVAILABLE with the reason when there is no mark (never a bare OPEN, never 0); entry, current, quantity, value, unrealized, realized, fees, net, peak, drawdown; green / red / neutral with TrendingUp / TrendingDown / Minus icons; EVM market cap in USD ($950 / $9.5K / $1.05B) on the token list and token page (section 24) | M14 |
| 60 | NO EMOJIS | DONE (this phase) | alert prefixes and the live page tick mark removed | M0 |
| 62–63 | 24/7 server-side workers | DONE | all engines are containers; dashboard is a viewer | — |
| 64–66 | GitHub / provider update monitor with Telegram + System Health | DONE in code (M15); GitHub path NOT VERIFIED against the real API from the build environment (blocked there), PyPI path checked against pypi.org | `update_monitor.py` in the ml service: 14 repositories + 8 pinned dependencies every 6 h via GitHub REST and PyPI JSON (no HTML); classes INFO / UPGRADE_AVAILABLE / BREAKING_CHANGE / SECURITY_UPDATE / PROVIDER_CHANGE / ACTION_REQUIRED; baseline first check; history in `update_events` (migration 0031); Telegram kind `infrastructure_update`; System Health → Research / Updates with acknowledge; never deploys (section 21). M15b: every update says what to do (APPLIED / PIN BUMP / INTEGRATION CHECK / REVIEW ONLY); `DEPLOY_PULL=1` for base-image patches; first dependency round applied (section 25) | M15 |
| 67–70 | Multiple detection methods, source priority, NO_TRADE on provider failure | PARTIAL | NO_TRADE on unavailable data holds on both chains; single detection path per chain | M8 |
| 71–75 | Paper trading all chains feeding ML | DONE in code (M12), NOT VERIFIED on real data yet | Solana complete; EVM: every observation is a sample whether traded or not, a traded one carries the executable return of its closed paper position (fees and taxes included); ML knowledge on ML Review (samples by kind, wins / losses, missed winners, copy outcomes, models, contribution 0 %) (section 26) | M12 |
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

## 8. BSC event coverage check (2026-09-30, server)

Every log the Four.meme and Flap contracts emitted over ~1500 blocks was
counted by event type, and a closed block range was compared with the
database:

- Stored equals on chain: Four.meme 37/37 trades and 53/53 launches, Flap
  2540/2540 trades and 82/82 launches; no `discovery_gap_skipped` in 24 h.
- Undecoded events identified by their signature hash: Four.meme
  `TokenPurchase2` / `TokenSale2` (one per trade, a second event of the same
  trade); Flap `FlapTokenCirculatingSupplyChanged` / `FlapTokenProgressChanged`
  (one per trade) and `TokenCurveSetV2` / `TokenDexSupplyThreshSet` /
  `TokenVersionSet` (one per launch). None is a missed trade. A few Flap
  events (one about as frequent as trades) remain unidentified; none matches
  the trade or launch counts.
- Missing: Flap graduation `LaunchedToDEX(token, pool, amount, eth)` was not
  decoded (graduations were only found by polling getTokenV8Safe, so the
  activity rollup showed 0 Flap migrations). Now decoded as a migration;
  layout confirmed from a real log (all fields in data; 200M tokens and
  ~89.29 BNB per graduation) and covered by a test built from that log.
- RPC: publicnode answered 403 even for single blocks right after a burst of
  log requests, and the client then skipped eth_getLogs on it for 30
  minutes (BSC's only public logs endpoint). A node that already served
  logs now gets a short doubling cooldown instead.
- Four.meme graduation: the community integration (four-meme-community/
  four-meme-ai) lists the same TokenManager2 address and the same
  `LiquidityAdded(base, offers, quote, funds)` event we decode. On the server
  none of the 6,392 Four.meme tokens tracked had graduated by its own
  contract state (stage DEX = 0), so no graduation was missed. Flap: 48
  graduated tokens, 44 of them found by state polling before the
  LaunchedToDEX fix (no event time).

## 9. M4b — copy link, latency stages, paper copy outcomes (2026-10-01)

- Link (§32): `copy_outcomes.link` builds each copied position's link from
  rows already stored (copy_events, copy_positions, paper_positions), so it
  also covers positions opened before this change. Prices are native per
  whole token. Displacement = our entry against the target's. Slippage is
  None for paper (the paper fill is the executable quote at decision time).
- Latency (§33): `decision` stage added; build / sign / submission /
  landing / confirmation are None (never 0) and listed as live only.
- Outcomes (§35): the copy engine evaluates every target BUY 60 minutes
  after it was seen, from trade prices of the same token (EVM: evm_trades,
  kept 14 days; Solana: the pump stream, kept 3 hours, so a Solana event
  not evaluated within ~2h45 becomes NO_PRICE_DATA). Entry: first trade
  after we saw it. Exit: the target's own sell for MIRROR targets, else the
  last trade at the horizon. Before our fees, price impact and gas.
  Evaluated once (`outcome_at`). It never changes a decision.
- Copy page: open / closed copy positions with link columns and a detail
  row; "Paper copy outcomes" table per target and per class; outcome column
  on copy events; full latency stage line. Rendered locally with seeded
  data (no console errors); NOT VERIFIED with real target wallets.

## 10. RPC alerts seen in Telegram (2026-10-01)

- `[data-solana] rpc_health_check_failed`: the 30-second getHealth check
  in data-solana and the three Solana engines sent every failure straight
  to Telegram, bypassing the alert throttle every other service uses. It now
  goes through `notify.alert_error` (once per 5 minutes per event, with the
  number suppressed); every failure is still stored as a SystemEvent.
  The failure itself is the endpoints: in the reported snapshot the
  SOLANA_RPC_URL Helius key had 0 successes in 88 calls (all HTTP 429),
  Chainstack 0 / 20 (HTTP 403), Ankr 0 / 2 (getHealth not supported,
  timeouts), dashboard Helius 44 % with 7.7 s latency; only Alchemy was
  healthy (96.6 %).
- `[data-evm] robinhood.<launchpad>.discovery_failed: all cooling down`:
  Robinhood Chain has no ROBINHOOD_RPC_URLS configured, so data-evm uses the
  single public endpoint, which rate-limits. A cooldown loses nothing (the
  cursor resumes; a backlog over 60 minutes is skipped and alerted). Such
  RPC-unavailable failures are now alerted once they persist 2 minutes
  (per launchpad and per chain); any other discovery error is alerted at once.

## 11. One place for every chain's RPC (2026-10-01)

- RPC & Data Providers now takes BSC and Robinhood Chain endpoints as well as
  Solana (same table, `rpc_providers.chain`; URLs encrypted; admin password
  to add or change a URL). data-evm and copy-engine apply the list on the
  configuration revision (no restart). Order: dashboard (150) → .env
  BSC_RPC_URLS / ROBINHOOD_RPC_URLS (500+) → built-in public (900+); .env and
  public endpoints can be reordered or disabled; a chain never ends up with
  no endpoint.
- EVM TEST CONNECTION (`chains/evm/rpc_registry.test_evm_rpc`, also used by
  `tools/evm_rpc_probe`): chain id must match; eth_getLogs over 10 / 100 /
  1000 / 2000 blocks of the chain's busiest launchpad contract, ending 5
  blocks under the head. The old probe asked a dead Four.meme V1 contract up
  to the exact head and reported the working publicnode as NO LOGS.
- The EVM client now remembers the eth_getLogs span each endpoint accepted
  (a 10-block free tier is no longer asked 2000, 1000, ... first on every
  call) and retries twice that after 50 answers.
- Provider research (2026-10-01, from provider pages / docs via search):
  Alchemy and QuickNode both serve Solana, BNB Smart Chain and Robinhood Chain
  mainnet. Free tiers limit eth_getLogs (Alchemy free: 10 blocks on BNB and
  Robinhood; QuickNode free trial: 5 blocks); paid plans lift it (Alchemy Pay
  As You Go: unlimited on BNB and Robinhood; QuickNode paid: 10,000 blocks).
  Exact per-chain URL formats are copied from the provider's dashboard, not
  built by this app (NOT VERIFIED here).

## 12. M3b — wallet validation, market regimes, discovery (2026-10-01)

- Profiles now cover the full 14 days of retained trades (was 7), so the
  14D window and weekly checks are measured.
- Validation (§26, `wallet_validation`): trades, closed trades, active days
  and weeks, unique tokens and history coverage decide whether there is
  enough history (else INSUFFICIENT DATA, with the missing checks named);
  profitable days, share of active days profitable (consistency), max
  drawdown as % of capital put in, profit factor, best-trade share of gains
  and median return decide VALIDATED / NOT VALIDATED. Every check is shown
  with value and requirement; thresholds are edited on the Smart Wallets page
  (platform setting `wallet_validation`).
- Regimes (§28, `market_regimes`): each completed hour of a chain's
  launchpad trades is summarised once (`market_regime_hours`, migration
  0026): volume, net buy/sell flow, median price range. Hours are classified
  HIGH/LOW volume, BULLISH/BEARISH, HIGH/LOW volatility against the window's
  medians (launchpad-market regimes, not the BNB / ETH price). A wallet's
  closed trades are split by the regime of the hour they were opened in:
  CONSISTENT, REGIME_DEPENDENT (names where it lost) or INSUFFICIENT_DATA.
  The first run starts at the first traded hour and fills up to a week of
  hours per profile rebuild.
- Discovery (§25): COLLECTING_HISTORY → VALIDATED → PAPER_FOLLOWED, or
  REJECTED. A validated wallet's last 20 first-buys are replayed with the
  copy outcome evaluator (entry 3 s after their buy at the first trade
  after it, exit at their sell or 60 min; refreshed hourly). Nothing adds a
  copy target: that stays the operator's decision.
- Rendered locally on profiles built by the real rebuild: a consistent
  wallet VALIDATED and paper-followed, a wallet carried by one 30x trade
  REJECTED. NOT VERIFIED on production data yet.

## 13. M5 — launch-window coordination (2026-10-01)

Master §11: Pons is active, not safe. Every EVM token in an entry category is
assessed with its safety check (data-evm, every 2 minutes while it trades)
and again before an EVM copy buy when the assessment is older than 5
minutes (`launch_coordination`, migration 0027: `evm_tokens.coordination`,
`evm_wallet_funders`).

| Master §11 item | Detection | Data |
|---|---|---|
| anti-sniping exemptions | DECLARED_EXEMPTIONS | Pons V2 launch calldata (four entrypoints below) |
| privileged wallets / special treatment | PRIVILEGED_BUYERS | declared list, and `curve.currentSnipeTaxBps(buyer)` at the buy's block = 0 while a reference address pays tax |
| creator-linked wallets | CREATOR_BOUGHT | deployer, fee recipient, launch sender, launchAndBuy opening-buy recipient |
| creator-funded wallets | CREATOR_FUNDED_BUYERS | first funder from the explorer |
| common funders | COMMON_FUNDER | same (disperse contracts resolved to their caller; operator ignore list for exchanges / bridges) |
| wallets buying almost simultaneously | LAUNCH_BLOCK_BUNDLE, NEAR_SIMULTANEOUS_BUYERS | stored trades |
| abnormal initial ownership | ABNORMAL_INITIAL_OWNERSHIP | Pons V2 mints all supply to the curve: totalSupply - balanceOf(curve) vs net curve buys at the last stored block |
| supply concentration | WINDOW_SUPPLY_CONCENTRATION, SINGLE_WALLET_CONCENTRATION | curve buys minus sells of window buyers / totalSupply |
| coordinated exits | COORDINATED_EXIT | window buyers' sells within a span |
| (extra) fresh wallets | FRESH_WALLET_CLUSTER | eth_getTransactionCount at the window's last block |
| (extra) unknown launch / failed reads | WINDOW_NOT_OBSERVED, COORDINATION_DATA_UNAVAILABLE | NO_TRADE by default (provider failure is never "safe") |

- Actions per detection are set on EVM Markets → Launch-window coordination
  (platform setting `launch_coordination`); the strictest applies. NO_TRADE
  blocks, MANUAL_APPROVAL waits for an operator approval bound to the exact
  findings (fingerprint) and expiring after `approval_minutes`, REDUCE_SIZE
  multiplies the paper size, NONE reports only. A NO_TRADE finding cannot be
  approved. Copy skips count as BLOCKED_BY_SAFETY in the copy outcomes.
- Thresholds are configurable defaults, not verified thresholds; the
  per-launchpad summary (24 h: assessed, detected, actions, detections,
  checks without data) is there to tune them on real launches.
- Funding: Robinhood Chain uses its public Blockscout; BSC uses Etherscan
  API V2 with `ETHERSCAN_API_KEY` (Settings → Block explorers, with a
  connection test). Without a key the funding checks are NOT_CONFIGURED,
  never read as "no common funder". Holdings follow launchpad trades only.

Research (§12) used here:
- github.com/ponsdotdev/pons-labs (commit b51431f, 2026-09-29),
  `PonsV2LaunchFactory`: snipe tax starts at 99 % and decays over 15 s
  (max 60 s); `launchToken(params, configId, pairToken, address[] exemptions)`
  is documented as "the sanctioned pathway for organized teams that bundle
  their opening buys"; up to 32 exemptions; deployer and fee recipient are
  exempt automatically; `launchTokenFor` is callable only by the launch
  forwarder. The published `PonsV2BondingCurve` source has no snipe-tax code
  although the factory calls `exemptFromSnipeTax` on it: the repository is
  not the complete deployed source.
- github.com/slightlyuseless/pons-launch-engine: an open-source multi-wallet
  "launch and buy your own token" engine for Pons. Gives the launchAndBuy
  router (0xe33E9E479dF8802cb0866d5d05258bEc4cF62948) ABI with its
  exemption list, the full V2 TokenParams struct (ends with
  expectedEconomics and a CREATE2 salt) and the curve view
  `currentSnipeTaxBps(recipient)`. It is exactly the coordinated launch the
  master prompt warns about; nothing is imported from it.
- github.com/yesiambroke/pons-terminal: a trading terminal; nothing used.
- Selectors (from the struct above): launchToken 0xf35abbcf, launchToken
  with list 0xa72101af, launchTokenFor 0xd6a0eef5, launchAndBuy 0xf85f8e41.
  NOT VERIFIED against real launches from this sandbox (Robinhood RPC not
  reachable here): run
  `python -m yonixalpha_core.tools.coordination_check` in data-evm on the
  server; an unrecognised selector is reported, never read as "no
  exemptions".
- Tests: 10 core (constructed launches, ABI-encoded calldata for each
  entrypoint, the snipe-tax confirmation on a fake node, approval rules,
  mocked Blockscout / Etherscan), a data-evm pipeline test (bundle blocked,
  then reported only after the operator sets NONE), a copy-engine test (a
  target buying into a bundled launch is not copied), an API test
  (settings, approval, revoke, summary), provider tests for both explorers.

## 14. M5 on the server (2026-10-01, deploy a9792a4)

`coordination_check` on production:
- Pons V2 launches (last 30) by entrypoint: launchAndBuy router 9,
  factory.launchToken 7, factory.launchToken with an exemption list 6,
  unrecognised 8. The decoded selectors are now VERIFIED on real launches.
  Non-empty exemption lists: 3 (sizes 1, 1 and 20 wallets).
- Unrecognised: 0xa3a3ee69 (x2) is `launch(TokenParams, address)`, a
  third-party wrapper (found by signature search; named in the report, its
  exemption list is not in its calldata). 0x89942133 (x1) and 0x0a5f3d53
  (x5) are not identified yet; the tool now prints the contract they were
  sent to and checks whether the launch receipt names the exempted wallets
  (an event per exemption would make the list readable for every
  entrypoint). Until then those launches show DECLARED_EXEMPTIONS UNKNOWN,
  never "none"; PRIVILEGED_BUYERS is still checked on chain for them.
- `currentSnipeTaxBps` answers at a past block (a buyer at the launch block
  paid 9900 bps, like the reference address): the on-chain exemption check
  works. VERIFIED.
- Robinhood Blockscout answered HTTP 403 to the default client; the funding
  lookups now send a browser-like User-Agent and report the response body
  when refused. NOT VERIFIED until the next run.
- One Pons V2 launch assessed: creator-linked buy in the window (REDUCE_SIZE),
  everything else passed; one Four.meme launch: nothing detected.

## 15. M6 — token observation (2026-10-01)

Master §14-17. Every BSC / Robinhood token enters observation before it can
be traded (`chains/evm/observation.py`, migration 0028 `evm_observations`,
one row per token and category, never deleted):

- Opened at the launch (FRESH), the migration (MIGRATED) or the first
  momentum signal (MOMENTUM), with `observation_started_at`, deadline and
  reason.
- States: DISCOVERED → OBSERVING → ANALYZING (signal not met) → QUALIFIED →
  WAITING_FOR_ENTRY (held by limits, launchpad evidence, a coordination
  approval…, the blocker recorded) → ENTRY_PENDING → ENTERED; OBSERVING →
  NO_ENTRY → EXPIRED (`expiry_reason` EXPIRED_NO_ENTRY plus the last
  blocker); OBSERVING → SAFETY_FAILURE → REJECTED after 3 consecutive
  safety FAILs (one FAIL is SAFETY_FAILURE only, UNKNOWN never rejects).
  Every transition is in the state history.
- data-evm enters a token only while its observation for the current
  category is open: an expired, rejected or entered observation is not
  entered again.
- Snapshots T0, T+5, T+10, T+20, T+30, T+60 (configurable), computed as of
  that moment from stored trades even when the pass runs late, and kept
  after an entry through the window (the path after entry is outcome data):
  price, market cap (native), interval and total volume, buy / sell volume,
  buyers, sellers, effective buyers (without the wallets the coordination
  check tagged), holders and holder growth (from launchpad trades),
  liquidity and its change, curve progress, creator trades, top buyer share,
  smart-money buyers (wallets the profile rebuild VALIDATED), net flow,
  organic net flow, coordination (manipulation), safety, decision. ML is
  None with "no EVM model yet": nothing is invented.
- Adaptive windows (MIGRATED, MOMENTUM): while the token keeps trading
  (>= 5 trades in the last 5 minutes) the deadline moves out 10 minutes at a
  time, up to 180 minutes. All of it is set on EVM Markets → Observation.
- Solana keeps its own observation (`token_observations`: OBSERVING,
  PROMOTED / REJECTED / EXPIRED, follow-ups); T+20m was added to its
  follow-ups. Mapping its outcomes onto the §15 state names is not done.
- Tests: core (snapshots as of their time, expiry without entry, terminal
  states, qualified → waiting → entered, adaptive extension, safety
  rejection, settings), data-evm pipeline (observed from launch, entered
  through it, snapshots after entry), API (list, counts, held-by, detail,
  settings).

## 16. M7 — provider roles and plan health (2026-10-01)

Master §48-53 (`provider_roles`, migration 0029: `rpc_providers.roles`,
`rpc_providers.plan`; .env / public endpoints keep theirs in the existing
override settings):

- Roles DISCOVERY, MARKET_DATA, EXECUTION, CONFIRMATION, HISTORICAL_DATA,
  WALLET_DATA can be ticked per endpoint on RPC / Data Providers, for
  dashboard, .env and built-in public endpoints alike. Every request maps
  to a role by its method (Solana: sendTransaction / simulate / blockhash /
  fees = EXECUTION, signature statuses / getTransaction = CONFIRMATION,
  signature history / blocks = HISTORICAL_DATA, balances / token accounts
  = WALLET_DATA, the rest MARKET_DATA; EVM: eth_getLogs = DISCOVERY, reads
  at a numbered past block = HISTORICAL_DATA, receipts / tx by hash =
  CONFIRMATION, balance / nonce = WALLET_DATA, send / gas = EXECUTION) and
  goes to the healthy endpoints holding that role first.
- An endpoint without roles serves every role, so nothing changes until
  roles are set. When no healthy holder exists the request still goes out
  to any usable endpoint and the fallback is counted (shown per role and
  chain); roles never stop traffic.
- Plan: the plan name the operator records per endpoint (not verified with
  the provider), shown with every finding.
- WSS URLs are accepted and stored (redacted) for BSC and Robinhood too,
  for mempool / sequencer streaming (M8); nothing consumed them at M7 (BSC WSS is used by the M8 pending stream, §17).
- Plan health (`GET /api/rpc/plan-health`, RPC / Data Providers and System
  Health): UPGRADE REQUIRED only on an observed limitation — sendTransaction
  or simulate refused ("AUTOMATIC SELL LATENCY MAY BE LIMITED BY CURRENT
  RPC PLAN"), getProgramAccounts / holder / history methods refused, >= 5 %
  HTTP 429 over >= 200 requests, eth_getLogs refused or served under 100
  blocks (the span data-evm learned on real requests), Helius
  transactionSubscribe refused in the probe — or on a chain served by
  public endpoints only. Authentication refusals are CONFIGURATION; no
  WSS for an EVM chain is INFO. Each finding: provider, current plan,
  required capability, observed limitation, impact, recommended upgrade.
- Launch coordination (M5) now reads the snipe-tax exemptions from the
  launch receipt: the curve emits `SnipeTaxExempted(address)` once per
  exempted wallet (identified on the server: 31 declared + deployer + fee
  recipient = 33 events), so every entrypoint, including the two still
  unidentified wrappers, has a readable list. The calldata decoder stays
  as the fallback.

## 17. M8 — transaction streams (2026-10-01)

Master §9 and §13 (`yonixalpha_core.chains.evm.streams`, run by data-evm;
settings key `evm_streams`; no migration).

Robinhood Chain sequencer feed (§13):

- Robinhood Chain is an Arbitrum Orbit chain. data-evm connects to
  `wss://feed.mainnet.chain.robinhood.com` (Nitro broadcast format,
  `Arbitrum-Feed-Client-Version: 2`) and checks the `Arbitrum-Chain-Id`
  response header against 4663 (WRONG_CHAIN otherwise).
- Each message is decoded: L2 batches are unrolled, every signed
  transaction (legacy, 2930, 1559, 7702) gives hash, to, value and
  selector. Compressed messages (kind 7) are counted, not decoded.
- The sender is recovered for every transaction to a watched contract
  (launchpad contracts and curves known to the adapters, plus the Pons
  launchAndBuy router) and for at most `recover_budget_per_s` others per
  second (pure-Python recovery costs about 7 ms). A transaction from an
  enabled copy target or to a launchpad is kept in Redis for 15 minutes
  with the time it was seen.
- Measured and shown: state, reconnects, messages, transactions, sequence
  gaps and missing messages, duplicates / out-of-order, feed delay median
  and p95 (message timestamp, 1 s resolution), matches.
- On reconnect the client asks for the last sequence number it has seen
  (that message arrives again and is dropped quietly), so a short drop
  loses nothing. Changed in M9: asking for the next number, past the
  server's tail, replays the whole backlog (section 18). After `fallback_after_failures` (3) failed
  connections it uses the delayed feed and retries the primary every
  `primary_retry_minutes` (10). Delayed-feed sightings are labelled
  `delayed_feed`, never as primary-speed.

BSC pending transactions (§9):

- `eth_subscribe newPendingTransactions, true` over the first enabled
  WSS URL on RPC / Data Providers for BSC. Full bodies are matched against
  copy targets and launchpad contracts.
- A provider that refuses the subscription is REFUSED, one that streams
  hashes only is LIMITED; both are UPGRADE REQUIRED on plan health
  ("mempool / pending transactions") and retried every 30 minutes. No WSS
  endpoint is NOT_CONFIGURED (INFO). The public BSC endpoints do not offer
  this, so the stream is idle until a provider WSS is added.

What the streams do and do not do:

- They never trade. A sequenced or pending transaction can still revert,
  so copy decisions stay on confirmed trades (master: "A target wallet
  buying a token is NOT permission to buy it").
- The copy engine looks up every confirmed copy-target trade; when a stream
  saw it first, the copy event's latency carries `stream_source` and
  `stream_lead` (ms between the stream sighting and confirmed-trade
  detection). RPC / Data Providers shows, per chain over 24 hours, how
  many copy-target trades a stream saw first and the median / p95 lead.
- Settings (RPC / Data Providers, "Stream settings"): feed on/off, feed
  and delayed-feed URLs (wss:// only), fallback threshold, primary retry,
  sender-recovery budget, BSC pending on/off. data-evm re-reads them every
  minute and reconnects when the feed settings change.

Research record — 1chimaruGin/bsc-mempool (master §3):

| Field | Value |
|---|---|
| Repository | github.com/1chimaruGin/bsc-mempool |
| Commit reviewed | 212d4463 (2026-06-12) |
| License | Apache-2.0 OR MIT |
| Language | Rust |
| Chain | BSC |
| Purpose | copy trading of a KOL wallet list from the mempool |
| How it gets pending transactions | its own synced bsc-geth full node over IPC (`eth_subscribe newPendingTransactions, true`), submission through BlockRazor / Puissant relays |
| Other parts | KOL whitelist, adaptive trailing exits, shadow / tiny / full rollout phases |
| Fit for this server | NO: a BSC full node needs far more than the 2 vCPU / 2 GB server; relays need accounts |
| Decision | PARTIALLY USE (ideas only, no code): the pending-transaction subscription with full bodies is used against a provider WSS; the shadow-first rollout matches our paper-first copy trading. Relay submission and mempool-triggered buys are not adopted |

Tests: `packages/core-py/tests/test_evm_streams.py` (decoding of real
signed transactions, batches, sequence gaps / duplicates, delay, matches,
resume and delayed-feed fallback and pending full / hash-only / refused
against local WebSocket servers), `test_provider_roles.py`
(stream findings), data-evm stream wiring, copy-engine stream lead, API
`/api/evm/streams` and stream settings. The real Robinhood feed and a real
BSC pending stream are NOT VERIFIED here (no egress from this
environment); check the panel on the server after deploy.

## 18. M9 — reference repository records (2026-10-01)

Master §3, §7, §8, §12. Every repository named there was cloned and its code
read at the commit given; nothing was installed into YonixAlpha or run
against a wallet. "Last update" is the latest commit; "meaningful" is the
latest commit that changed files (several repositories add empty commits
that only refresh their activity date, so activity is not evidence of
quality).

### What changed in YonixAlpha because of it

- **Sequencer feed client (M8), checked against real data.** chainstacklabs'
  captured mainnet frames: our decoder gives the same hash, to, selector,
  sender, value and nonce for all 143 transactions in 39 messages.
  Fixes from their measurements:
  - Resume asks for the last sequence number seen, not the next one. A
    number past the server's tail makes Nitro replay its whole backlog of
    about 1,200 messages.
  - The first connection's replayed history is sequenced but not matched
    or timed.
  - A re-sent sequence number with another block hash counts as a reorg.
  - permessage-deflate is stated explicitly (the feed refuses clients
    without it since 2026-09-17).
- **Feed signature check (on by default).** Every Robinhood feed message
  carries `signatureV2`. Our own implementation of Nitro's preimage
  recovers the sequencer key `0xdaa5…87f4` on all 39 real messages and
  scatters with a wrong chain id. A message that fails is dropped before
  it moves the sequence. Many failures become a CONFIGURATION plan-health
  finding (key rotation). The key is the one chainstacklabs found to be
  the L1 batch poster; it is NOT re-checked on L1 from here.
- **coincurve pinned.** It makes sender recovery 6 times faster
  (10.3 to 1.7 ms measured) and feed signatures cheap.
- **Router attribution, measured before changing anything.** The official
  Pons source emits `CurveBuy(msg.sender, recipient, ...)` /
  `CurveSell(msg.sender, ...)`. A trade through a router (pons-terminal's
  TradeRouters, the launchAndBuy router) is therefore stored with the
  router as the trader. Wallet profiles, smart-wallet discovery and copy
  detection would see one busy "wallet" instead of the people behind it.
  `tools.trader_attribution` (read-only) reports, per launchpad:
  - the busiest traders, contract or wallet, with a known-router label;
  - how many Pons curve buys name a different recipient.

  Attribution changes only after that output is seen.
  `chains/evm/known_routers.py` holds the router addresses found in the
  code. They are labels only, never trusted.
- **Pons events verified against the official source.** The Solidity
  source is ponsdotdev/ponsfamily at 44a3db9, MIT. Every Pons event we
  decode matches it by topic hash: V1 TokenLaunched, V2 TokenLaunched,
  CurveBuy, CurveSell, CurveCompleted, PoolGraduated and LaunchSwept.
  Both factory addresses match the registry.

### Gaps found, not fixed in M9

- **Four.meme X Mode and AntiSniperFeeMode tokens are not detected.**
  four-meme-ai names the on-chain flags:
  - X Mode: `_tokenInfos[token].template & 0x10000`. X Mode tokens must
    be bought with a signed method; a plain buy reverts.
  - AntiSniperFeeMode: `_tokenInfoEx1s[token].feeSetting > 0`.
  - TaxToken: `(template >> 10) & 0x3F == 5`; we already detect it via
    `feeRate()`.

  The struct layouts of those getters are not published there. Decoding
  a guessed layout could misread every token, so this needs the verified
  TokenManager2 ABI or an on-chain probe first (M10).
- **Pons V1 legacy factory** `0x0c37a24F5D23A486FA692d1500881d698B1F77a4`
  (named by pons-launch-engine and casatrickdev) is not in the registry.
  It is "legacy" in both, so it is left out until a server scan shows
  launches there.
- **Wash-volume tooling exists for Pons.** pons-terminal's V2 router has
  `roundTrip` (buy and sell in one transaction) and a "Volume" mode of
  buy-then-sell cycles. Volume on Pons tokens can be manufactured by design.
  The manufactured-pump detector covers Solana; an EVM equivalent that
  flags round trips is M10 / M12 work.
- **Meteora DBC allows Token-2022 base tokens with transfer hooks**
  (0.2.x). Our Solana safety already parses Token-2022 extensions
  including transferHook, so a DBC adapter can reuse it.

### Records

**chainstacklabs/robinhood-chain-sequencer-feed**
- URL: github.com/chainstacklabs/robinhood-chain-sequencer-feed.
- Commit: 8ea0972 (2026-09-25, also the latest meaningful one).
- License: Apache-2.0. Language: Python 3.11+. Chain: Robinhood Chain.
- Purpose: decode the sequencer feed (`rhfeed`), with an optional
  Offchain Labs relay.
- Production status: "Experimental. A reference implementation, not for
  production use".
- Dependencies: websockets, coincurve, eth-hash, orjson; Docker relay
  pinned to nitro v3.12.0.
- Execution: none. Monitoring: yes (filter by to / selector / sender,
  verify signatures).
- Latency (theirs, one machine): ~4 µs per transaction for cheap fields,
  ~70 µs with the sender; the chain does ~71 tx/s.
- Security:
  - signature verification is opt-in;
  - a stock relay verifies nothing (documented by them);
  - no keys involved.
- Limitations:
  - soft confirmations only: a transaction can still revert or be voided
    by ArbOS compliance filtering;
  - the relay hides reorgs.
- Decision: PARTIALLY USE.
  - Their measurements, the preimage description and 12 captured frames
    are used: the frames are a test fixture with the Apache-2.0 licence,
    unmodified.
  - No code taken; our decoder and signature check are our own and
    tested against theirs.

**ponsdotdev/ponsfamily (official Pons contracts)**
- URL: github.com/ponsdotdev/ponsfamily.
- Commit: 44a3db9 (2026-10-01), 399 commits.
- License: MIT. Language: Solidity 0.8.26 / 0.8.30. Chain: Robinhood Chain.
- Purpose: V1 CREATE2 factory with locked Uniswap V3 liquidity; V2
  bonding curve graduating to a Uniswap V4 pool with hook, creator tax,
  snipe tax and buyback vault.
- Production status: deployed; factories `0xA5aA…1feB` (V1) and
  `0x7eD5…EC7e` (V2), verified on chain according to the README.
- Execution / monitoring: n/a (contracts).
- Security: says to verify deployed bytecode against source before
  trusting an address; NOT VERIFIED from here.
- Decision: ACCEPT as the primary source for Pons ABIs and event
  semantics.

**slightlyuseless/pons-launch-engine**
- URL: github.com/slightlyuseless/pons-launch-engine.
- Commit: 29205ab (2026-09-01), 2 commits, 1 author.
- License: MIT. Language: TypeScript (Node 22.5). Chain: Robinhood Chain.
- Purpose: a creator-side campaign engine:
  - launch your own Pons token and buy it from up to 32 controlled
    wallets on the V2 exemption list;
  - target the V1 restricted blocks N+1 / N+2;
  - automated exits.
- Production status: new, single drop.
- Dependencies: viem, SQLite, Alchemy RPC, Blockscout, Pinata.
- Execution: yes (multi-wallet). Monitoring: one deployer.
- Security: keys in a local gitignored file, not printed.
- Limitations: built for launch-window self-buying, which is exactly what
  the Pons coordination check (M5) is meant to catch. The server's 31
  declared + 2 automatic exemptions match its "up to 32 exemption
  addresses".
- Decision: REJECT for integration; evidence for §11 safety.

**yesiambroke/pons-terminal**
- URL: github.com/yesiambroke/pons-terminal.
- Commit: e678f69 (2026-09-21), 6 commits.
- License: MIT. Language: TypeScript. Chain: Robinhood Chain.
- Purpose: multi-wallet Pons trading terminal: ladders, "Volume"
  buy-then-sell cycles, multi-wallet sell-all.
- Production status: beta tool.
- Dependencies: viem, CoinGecko price API, a third-party RPC
  (arrowrpc).
- Execution: yes, every trade through its own TradeRouter contracts
  (V1 `0x0102…E31B`, V2 `0xb8f7…3B06`), which skim 0.5% per leg ("so
  protocol fees fund the project"). Monitoring: balances and routes only.
- Security: AES-encrypted local wallet vault.
- Limitations: router-mediated trades hide the wallet in Pons events
  (see above); `roundTrip` manufactures volume.
- Decision: REJECT for integration. Its router addresses are labels in
  `known_routers.py`.

**casatrickdev/robinhood-trading-tools (pons-sdk)**
- URL: github.com/casatrickdev/robinhood-trading-tools.
- Commit: 0715eb6 (2026-09-15), 14 commits.
- License: MIT. Language: TypeScript. Chain: Robinhood Chain.
- Purpose: read-only Pons SDK, historical launch indexer, copy-trading
  research ("no live execution"; sniper and bundler on the roadmap).
- Production status: early.
- Dependencies: viem.
- Execution: none. Monitoring: launches and swaps via eth_getLogs in
  2,000-block chunks.
- Security: no keys.
- Limitations: names the Pons legacy factory and lockers we do not
  track; public RPC times out on wide log ranges (as we found).
- Decision: PARTIALLY USE (addresses cross-checked; nothing imported).

**nirholas/robinhood-chain-sdk ("hoodchain")**
- URL: github.com/nirholas/robinhood-chain-sdk.
- Commit: c1a76ee (2026-09-15); meaningful fd35d65 (2026-07-29); since
  then only empty "refresh activity" commits.
- License: "All rights reserved" (proprietary).
- Language: TypeScript (viem). Chain: Robinhood Chain (mainnet and
  testnet addresses).
- Purpose: Stock Tokens, Chainlink quotes, Uniswap V3, NOXA / Odyssey
  watchers, sequencer firehose.
- Production status: library, npm.
- Execution: swaps. Monitoring: launch watchers, firehose.
- Security: n/a.
- Limitations:
  - no Pons (the most active venue);
  - the licence forbids reuse.
- Decision: REJECT (licence). NOXA / Odyssey addresses match our
  registry; its differing WETH / router addresses are testnet.

**nirholas/robinhood-trading-bot**
- URL: github.com/nirholas/robinhood-trading-bot.
- Commit: 7e46f1a (2026-09-15); meaningful 71bb405 (2026-07-22, "Pivot
  execution venue to Robinhood Chain").
- License: Apache-2.0, but it depends on the proprietary hoodchain.
- Language: JavaScript (Node 22.5). Chain: Robinhood Chain.
- Purpose: rule-based entries on NOXA / Odyssey launches plus copy
  trading; paper by default; live triple-gated.
- Dependencies: hoodchain, viem, Blockscout API for launch scanning.
- Execution: Uniswap V3 SwapRouter02 (curve tokens are signal-only when
  live). Monitoring: launches, Transfer logs of tracked wallets.
- Security:
  - the key comes from .env;
  - the dashboard token is printed in a URL query string.
- Limitations:
  - no Pons;
  - depends on an explorer API (Blockscout) for discovery;
  - no launch-window coordination checks.
- Decision: REJECT. YonixAlpha already does this with on-chain events and
  stronger safety.

**nirholas/robinhood-chain-alerts ("hood-alerts")**
- URL: github.com/nirholas/robinhood-chain-alerts.
- Commit: 1c15c21 (2026-09-15); meaningful a0ce7f9 (2026-07-20).
- License: "All rights reserved". Language: TypeScript.
- Chain: Robinhood Chain (NOXA, Odyssey).
- Purpose: Telegram / Discord alert service with a rule engine and free
  / premium tiers.
- Execution: none. Monitoring: launches, curve trades, graduations, whale
  swaps.
- Useful idea, already followed: USD values only from on-chain
  liquidity, unknown stays null and never 0.
- Limitations: no Pons; proprietary.
- Decision: REJECT (licence; reference for the null-not-zero rule).

**four-meme-community/four-meme-ai**
- URL: github.com/four-meme-community/four-meme-ai.
- Commit: c81f0ee (2026-03-30), 9 commits.
- License: MIT. Language: TypeScript. Chain: BSC.
- Purpose: agent "skill" and CLI to create / trade Four.meme tokens,
  read TokenManager2 events, TaxToken info, EIP-8004 identity.
- Production status: maintained by the Four.meme community org; six
  months without changes.
- Execution: yes (PRIVATE_KEY in env). Monitoring: getLogs over
  TokenManager2.
- Security: plain-env key.
- Limitations: V1 not supported; struct layouts for the mode flags not
  given.
- Decision: PARTIALLY USE. Our contract addresses (V1 `0xEC45…bFbC`,
  V2 `0x5c95…762b`, Helper3 `0xF251…6034`) and the four event signatures
  match; the X Mode / AntiSniperFeeMode gap comes from here.

**MeteoraAg/dynamic-bonding-curve (+ -sdk, docs)**
- URLs: github.com/MeteoraAg/dynamic-bonding-curve,
  …/dynamic-bonding-curve-sdk, …/docs.
- Commits:
  - program f552f20 (2026-09-09, release 0.2.1);
  - SDK a07966d (2026-09-24);
  - docs 010de62 (2026-09-28).
- Licenses: the program is under the "Meteora Non-commercial Licence";
  the SDK is MIT.
- Languages: Rust (Anchor) / TypeScript. Chain: Solana.
- Purpose: a launch-pool protocol for partners. Each partner sets the
  curve, fees and quote mint; tokens graduate to DAMM v2 (DAMM v1
  deprecated for new configs).
- Program id: `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`.
- Events: EvtInitializePool, EvtSwap / EvtSwap2, EvtCurveComplete, the
  migration events, and others.
- Execution / monitoring: via the SDK.
- Security / limitations:
  - SPL and Token-2022 base tokens, including transfer hooks (sell risk);
  - the rate-limiter fee mode is deprecated;
  - the program licence forbids commercial reuse of its code.
- Decision: research only for M10. A DBC adapter would decode events
  from the published IDL with our own code, and needs launch / trade /
  migration verification before any paper trading.

**1chimaruGin/bsc-mempool**: see section 17.

### Tests

- `test_evm_streams.py` (12):
  - real-frame decoding and the signature check (including wrong chain id
    and tampering);
  - a forged frame dropped before sequencing;
  - backlog, reorg and quiet resume;
  - the attribution report on the real schema.
- `test_provider_roles.py`: the signature-failure finding.

NOT VERIFIED here:
- the live feed's behaviour after these changes;
- whether the delayed feed also signs its messages. If it does not, its
  messages are dropped while it is in use, and the stream panel and plan
  health say so; the signature check can be switched off in the stream
  settings;
- the attribution numbers. `tools.trader_attribution` must run on the
  server.

## 19. M10a — activity of the other Solana launchpads (2026-10-01)

Master §5-7. Before any adapter, the question §7 asks first is which venues
are actually being used. That question is now answered from the chain for
three programs. They are in the registry as OBSERVE ONLY: nothing is traded,
quoted or copied there, the trading status is DISABLED ("observe only"), and
the Launchpads page shows OBSERVE ONLY instead of a mode selector.

| Venue | Program | Source of id and instruction names |
|---|---|---|
| Raydium LaunchLab (LetsBONK / bonk.fun runs on it) | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | raydium-io/raydium-idl `raydium_launchpad.json` 0.2.0; raydium-sdk-V2 `LAUNCHPAD_PROGRAM` (the `DRay6…` id there is devnet) |
| Meteora Dynamic Bonding Curve (Bags, Jupiter Studio and others run on it) | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | MeteoraAg dynamic-bonding-curve-sdk IDL 0.2.1 |
| Moonshot | `MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG` | wen-moon-ser/moonshot-sdk IDL V4 (SDK last changed 2025-04) |

**The probe** (`solana/venue_probe.py`, run by data-solana every 5 minutes;
`SOLANA_VENUE_PROBE=0` switches it off):
- `getSignaturesForAddress(program, limit=1000)` gives the last successful
  transaction and an exact transaction rate over the span those cover.
- 25 of the newest successful transactions are fetched. Their
  `Instruction:` log lines, attributed through the invoke stack (an
  aggregator's CPI into the venue counts for the venue, the aggregator's own
  instructions do not), are classified as launch / trade / migration.
- Instruction names it does not know are counted and shown ("other
  instructions"), so a wrong naming assumption is visible instead of silent.
- All calls are background priority: about 26 per venue per probe, shed
  first when providers are limited.

**Why a probe and not a log stream.** A `logsSubscribe` stream on DBC or
LaunchLab carries every swap. That is a large, continuous load on the
provider for a question that a few calls every 5 minutes answer.

**What it shows** (Launchpad Health, Launchpads page):
- ACTIVE / QUIET / INACTIVE from the newest transaction itself (not from the
  probe time). INACTIVE still needs 7 days of probing (otherwise UNVERIFIED).
- Last transaction, transactions per minute, the latest sample's kinds and
  other instructions.
- Last launch / trade / migration "seen in samples".
- 7-day counts and volume are NOT measured and show "not tracked", never 0:
  a sample can miss launches.
- A probe that fails is recorded as ACTIVE FAIL with the error.

**Also fixed.** Launchpad verification for Solana used the production
evidence for any key other than `pumpfun`, which would have given these
venues PumpSwap's evidence. Now only `pumpfun` and `pumpswap` use it; every
other venue uses recorded checks.

**Not done (M10 continues):**
- Per-site split: LetsBONK vs other LaunchLab platforms; Bags vs Jupiter
  Studio vs other DBC configs. This needs the platform-config or DBC-config
  account of each launch.
- Event decoding, quotes, safety and paper trading for any of these venues.
- StonkFun not identified.

**Tests** (`test_venue_probe.py`):
- invoke-stack attribution and unknown names;
- rate, last transaction and sample classification against a fake RPC;
- failed transactions skipped;
- Launchpad Health from two probes;
- "not tracked" counts;
- the trading status stays DISABLED with probe checks.

The first run also caught `launchpad_checks.source` being 48 characters at
most; the probe label is shorter now.

NOT VERIFIED here: the instruction names as they appear in real logs
(Anchor UpperCamelCase assumed; the "other instructions" list on the
server shows any mismatch), and each venue's real activity.

## 20. M10b — who a trade belongs to (2026-10-02)

Server evidence (`tools.trader_attribution --days 3`, 2026-10-02):

| Venue | Trades (3 days) | Distinct traders | Busiest traders |
|---|---|---|---|
| BSC Flap | 871,944 | 9,837 | 9 of the 12 busiest are contracts; the busiest alone has 40.8% of all trades |
| BSC Four.meme | 72,340 | 8,745 | 11 of 12 are wallets; the busiest contract has 1.2% |
| Robinhood Pons V2 | 614,178 | 34,785 | all 12 busiest are contracts; 110,661 of 333,484 curve buys (33.2%) name a recipient other than the caller |

Launchpad events name whoever called the launchpad. When a wallet trades
through a router or bot contract, the event names that contract. Wallet
profiles, smart-wallet discovery, copy detection and buyer counts all read
the stored trader, so they saw one very busy "wallet" instead of the people
behind it.

**Pons: credited to the recipient.**
- The official PonsV2BondingCurve emits `CurveBuy(msg.sender, recipient, …)`
  and `CurveSell(msg.sender, recipient, …)`.
- The recipient gets the tokens (buy) or the quote (sell). So when it
  differs from the caller, it is now the stored trader, and the caller is
  kept in `extra.caller`.
- A router sell that pays the router stays credited to the router: the
  wallet behind it is not in the event.
- Launch coordination already read the recipient as the holder, so it is
  unchanged.
- Trades stored before this change keep their old trader.

**Address kinds** (`chains/evm/address_kinds.py`, table `evm_address_kinds`,
migration 0030), from `eth_getCode`:
- WALLET: no code.
- DELEGATED_WALLET: EIP-7702 designator `0xef0100 || delegate`. Still a
  wallet that signs its own transactions. The M9 tool counted these as
  contracts and now shows them as wallets.
- CONTRACT: any other code.

Contracts are never re-checked; wallets are re-checked after 7 days (they
can gain a delegation). A failed lookup leaves the address unknown, never
"wallet". Lookups are bounded: 300 new addresses per chain per 10-minute
profile rebuild.

**What uses it:**
- **Wallet profiles:** a CONTRACT keeps its metrics but gets no score, the
  CONTRACT label and the discovery stage REJECTED with the reason. A router
  or bot can never become a smart-wallet candidate or get a paper follow.
- **Smart Wallets:** contract profiles are hidden unless "show contract
  addresses" is ticked; CONTRACT and DELEGATED_WALLET are label filters.
- **Copy targets:** adding a contract on BSC / Robinhood is refused (422,
  with the reason). If the check cannot run (RPC down), the target is
  added with a warning.

**Not solved, stated:**
- Flap's events carry only `trader`, the caller. For the roughly 75% of
  Flap trades made through contracts, the wallet behind them would need
  each transaction's sender: about 290k extra RPC calls a day at the
  current volume, so not fetched.
- Those trades still count toward per-token buyer counts and concentration
  as one buyer per contract.
- A copy target that trades only through a router is not seen as trading
  (its trades are credited to the router), except Pons buys, which now name
  the target as recipient.

## 21. M15 — update monitor (2026-10-02)

Master §64-66: watch the repositories and dependencies YonixAlpha relies
on, tell the operator when something changes, never apply it.

**What is watched** (`yonixalpha_core/update_monitor.py`, `WATCHES`):

| Area | Sources | Used directly (a change on our paths is ACTION REQUIRED) |
|---|---|---|
| Solana | pump-fun/pump-public-docs, anza-xyz/agave, raydium-io/raydium-idl, MeteoraAg/dynamic-bonding-curve-sdk | Pump IDLs, LaunchLab IDL, DBC IDL |
| BSC | four-meme-community/four-meme-ai, 1chimaruGin/bsc-mempool | Four.meme addresses / events / errors |
| Robinhood | ponsdotdev/ponsfamily, chainstacklabs/robinhood-chain-sequencer-feed, OffchainLabs/nitro | Pons V1 / V2 contracts and ABI, feed signer / codec |
| Providers | helius-labs/helius-sdk, jito-labs/jito-ts, 0xProject/0x-settler, madeonsol/madeonsol-sdk, nansen-ai/nansen-cli | — (API wording changes are PROVIDER CHANGE) |
| Dependencies (PyPI) | solders, eth-account, eth-abi, websockets, httpx, sqlalchemy, cryptography, coincurve | all (pinned in `packages/core-py/pyproject.toml`) |

Every repository was confirmed to exist (git ls-remote) when the list was
written.

**How** (official APIs only, no page scraping):
- GitHub REST: newest commit, latest release, and `compare/{old}...{new}`
  for the commits and files changed since the last check.
- PyPI JSON: latest version, and the known vulnerabilities of the
  installed version (the version the ml container runs).
- Every 6 hours, from the ml service. `UPDATE_MONITOR=0` switches it off.
- Unauthenticated GitHub allows 60 requests per hour; one pass uses about
  30-45. An optional `GITHUB_TOKEN` (no permissions needed) raises the
  limit to 5,000. On a rate limit the pass stops, the error is shown on
  the watch, and the next pass continues.

**Classification** (rule based, from commit and release text and the
changed paths; the panel says "read the change before acting"):

| Class | When | Notification |
|---|---|---|
| SECURITY UPDATE | security / vulnerability / CVE / GHSA / exploit / advisory wording, or a known vulnerability in the installed version | critical |
| BREAKING CHANGE | breaking / deprecation wording, a major version, a removed file on a path we use | warning |
| PROVIDER CHANGE | a provider repository's change mentions an API / RPC / endpoint / schema | warning |
| UPGRADE AVAILABLE | a new release, or a newer dependency version | info (in-app only) |
| INFO | anything else | none (shown on the panel) |
| ACTION REQUIRED | any of the first three, or any change on a path we read, for a source used directly | critical |

- The first check of each source is a baseline: stored, never notified.
  The exception is a dependency whose installed version already has a
  known vulnerability; that is reported at once.
- Commits that change no files (some repositories refresh their activity
  date with empty commits) raise nothing.
- A new dependency version or a new vulnerability is reported once, not on
  every pass.
- Each change is a row in `update_events` (history, never overwritten):
  from / to reference, reasons, the first commit messages, files on our
  paths, whether it was notified, and who acknowledged it.

**Where it shows:**
- Notification kind `infrastructure_update`, on Telegram by default
  (switchable under Notifications). ACTION REQUIRED, SECURITY, BREAKING
  and PROVIDER changes go to Telegram and the in-app bell; UPGRADE
  AVAILABLE (routine, e.g. every validator release) to the bell only.
  Every message ends "Not applied automatically: review and deploy by
  hand."
- System Health → Research / Updates: open changes by class, the change
  list with Acknowledge, and every watched source with its last check,
  latest commit / release or installed / latest version, and last
  classification. A source never checked reads NOT CHECKED, never "up to
  date".
- API: `GET /api/system/updates`, `POST /api/system/updates/{id}/acknowledge`
  (audited).

**Never automatic** (§66): nothing is upgraded, pulled or deployed. The
operator reads the change, updates the pin or adapter in a PR, and deploys
with `scripts/deploy.sh` as usual.

**Verified:**
- Classification, baseline, empty commits, one event per change, rate
  limit and vulnerability handling, notification text and the API are
  covered by `tests/test_update_monitor.py` and `apps/api/tests/test_system.py`
  against mocked GitHub / PyPI responses.
- PyPI: checked against the real pypi.org from the build environment.
  All 8 dependencies answered.
- GitHub: NOT VERIFIED from the build environment (its proxy refuses
  api.github.com). The server's first pass is the first real check. Its
  result shows on the panel within minutes of the ml service starting.

**Deploy build cache** (seen on the server 2026-10-02: about 30 GB of
build cache filled the disk). `scripts/deploy.sh` now runs
`docker builder prune -f --filter until=48h` after a successful deploy.
Layers used in the last 48 hours stay, so the next build is still fast.
`BUILD_CACHE_KEEP=168h scripts/deploy.sh` keeps a week. A failed prune
never fails the deploy.

## 22. M10c — Four.meme modes, Genius.fun, launch sites (2026-10-02)

### Four.meme X Mode

four-meme-ai names the on-chain flags but not the layout of the getters
that hold them (section 18).

- **X Mode is detected without the layout.** An X Mode token can only be
  bought with the signed `buyToken(bytes,uint256,bytes)`; a plain
  `buyTokenAMAP` reverts with "A" (four-meme-ai errors.md). The safety
  check now simulates that plain buy:
  - eth_call to TokenManager2;
  - msg.value from Helper3 `tryBuy`;
  - funds rounded down to GWEI;
  - a state override funds the simulation account.
- **Results:**

| Result | Finding | Effect |
|---|---|---|
| reverted "A" | FOURMEME_X_MODE | FAIL: YonixAlpha has no X Mode buy, so the token cannot be entered |
| reverted, other reason | PLAIN_BUY_REVERTS (reason shown) | WARN, until server evidence shows the simulation matches real buys |
| goes through | PLAIN_BUY_SIMULATED | INFO |
| node refuses state overrides | PLAIN_BUY_NOT_SIMULATED | INFO, stated: X Mode not ruled out |
| no RPC | PLAIN_BUY_UNAVAILABLE | UNKNOWN (never safe) |

- The buy and sell quotes alone pass on an X Mode token. Before this
  change, X Mode tokens could become paper entries that a real buy could
  never make.
- It applies to V2 tokens on the curve with a BNB quote.

### AntiSniperFeeMode and the template bits

These still need the struct layout. `tools.fourmeme_modes` (read-only)
collects the evidence that fixes it. For the newest 40 curve tokens it
records:
- the plain-buy result;
- TaxToken from `feeRate()`;
- the raw words of `_tokenInfos` and `_tokenInfoEx1s`;
- with `--api`, four.meme's own token API (version V8 = X Mode,
  feePlan = AntiSniperFeeMode);
- with `ETHERSCAN_API_KEY`, the verified TokenManager2 ABI.

It reports, per word, how often the template bits agree with the TaxToken
and X Mode evidence. Nothing is decoded until that output has been read.

```
docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml \
  exec -T data-evm python -m yonixalpha_core.tools.fourmeme_modes --api
```

### Genius.fun (BSC)

**Research:**
- Launched 2026-09-16/17. Most launches pair with tokenized stocks
  (bStocks, xStocks, 4Stocks, Ondo).
- Production factories `0x78EAE9537C0ef90DFe9B7ae964682Fe8138afe31` (still
  open) and `0x37eE8AeE29C5efd3C1A7edA6dF3F510779928a37`.
- Graduation goes to PancakeSwap Infinity pools with a Genius hook.
- Source: DefiLlama dimension-adapters PR #9598 (helpers/genius-fun.ts,
  59c6c55). Its own manifest, genius.fun/contracts/manifest.json, is not
  reachable from the build environment: NOT VERIFIED against it.

**Same events as Pons V2:**
- TokenLaunched, CurveBuy and CurveSell are field for field the Pons V2
  events (same names, types, indexing), so the topics are identical.
- Genius.fun is therefore decoded by the Pons V2 decoder with its curve
  bookkeeping and router attribution (recipient as trader).

**OBSERVE ONLY:**
- No quotes, safety reads, entries or copies.
- Graduation (Infinity) is not decoded.
- The data-evm safety pass now skips every observe-only venue.

**Volume** counts native-quote trades only. A trade against a stock or any
other ERC-20 pair is counted as a trade, not added to BNB volume. The same
rule now applies to Pons V2 curves with an ERC-20 pair. Four.meme tokens
quoted in a BEP-20 (e.g. USD1) are still summed into volume: the scan does
not know the quote per token. That is a known gap. (Closed in M10d,
section 23.)

**Curve launches:** only curves launched inside the backfill window are
known. Trades on older Genius curves are rejected as foreign until they
reach the window.

### StonkFun

- StonkFun (Solana, launched 2026-08-03) pairs launches with tokenized
  stocks and moved to Raydium LaunchLab (The Block / CoinGecko,
  September 2026).
- It is therefore one of LaunchLab's platform configs, not a separate
  program. The site split below shows it once a sample contains its
  trades.

### Launch sites on LaunchLab and Meteora DBC

**From each sampled transaction:**
- Every LaunchLab / DBC instruction, outer or CPI, whose Anchor
  discriminator (`sha256("global:<name>")[:8]`, checked against the IDLs)
  is a trade or launch gives its site account:
  - LaunchLab: platform_config, account 3;
  - DBC: config, account 1 in swaps and 0 in initialize_*.

**How sites are labelled:**
- LaunchLab platform configs store their own name and web address
  (PlatformConfig.name at byte 112, web at 176). They are read once with
  getMultipleAccounts and cached, so a site is named by the chain, not by
  a list we keep.
- DBC configs have no name and are shown with their quote mint.

**On the page:** the Launchpads page lists the top 10 sites with their
share of the sampled instructions. It is a sample of 25 transactions, so
small sites can be missing.

## 23. M10d — Four.meme curves quoted in tokenized stocks (2026-10-02)

### Server evidence

`tools.fourmeme_modes`, 40 newest Four.meme curve tokens:
- 32 of the 40 are quoted in a BEP-20, not BNB. In the sample that is
  BNCB, a bStocks tokenized share of CEA Industries worth about $6
  (`0x4902c5EB…eEc3f`).
- Only the 8 BNB-quoted tokens could be simulated (all plain-buy OK).

DefiLlama dimension-adapters issue #9736 found the same thing:
- Since 2026-09-14, Four.meme curves have been quoted in tokenized stocks
  (BNCB, SPCXB, NVDAB, FORM ...).
- Pricing their trades as BNB inflated Four.meme volume 10–30x.

`TokenPurchase.cost` is in the quote token's units. We stored it as BNB,
so the following summed stock units as BNB:
- the launchpad's 7-day volume;
- wallet profit and loss, profiles and smart-wallet scores;
- market regimes.

Paper entries were never affected: safety already fails a non-BNB quote
(NON_NATIVE_QUOTE).

### Fix

**Quote per token.**
- The Four.meme adapter reads each token's quote once from Helper3
  `getTokenInfo` (documented ABI; cached, 50,000 tokens) when it sees the
  launch or the token's first trade.
- The launch stores it as the token's quote. Every launch and trade
  records `native_quote`.
- A revert (not a Four.meme token) leaves it unknown. An RPC outage stops
  the scan, which resumes later.

**Older tokens.**
- The data-evm safety cadence reads the quote of up to 200 stored
  Four.meme tokens per pass that have none, traded in the last 14 days,
  most recently traded first.
- An unreadable token is marked and not asked again.

**Native-unit sums** (`store.native_quote_trade`) leave out a trade that
is flagged non-native, or whose token has a recorded ERC-20 quote:
- wallet profiles / profit and loss;
- market regimes;
- launchpad volume.

BNB / ETH and their wrapped forms (WBNB, Robinhood WETH) count as native.
Trades on stock-quoted curves still count as trades, not as volume.

**Stale profiles.**
- A wallet whose trades were mostly on stock-quoted curves can drop below
  the rebuild threshold. Its old profile (with figures in stock units) is
  kept but marked stale with the reason.
- The Smart Wallets page shows a STALE label.
- The next rebuild of that wallet clears it.

### Four.meme struct layout (from the same run)

`_tokenInfos` returns 13 words:
- word 0 = the token;
- word 1 = the quote (non-zero exactly for the 32 BEP-20-quoted tokens);
- word 3 = 1e27 (total supply);
- words 4 and 7 = 8e26 (max offers, and offers while nothing is sold).

That fits base, quote, template, totalSupply, maxOffers, maxRaising, ...
with word 2 the template:
- its low bits read creator type 9 (`0x…241b`);
- bit 16 is clear on every plain-buy-OK token.

**Not settled.** The sample held no TaxToken, no X Mode token and no agent
token, so the template bits are still untested. X Mode detection
therefore stays on the buy simulation (section 22). The tool now also
samples:
- tokens safety has found X Mode;
- tokens with a stored tax;
and tests word 2 directly.

`_tokenInfoEx1s` returns 5 words:
- word 3 is non-zero on every token (about 125.3M, close to the current
  BSC block);
- the others were zero.

So AntiSniperFeeMode (`feeSetting`) is not located yet.

The four.meme API cross-check returned nothing on the server. The tool
now prints why (HTTP status or the API's error).


## 24. M13 + M14 — manual EVM trading, balances and gas, explorer, PnL (2026-10-02)

### M10d server result (deploy c64ab13)

`tools.fourmeme_modes` v2 sampled 60 tokens:
- quote assets: BNCB `0x4902…eec3f` (39), BNB (19), `0x2058…efc7` (2);
- no TaxToken, X Mode or agent token was in the sample, and the database
  holds 0 FOURMEME_X_MODE findings, so the template bits stay untested;
- the four.meme API answered HTTP 403 from the server for all 60.

Nothing changes: X Mode detection stays on the buy simulation, and the
stock-quoted curves stay out of BNB sums (section 23).

### Manual BUY / SELL on BSC and Robinhood Chain (§45)

Not a second trading path. `POST /api/trade/evm/buy` (confirmation
required, audited) records the request in Redis and queues it for that
chain's data-evm worker. The worker:
1. expires a request no worker took within 10 minutes (never executed late);
2. re-runs the safety check when the stored one is missing or older than
   5 minutes;
3. runs the automatic entry code, `paper.evaluate_entry(operator=True)`.

Only the strategy's trade signal (buy counts, distinct buyers, buy share)
is replaced by the operator's decision. Everything else still applies:
- kill switch and the trading switches (a category switch only for
  FRESH / MIGRATED / MOMENTUM);
- the launchpad's verified status;
- safety PASS;
- liquidity;
- launch-window coordination;
- account limits and the re-entry cooldown;
- gas (below) and the risk plan.

A blocked request is BLOCKED with every reason. An observe-only venue
(Genius.fun, the Solana observe-only launchpads) is refused before
queueing. A token with an open position gets POSITION_ALREADY_OPEN.
There is no override control (task #138 stays blocked).

Status: QUEUED → EVALUATING → BLOCKED | PAPER_POSITION_OPEN | FAILED |
EXPIRED, kept 24 h, shown live in the Confirm Purchase dialog.

Manual SELL is the existing operator exit (`POST /api/trade/sell/<id>`,
`exit_requested`). The data-evm position manager executes it at the
executable sell quote of the position's current venue. EVM positions now
have a SELL button (EVM Markets, token page).

**Paper only.** EVM LIVE execution is locked (task #178).

### Balances, gas reserve, INSUFFICIENT GAS, unified wallet (§56-58)

**YonixAlpha Trading Wallet** (`GET /api/wallets/overview`, top of the
Wallets page). It has one row per chain and mode: Solana, BSC and
Robinhood, each LIVE and PAPER, never mixed. Each row shows:
- Total;
- Reserved (LIVE: pending buy orders; PAPER: open positions);
- Available = total - reserved;
- Gas reserve;
- Trading balance = available - gas reserve;
- USD values from the SOL / BNB / ETH rate.

The rules behind those rows:
- A LIVE balance older than 3 minutes is STALE. One that was never read is
  UNAVAILABLE, with no numbers (never 0).
- The Solana and EVM accounts have separate keys; neither is derived from
  the other. Only public addresses are returned.
- data-evm reads the EVM wallet balance every 60 s (watch-only;
  `yx:evm:wallet:<chain>`).
- data-evm also refreshes BNB/USD and ETH/USD every 60 s from the
  PancakeSwap V2 router quote against USDT (`chains.evm.native_price`).
  Rates are used for 5 minutes, and only inside a sanity range.

**INSUFFICIENT GAS (§57).** It is checked before the entry, never
discovered after signing.
- **EVM paper entries:**
  - `build_plan` reads `eth_gasPrice` and estimates gas for the buy and
    the sell (price x `gas_units_per_swap`, default 300 000, x 2);
  - it needs that plus the chain's `gas_reserve` (BSC 0.002 BNB,
    Robinhood 0.0005 ETH; API settings);
  - otherwise the result is NO_TRADE INSUFFICIENT_GAS; no gas price gives
    NO_TRADE GAS_PRICE_UNAVAILABLE;
  - the size is planned from what remains;
  - paper does not charge the gas; the estimate is shown in the decision
    detail.
- **Solana LIVE entries:**
  - the gate adds INSUFFICIENT_GAS (CRITICAL, NO_TRADE) when the wallet
    holds less than the fee reserve (`min_sol_reserve`) plus the round
    trip's fixed costs.
- **Solana paper:** unchanged. No fee reserve is held back, and the
  Wallets page says so.

### Token Explorer (§54-55)

**Search.** The Token Explorer page searches Solana, BSC and Robinhood
Chain together, with an optional chain filter
(`GET /api/explorer/search`):
- name or symbol from the start: LIKE wildcards match literally, and
  migration 0032 adds the expression indexes, built CONCURRENTLY;
- mint / contract address, in any case;
- creator;
- wallet (profile, copy target, trades in the last 14 days).

A Solana address only searches Solana; a 0x address only searches BSC
and Robinhood Chain.

**EVM token page** (`/dashboard/explorer/<chain>/<address>`,
`GET /api/explorer/token/...`):
- price (native and USD) and USD market cap;
- liquidity and volume;
- buyers and sellers (stats window and 14-day retained trades);
- safety findings, launchpad, migration, status;
- last automatic and manual decision;
- smart money (copy targets and validated wallets that traded it,
  labelled "not a reason to buy");
- manipulation (launch-window coordination);
- ML;
- positions with full PnL, Manual BUY and SELL.

Holder counts are not tracked on EVM. The page says so instead of showing
0. ML is NOT_AVAILABLE until the EVM features of M12 exist.

**Explorer actions** (`yonixalpha_core.explorer_links`): OPEN TOKEN,
TRANSACTION, CREATOR, WALLET, LAUNCHPAD, DEX and EXPLORER.
- Each one is built for the token's own chain, and an address invalid for
  that chain gets no link. A Solana link is never built for an EVM token.
- Only confirmed URL formats are used:
  - Solscan, BscScan, Robinhood Chain Blockscout;
  - pump.fun/coin, four.meme/token, flap.sh/bnb,
    ponsfamily.com/launchpad;
  - DexScreener for Solana and BSC.
- Robinhood Chain has no confirmed DEX page. That action is shown disabled
  with the reason, never a guessed URL.

### Real-time PnL, colours, USD market cap (§59-61)

`yonixalpha_core.position_pnl.view` is the one PnL view used by:
- paper positions;
- live positions;
- copy positions;
- EVM positions;
- the dashboard overview;
- the Solana and EVM token pages;
- trade details.

**Fields:** outcome PROFIT / LOSS / BREAKEVEN with net %, entry, current,
quantity, value, unrealized, realized, fees, net, peak and drawdown from
peak.

**How each value is computed:**
- Realized includes partial take-profits on an open position (proceeds -
  the sold share of the entry cost).
- Net = realized + unrealized, after the entry fees (the entry cost
  includes them).
- The mark basis is stated per engine:
  - EVM: executable sell quote, fees and taxes included;
  - Solana: curve / pool price, exit costs not deducted.
- A mark older than 2 minutes (live: 60 s) is flagged stale. With no mark
  the outcome is PNL_UNAVAILABLE with the reason: never a bare "open",
  never 0.

**Display:** green / red / neutral with TrendingUp / TrendingDown / Minus
icons; no emojis.

**Market cap.** EVM market cap = launchpad price x total supply (read
once from the token contract) x BNB/USD or ETH/USD. It is shown as $950 /
$9.5K / $1.2M / $1.05B on the EVM token list and token page; native
amounts come second. A curve quoted in a tokenized stock gets no BNB or
USD figure (section 23).

### Tests

- core: `test_balances`, `test_explorer_links`, `test_evm_token_view`,
  `test_position_pnl`, `test_chains::test_gate_refuses_an_entry_the_wallet_cannot_pay_gas_for`.
- data-evm: gas for both swaps and the reserve; wallet / USD-rate sync;
  manual BUY runs every entry check but replaces the signal; manual BUY
  refused on an observe-only venue.
- api: `test_explorer_wallets_api` (search, links per chain, LIKE
  escaping, the token view, PnL on the lists, the unified wallet, the
  manual BUY queue, audit, observe-only refusal).

**NOT VERIFIED until the server runs it:**
- the EVM wallet balance read;
- the BNB/ETH USD rate from the real router;
- a manual BUY taken by the real worker.

## 25. M10 close, M15b, M11 — Odyssey / NOXA, staying up to date, external wallet intelligence (2026-10-03)

### Robinhood venues (M10 close)

The operator reported NOXA and The Odyssey are no longer active. Launchpad
Health agrees: no launch or trade was seen from either since monitoring
began.
- NOXA was already inactive.
- The three Odyssey adapters (curve, instant, reflection) are now
  `active=False` with that reason.

Effect:
- discovery skips them, so no RPC is spent on them;
- the Launchpads page shows them DISABLED with the reason;
- nothing is traded there;
- decoders, adapters and tests stay, so a reactivation is a one-line
  change.

### Staying up to date (M15b)

The question was how to take the upgrades the monitor reports. Deploying
again does not do it: every Python dependency is pinned in the
repository, and a deploy installs those exact pins.

Each update now says what to do, in the panel and in the Telegram text:

| What to do | Meaning |
|---|---|
| APPLIED | the server already runs that version |
| PIN BUMP | a newer release of a pinned dependency; it reaches the server through a dependency-update pull request that runs every test, then a deploy |
| INTEGRATION CHECK | a repository whose IDL / ABI / API is read changed a file used here |
| REVIEW ONLY | a repository that is not installed; nothing to deploy |

`DEPLOY_PULL=1 scripts/deploy.sh` pulls the newest base images (same
major versions) for operating-system patches. The routine is in
DEPLOYMENT.md section 3.1.

**First dependency round.** PyPI was checked on 2026-10-03; no pinned
version had a known vulnerability.

| Package | Change | Result |
|---|---|---|
| websockets | 14.1 → 17.1 | applied; all uses are the modern client API (`connect`, `additional_headers`, `recv`) |
| eth-abi | 5.2.0 → 6.0.0 | applied |
| eth-account | 0.13.7 → 0.14.0 | applied |
| cryptography | 50.0.1 → 50.0.2 | applied |
| pyjwt | 2.15.0 → 2.15.1 | applied |
| fastapi | 0.141.1 → 0.142.2 | applied |
| starlette | 1.6.0 → 1.7.0 | applied |
| alembic | 1.14.0 → 1.20.0 | applied; `alembic check` clean |
| SQLAlchemy | 2.0.36 → 2.0.54 (newest 2.0.x) | applied |
| SQLAlchemy | 2.1.3 | **held back**: under it, two tests that read rows after a raw SQL statement failed intermittently in the full suite (passing alone), a behaviour change not cleared in tests |

The RPC-registry test that showed the staleness now expires the session
explicitly.

The API's live-update WebSocket was also checked end to end on websockets
17.1: a real uvicorn server, then login, connect, auth and `ws.ready`. The
API tests cannot show this, because they run the app without uvicorn.
uvicorn 0.32.1 still serves WebSockets through websockets' deprecated
legacy module. It works with 17.1, but the next websockets major version
may remove that module, so uvicorn is the next pin to bump.

### External wallet intelligence (M11)

Sources were read from the providers' own code (2026-10-03), not from
HTML or guesswork.

**Nansen** (nansen-ai/nansen-cli `src/api.js`):
- base `https://api.nansen.ai`, header `apikey`;
- `POST /api/v1/profiler/address/labels`;
- `/profiler/address/pnl-summary` with a `date` range;
- `/smart-money/dex-trades` for candidates;
- free `GET /api/v1/account` for the connection test;
- chains `solana` and `bnb` (BSC). Robinhood Chain is not covered.

**MadeOnSol** (madeonsol/madeonsol-sdk `src/index.ts`):
- base `https://madeonsol.com/api/v1`, `Authorization: Bearer`;
- `GET /wallet/{a}/pnl` (FIFO P/L summary in SOL);
- `/kol/{wallet}` (404 when not a tracked KOL);
- `/kol/leaderboard` for candidates;
- free `GET /me` (tier and quota) for the test;
- Solana only.

**Rules:**
- **Enrichment only.** Labels, name and provider P/L are stored in
  `wallet_enrichment` (migration 0033) and shown next to YonixAlpha's own
  FIFO ledger, marked "provider-reported; not verified by YonixAlpha and
  never used as a trade signal". They do not change validation, scores,
  copy decisions or the safety gate.
- **Paid, so off by default.**
  - Keys (`NANSEN_API_KEY`, `MADEONSOL_API_KEY`, and now `GITHUB_TOKEN`)
    are set and tested from Settings, never returned.
  - Nothing is called until "Enrichment on" is set in Smart Wallets →
    External intelligence.
  - Calls are limited by a daily budget per provider (default 100 Nansen,
    200 MadeOnSol) and a refresh window per wallet (24 h).
  - A refusing provider (401 / 402 / 429) is not called again in that pass,
    and errors go to Telegram (throttled).
- **Order.** Copy targets first, then validated / paper-followed wallets,
  then the highest-scored wallets still collecting history. Every
  10 minutes, `wallets_per_pass` per provider.
- **Discovery (§25)**, a separate switch:
  - once a day per provider and chain, the Nansen smart-money traders and
    the MadeOnSol KOL leaderboard (30 d) are stored as CANDIDATES;
  - candidates are listed with their own history here (or INSUFFICIENT
    DATA);
  - candidates are never copied;
  - the operator can only add a NOTIFY target, which buys nothing;
  - a candidate becomes VALIDATED only through its own trades and the
    validation gates.

**Tests:**
- core `test_enrichment`: request shapes and headers, BSC → bnb,
  unsupported chains, error classes, budget, refresh window, a refusing
  provider, discovery once a day, nothing copied;
- api `test_enrichment_api`: off by default, settings validated and
  audited, profiles carry provider records, manual lookup without keys
  calls nothing, update guidance;
- copy-engine: step off until switched on.

**NOT VERIFIED until real keys are used:** the provider responses beyond
the fields typed in their SDKs. The Nansen smart-money row shape is read
by `trader_address`; an unrecognised shape is reported, never guessed.

## 26. M12 — EVM opportunities and wallet behaviour as ML data (2026-10-03)

Everything in this phase is review data. No entry, exit or size reads it,
and ML contribution stays 0 %. Real-data results are NOT VERIFIED until the
server has collected enough samples: a model trains only from 200 labelled
samples.

### M12a: every EVM opportunity is a sample

Source: the observation state machine (section 15). Each BSC / Robinhood
observation becomes one sample, whether it was traded, waited or rejected
(`yonixalpha_core/ml/evm_samples.py`, table `evm_ml_samples`).

| Part | Rule |
|---|---|
| Decision point | the observation's T+5 snapshot, the same moment for every token, so traded and untraded opportunities are comparable |
| Features | only T0 and T+5 data: trades, buyers, sellers, volumes, holders, effective buyers, top buyer share, flows, smart-money buyers, creator trades, market cap, price change since T0, chain / category / launchpad. Unknown stays unknown, with a `__missing` flag, never 0 |
| Live state | liquidity and curve progress are read from the chain, not from trades. They are used only when read within 90 s of the snapshot; a later read could carry the future, so it is left missing |
| Labels | from the token's trades in the hour after T+5: reached +50 %, reached +100 %, fell 50 % within 10 min (fast dump), return after 60 min, max drawdown, migrated within the hour |
| Executable return | only for a traded sample: the result of its closed paper position, fees and taxes included |
| Left out | curves quoted in another token (Four.meme tokenized stocks: not BNB volumes), and observations without a T+5 snapshot or a price (stored as unknown, never trained on) |

The sample is built once the outcome hour has passed, and only while the
trades are still retained (14 days). Building is idempotent: one row per
observation.

### §41 comparison: BUY / WAIT / REJECT

Each sample records four verdicts:
- **deterministic**: did the trade signal qualify (BUY / WAIT);
- **risk**: did safety allow it (ALLOW / REJECT);
- **final**: what the system did (BUY = entered, REJECT = rejected, WAIT = expired without entry);
- **ML**: from the shadow models once trained. BUY when P(+50 %) is at least 0.5, REJECT when P(fast dump) is at least 0.5, WAIT otherwise.

ML Review → EVM shows, per recommender and verdict:
- the share that reached +50 % and +100 %;
- the share that dumped fast;
- the mean and median hour return;
- the executable paper result.

It also shows the final action's missed winners (not bought, reached
+100 %) and bad entries (bought, dumped fast).

**In-sample rule.** A model's predictions on samples older than its holdout
start are marked IN_SAMPLE. They are never counted as an ML BUY / WAIT /
REJECT. Only out-of-sample verdicts measure whether ML would add anything.

SELL / HOLD (exits) are not compared yet.

### M12b: wallet behaviour labels (§36-37)

Scope: BSC / Robinhood wallets with a profile, contracts excluded
(`yonixalpha_core/ml/wallet_labels.py`, table `wallet_trade_labels`). A
token is labelled a day after its launch, once, from the retained trades.

| Label | Rule |
|---|---|
| SUCCESSFUL_ENTRY_PATTERN | the price reached +50 % over the wallet's entry within an hour, before any -50 % |
| FAILED_ENTRY_PATTERN | -50 % first, or +50 % never reached within the hour |
| LATE_ENTRY | bought at 3x or more the token's first price |
| PREMATURE_EXIT | the price doubled within an hour after its last sell |
| LATE_EXIT | up 2x while held, then sold (or still holds) at half that peak or less |
| MISSED_WINNER | a token of a launchpad the wallet was trading doubled in its first hour, and the wallet never bought it (copy targets and validated wallets only) |

Features at the entry use only what was known then:
- token age;
- entry multiple;
- buyers, trades and volumes before the entry;
- entry size;
- the wallet's earlier episodes, counted only once their outcome hour had passed (`available_at`).

A shadow model `shadow_wallet_p_successful_entry` is trained from them.

Smart Wallets shows the summary per wallet ("Entries": successful / failed,
missed winners) and every labelled entry in the wallet detail. Behaviour
labels describe how a wallet traded; they are never a reason to copy or
buy.

### Models

The ml service runs this every training cycle after the Solana models
(`services/ml/app/evm_ml.py`).

| Model | Target |
|---|---|
| shadow_evm_p_upside_50, _p_upside_100, _p_fast_dump, _p_migrate | binary outcomes of the hour after T+5 |
| shadow_evm_e_return_60m, _e_max_drawdown | regression |
| shadow_wallet_p_successful_entry | wallet entry outcome |

Training follows the same method as the Solana shadow models:
- time split (oldest 75 % train, newest 25 % holdout) with a purge gap;
- holdout metrics;
- no training when no new samples arrived.

Registry handling:
- each model is registered with status "shadow";
- a newer version marks the older one superseded, never deleted;
- these models are not "challenger", so the promotion flow cannot pick them up.

A cycle failure is recorded as `evm_ml_cycle_failed` (error, so it reaches
Telegram).

### Verified here

- core: features without look-ahead (late live state dropped), labels, verdicts, comparison, wallet labels, prior-only wallet features, both builders idempotent, missed winners;
- ml service: 300 synthetic samples train and register every model, and in-sample predictions are marked IN_SAMPLE;
- api: `/api/ml/evm` counts, comparison and contribution 0; behaviour on profiles and `/api/wallets/behaviour`;
- migration 0034: upgrade, downgrade and check.

NOT VERIFIED on real data.

### Server observation (deploy 1232c4b)

data-evm logged one ConnectTimeout on `eth_call` from both Robinhood
endpoints (Alchemy and the public one) at the same time. Token safety
reported "unavailable" for that read, which means NO_TRADE. That is the
intended behaviour. If it repeats, Providers will show the endpoints'
plan health.
